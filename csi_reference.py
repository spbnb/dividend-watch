"""Official closing series and dated index dividend yields."""

import gzip
import http.client
import json
import math
import struct
import time
from datetime import date, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from market import ETF_INDEX_CODES


CSI = "https://www.csindex.com.cn"
CNI = "https://www.cnindex.com.cn"
CNI_DAILY = "https://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
CSI_INDICATOR = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/indicator"

# Price-index code, corresponding total-return index, its name, base date, publication date.
SERIES = {
    "H30269": ("H30269", "H20269", "中证红利低波动全收益指数", "2005-12-30", "2013-12-19"),
    "000922": ("000922", "H00922", "中证红利全收益指数", "2004-12-31", "2008-05-26"),
    "930914": ("930914", "H20914", "中证港股通高股息投资全收益指数", "2014-11-14", "2016-11-25"),
    "980092": ("980092", "480092", "国证自由现金流全收益指数", "2012-12-31", "2012-12-28"),
    "931446": ("931446", "921446", "中证东方红红利低波动全收益指数", "2009-12-31", "2020-04-21"),
}
# Existing historical research calls can still resolve former watchlist keys.
SERIES.update({fund: SERIES[symbol] for fund, symbol in ETF_INDEX_CODES.items() if symbol in SERIES})
SERIES["012708"] = SERIES["931446"]
SOURCE_ERRORS = (OSError, EOFError, http.client.HTTPException, ValueError, KeyError, TypeError, IndexError)


def _download(url, referer):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer,
                                    "Accept-Encoding": "gzip"})
    for attempt in range(2):
        try:
            with urlopen(request, timeout=10) as response:
                data = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    data = gzip.decompress(data)
                return data
        except (OSError, EOFError, http.client.HTTPException) as exc:
            if attempt or isinstance(exc, HTTPError) and exc.code < 500 and exc.code not in {408, 429}:
                raise
            time.sleep(0.3)


def _day(value):
    if not isinstance(value, str):
        return None
    if len(value) == 8 and value.isascii() and value.isdigit():
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _series(rows, start, today, *, allow_zero=False):
    result = {}
    for raw_day, raw_value in rows:
        day = _day(raw_day)
        if not day or not start <= day <= today or day.weekday() >= 5:
            continue
        try:
            value = float(raw_value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(value) and (value > 0 or allow_zero and value == 0):
            result[day.isoformat()] = value
    return sorted(result.items())


def csi_bars(symbol, base, today):
    """Accept either official price or total-return code. Never splice different series."""
    start = date.fromisoformat(base)
    # If a query starts on a holiday CSI repeats the NEXT trading day's close
    # on that requested start date. Put any synthetic boundary outside our range.
    query_start = start - timedelta(days=1) if start > date.min else start
    params = urlencode({"indexCode": symbol, "startDate": query_start.strftime("%Y%m%d"),
                        "endDate": today.strftime("%Y%m%d")})
    data = json.loads(_download(f"{CSI}/csindex-home/perf/index-perf?{params}", CSI))
    if not isinstance(data, dict) or data.get("success") is not True:
        return []
    rows = data.get("data")
    if not isinstance(rows, list):
        return []
    return _series(((row["tradeDate"], row.get("close")) for row in rows
                    if isinstance(row, dict) and row.get("indexCode") == symbol and "tradeDate" in row),
                   start, today)


def cni_bars(symbol, base, today):
    params = urlencode({"indexCode": symbol, "startDate": base, "endDate": today.isoformat(), "frequency": "day"})
    payload = json.loads(_download(f"{CNI_DAILY}?{params}", CNI))
    if not isinstance(payload, dict) or payload.get("code") != 200:
        return []
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("indexCode") != symbol:
        return []
    fields = data.get("item") or []
    if not isinstance(fields, list) or "timestamp" not in fields or "close" not in fields:
        return []
    day, close = fields.index("timestamp"), fields.index("close")
    return _series(((row[day], row[close]) for row in data.get("data") or []
                    if isinstance(row, list) and len(row) > max(day, close)),
                   date.fromisoformat(base), today)


def _yield_rows(rows, symbol, today):
    # CSI's D/P2 uses index calculation shares. D/P1 weights by total share capital.
    columns = next(rows)
    day = columns.index("日期Date")
    code = columns.index("指数代码Index Code")
    dp = columns.index("股息率2（计算用股本）D/P2")
    return _series(((row[day], row[dp]) for row in rows
                    if len(row) > max(day, code, dp) and row[code] == symbol),
                   date.min, today, allow_zero=True)


def csi_yields(symbol, today):
    try:
        import xlrd
    except ImportError:
        return []
    data = _download(f"{CSI_INDICATOR}/{symbol}indicator.xls", CSI)
    if not data.startswith(bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1")):
        return []
    try:
        workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
        try:
            sheet = workbook.sheet_by_index(0)
            if not sheet.nrows:
                return []
            return _yield_rows((sheet.row_values(n) for n in range(sheet.nrows)), symbol, today)
        finally:
            workbook.release_resources()
    except (xlrd.XLRDError, xlrd.compdoc.CompDocError, struct.error) as error:
        raise ValueError("Invalid CSI valuation workbook") from error


def get_reference(code, today):
    if code not in SERIES:
        return {}
    price_code, symbol, name, base, launch = SERIES[code]
    cni = price_code == "980092"
    hkd = price_code == "930914"
    url = (f"{CNI}/module/index-detail.html?act_menu=1&indexCode={price_code}" if cni
           else f"{CSI}/#/indices/family/detail?indexCode={price_code}")
    result = {"bars": [], "name": name, "symbol": symbol, "kind": "全收益指数",
              "currency": "HKD" if hkd else "CNY", "market": "HK" if hkd else "CN",
              "provider": "国证指数" if cni else "中证指数", "url": url, "launch": launch,
              "yield_pct": None, "yield_at": None, "yield_history": []}
    if cni:
        result["method_since"] = "2024-08-15"
        result["note"] = ("前身指数于2012-12-28发布。2024-08-15更名并修订选样、权重与调样规则，"
                          "此前数据不能代表现行策略的实盘表现。")
    elif hkd:
        result["note"] = "价格指数和全收益指数均按港元计价。"
    try:
        result["bars"] = (cni_bars if cni else csi_bars)(symbol, base, today)
    except SOURCE_ERRORS:
        pass
    if not cni:
        try:
            yields = csi_yields(price_code, today)
        except SOURCE_ERRORS:
            yields = []
        if yields:
            result.update(yield_at=yields[-1][0], yield_pct=yields[-1][1], yield_history=yields,
                          yield_method="中证指数股息率2（按计算用股本加权）",
                          yield_history_source=f"{CSI_INDICATOR}/{price_code}indicator.xls")
    return result
