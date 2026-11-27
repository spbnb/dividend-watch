"""Read public market data. All observation times use the exchange's UTC+8 clock."""

import concurrent.futures
import html
import http.client
import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen


CST = timezone(timedelta(hours=8))
BENCHMARKS = {"CN": "sh000001", "HK": "hkHSI"}
# Price-index quote identifiers include the exchange to avoid equity code collisions.
INDEX_BENCHMARKS = {
    "H30269": ("中证红利低波动指数", "H30269", "2.H30269", "CN"),
    "000922": ("中证红利指数", "000922", "1.000922", "CN"),
    "980092": ("国证自由现金流指数", "980092", "0.980092", "CN"),
    "930914": ("中证港股通高股息投资指数", "930914", "2.930914", "HK"),
    "HSHYLV": ("恒生港股通高股息低波动指数", "HSHYLV", "124.HSHYLV", "HK"),
    "SPCLLHCP": ("标普中国A股大盘红利低波50指数", "SPCLLHCP", None, "CN"),
}
ETF_INDEX_CODES = {"sh512890": "H30269", "sh515180": "000922", "sz159201": "980092",
                   "sh513530": "930914", "sz159545": "HSHYLV", "sh515450": "SPCLLHCP"}
ETF_BENCHMARKS = {code: INDEX_BENCHMARKS[symbol] for code, symbol in ETF_INDEX_CODES.items()}
DATA_ERRORS = (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, IndexError)


def number(value, *, positive=False):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and (not positive or result > 0) else None


def timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, CST).isoformat(timespec="seconds")
    for pattern in ("%Y%m%d%H%M%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=CST).isoformat()
        except (ValueError, TypeError):
            continue
    return None


def read(url, *, encoding="utf-8", referer="https://finance.qq.com/", attempts=2):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=6) as response:
                return response.read().decode(encoding)
        except (OSError, http.client.HTTPException):
            if attempt + 1 == attempts:
                raise
            time.sleep(0.4)


def tencent_quotes(codes, etfs):
    raw = read("https://qt.gtimg.cn/q=" + ",".join(codes), encoding="gb18030")
    quotes = {}
    for code, payload in re.findall(r'v_(\w+)="([^"]*)";', raw):
        f = payload.split("~")
        if len(f) <= 32 or code not in codes or f[2] != code[2:]:
            continue
        price, at = number(f[3], positive=True), timestamp(f[30])
        if price is None or not at:
            continue
        q = {"price": price, "pct": number(f[32]), "at": at, "prev_close": number(f[4], positive=True),
             "volume": number(f[6]), "source": "腾讯财经"}
        if code in etfs and len(f) > 78:
            iopv, premium = number(f[78], positive=True), number(f[77])
            if iopv and premium is not None and abs((price / iopv - 1) * 100 - premium) < 0.08:
                q["iopv"] = iopv
        quotes[code] = q
    return quotes


def secid(code):
    if code in INDEX_BENCHMARKS:
        return INDEX_BENCHMARKS[code][2]
    if code.startswith("idx:"):
        return code[4:]
    if code.startswith("csi"):
        return "2." + code[3:]
    if code == "hkHSI":
        return "100.HSI"
    prefix = {"sh": "1.", "sz": "0.", "hk": "116."}[code[:2]]
    return prefix + code[2:]


def eastmoney_quotes(codes):
    ids = {key: code for code in codes if (key := secid(code)) is not None}
    if not ids:
        return {}
    params = urlencode({"secids": ",".join(ids), "fltt": 2, "invt": 2,
                        "fields": "f2,f3,f5,f12,f13,f18,f124,f297,f441"})
    for host in ("push2", "push2delay"):
        try:
            url = f"https://{host}.eastmoney.com/api/qt/ulist.np/get?{params}"
            payload = json.loads(read(url, referer="https://quote.eastmoney.com/"))
            records = (payload.get("data") or {}).get("diff") or []
            if isinstance(records, dict):
                records = records.values()
            result = {}
            for f in records:
                code = ids.get(f"{f.get('f13')}.{f.get('f12')}")
                price, epoch = number(f.get("f2"), positive=True), number(f.get("f124"), positive=True)
                if not code or price is None or epoch is None:
                    continue
                at = timestamp(epoch)
                data_day = str(f.get("f297") or "-")
                if data_day.isdigit() and len(data_day) == 8 and at[:10].replace("-", "") != data_day:
                    continue
                result[code] = {"price": price, "pct": number(f.get("f3")), "at": at,
                                "volume": number(f.get("f5")), "source": "东方财富",
                                "prev_close": number(f.get("f18"), positive=True),
                                "iopv": number(f.get("f441"), positive=True)}
            if result:
                return result
        except DATA_ERRORS:
            continue
    return {}


def history(code):
    query = urlencode({"param": f"{code},day,,,110,qfq"})
    # The mainland endpoint silently returns unadjusted bars for Hong Kong stocks.
    hk_stock = code.startswith("hk") and code != BENCHMARKS["HK"]
    endpoint = "hkfqkline" if hk_stock else "fqkline"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/{endpoint}/get?" + query
    data = json.loads(read(url))["data"][code]
    bars = data.get("qfqday") or ([] if hk_stock else data.get("day", []))
    closes = {}
    for bar in bars:
        if isinstance(bar, list) and len(bar) >= 3 and number(bar[2], positive=True):
            closes[date.fromisoformat(bar[0]).isoformat()] = float(bar[2])
    return sorted(closes.items())


def adjusted_history(code, today):
    """One split- and dividend-adjusted series; a later adjustment scales all earlier bars alike."""
    if code.startswith("hk"):
        symbol = f"{int(code[2:]):04d}.HK"
        currency = "HKD"
    else:
        symbol = code[2:] + {"sh": ".SS", "sz": ".SZ"}[code[:2]]
        currency = "CNY"
    start = date(2018, 1, 1)
    def epoch(day):
        return int(datetime.combine(day, datetime.min.time(), CST).timestamp())
    query = urlencode({"interval": "1d", "period1": epoch(start), "period2": epoch(today + timedelta(days=1))})
    for host in ("query1", "query2"):
        try:
            response = json.loads(read(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?{query}",
                                       referer="https://finance.yahoo.com/", attempts=1))
            chart = response["chart"]["result"][0]
            meta = chart["meta"]
            if meta.get("symbol") != symbol or meta.get("currency") != currency or meta.get("dataGranularity") != "1d":
                continue
            # Never substitute raw closes. In particular, some ETFs have synthetic
            # zero-volume NAV observations on Yahoo before the first exchange trade.
            values = chart["indicators"]["adjclose"][0]["adjclose"]
            volumes = chart["indicators"]["quote"][0]["volume"]
            stamps = chart["timestamp"]
            if len(values) != len(stamps) or len(volumes) != len(stamps):
                continue
            bars = {}
            for stamp, value, volume in zip(stamps, values, volumes):
                close = number(value, positive=True)
                if close is None or not number(volume, positive=True):
                    continue
                day = datetime.fromtimestamp(stamp, CST).date()
                if start <= day <= today:
                    bars[day.isoformat()] = close
            if bars:
                return sorted(bars.items())
        except DATA_ERRORS:
            continue
    return []


def fund_history(code, limit=110):
    """Build a comparable daily series from published NAV returns, including distributions."""
    raw = read(f"https://fund.eastmoney.com/pingzhongdata/{code}.js", referer="https://fund.eastmoney.com/")
    identity = re.search(r'\bvar\s+fS_code\s*=\s*"([0-9]+)"', raw)
    trend = re.search(r"\bvar\s+Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", raw, re.S)
    if not identity or identity[1] != code or not trend:
        return []
    returns = {}
    for entry in json.loads(trend[1]):
        if not isinstance(entry, dict):
            continue
        stamp = number(entry.get("x"), positive=True)
        if stamp is not None:
            try:
                day = datetime.fromtimestamp(stamp / 1000, CST).date().isoformat()
            except (OSError, OverflowError, ValueError):
                continue
            returns[day] = (number(entry.get("equityReturn"))
                            if number(entry.get("y"), positive=True) else None)
    bars = []
    level = 1.0
    days = sorted(returns.items())
    for day, change in (days[-limit:] if limit is not None else days):
        if change is None or change <= -100:
            bars.clear()
            level = 1.0
            continue
        level *= 1 + change / 100
        bars.append((day, level))
    return bars


def estimate(code):
    raw = read(f"https://fundgz.1234567.com.cn/js/{code}.js", referer="https://fund.eastmoney.com/")
    match = re.fullmatch(r"\s*jsonpgz\((\{.*?\})\);?\s*", raw, re.S)
    if match:
        data = json.loads(match[1])
        price, at = number(data.get("gsz"), positive=True), timestamp(data.get("gztime"))
        if data.get("fundcode") == code and price and at:
            return {"price": price, "pct": number(data.get("gszzl")), "at": at,
                    "source": "天天基金盘中估值"}
    return None


def reported_navs(code):
    params = urlencode({"fundCode": code, "pageIndex": 1, "pageSize": 5})
    raw = read("https://api.fund.eastmoney.com/f10/lsjz?" + params,
               referer="https://fundf10.eastmoney.com/")
    rows = json.loads(raw)["Data"]["LSJZList"]
    return {date.fromisoformat(r["FSRQ"]).isoformat(): {"price": float(r["DWJZ"]),
            "pct": number(r.get("JZZZL"))} for r in rows if number(r.get("DWJZ"), positive=True)}


def cn_trading_day(today):
    """Read the dated trading-day flag published by the Shenzhen Stock Exchange."""
    url = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?" + urlencode({"month": today.strftime("%Y-%m")})
    try:
        payload = json.loads(read(url, referer="https://www.szse.cn/disclosure/deal/"))
    except DATA_ERRORS:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    flags = [r.get("jybz") for r in data if isinstance(r, dict) and r.get("jyrq") == today.isoformat()]
    if flags == ["1"] and today.weekday() < 5:
        return True
    return False if flags == ["0"] else None


def cash_per_current_unit(events, today):
    """Events contain ex-date, cash per previous unit and new units per previous unit."""
    cash = 0.0
    for day, amount, factor in sorted(events):
        if today - timedelta(days=365) < day <= today:
            cash = (cash + amount) / factor
    return cash


def dividends(code, today):
    symbol = code[2:] + {"sh": ".SH", "sz": ".SZ"}[code[:2]]
    fields = "SECUCODE,EX_DIVIDEND_DATE,ASSIGN_PROGRESS,PRETAX_BONUS_RMB,BONUS_IT_RATIO"
    params = urlencode({"reportName": "RPT_SHAREBONUS_DET", "columns": fields,
                        "filter": f'(SECUCODE="{symbol}")', "pageNumber": 1, "pageSize": 500,
                        "source": "WEB", "client": "WEB"})
    raw = read("https://datacenter-web.eastmoney.com/api/data/v1/get?" + params, referer="https://data.eastmoney.com/")
    payload = json.loads(raw)
    result = payload.get("result") or {}
    rows = result.get("data") or []
    if not payload.get("success") or not rows or result.get("count") != len(rows):
        return None
    if not any(r.get("ASSIGN_PROGRESS") == "实施分配" for r in rows):
        return None
    events = []
    for row in rows:
        if row.get("SECUCODE") != symbol or not row.get("ASSIGN_PROGRESS"):
            return None
        if row.get("ASSIGN_PROGRESS") != "实施分配":
            continue
        day = date.fromisoformat(row["EX_DIVIDEND_DATE"][:10])
        if not today - timedelta(days=365) < day <= today:
            continue
        # Both fields are quoted per TEN pre-distribution shares.
        cash = number(row["PRETAX_BONUS_RMB"]) if row["PRETAX_BONUS_RMB"] is not None else 0.0
        shares = number(row["BONUS_IT_RATIO"]) if row["BONUS_IT_RATIO"] is not None else 0.0
        if cash is None or shares is None or min(cash, shares) < 0 or cash + shares == 0:
            return None
        events.append((day, cash / 10, 1 + shares / 10))
    return cash_per_current_unit(events, today)


def fund_distributions(code, today):
    """Use the separate cash distribution and split tables for the identified fund share class."""
    raw = read(f"https://fundf10.eastmoney.com/fhsp_{code}.html", referer="https://fund.eastmoney.com/")
    if not re.search(r"<title>[^<]*\(" + re.escape(code) + r"\)基金分红送配", raw):
        return None
    tables = re.findall(r"<table\b[^>]*>(.*?)</table>", raw, re.S | re.I)
    events = {}
    for is_cash, header, empty in ((True, "每10份分红", "暂无分红信息"), (False, "拆分折算比例", "暂无拆分信息")):
        sections = [t for t in tables if re.search(r"<th\b[^>]*>" + header + r"</th>", t)]
        if len(sections) != 1:
            return None
        body = re.search(r"<tbody\b[^>]*>(.*?)</tbody>", sections[0], re.S)
        if not body:
            return None
        rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", body[1], re.S)
        if not rows:
            return None
        for row in rows:
            cells = [html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()
                     for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)]
            if len(rows) == len(cells) == 1 and cells[0].rstrip("!！") == empty:
                continue
            if len(cells) != (5 if is_cash else 4):
                return None
            day = date.fromisoformat(cells[2 if is_cash else 1])
            if not today - timedelta(days=365) < day <= today:
                continue
            cash, shares = events.get(day, (0.0, 1.0))
            if is_cash:
                match = re.fullmatch(r"每10份派现金([\d.]+)元", cells[3])
                amount = number(match[1], positive=True) if match else None
                if amount is None:
                    return None
                cash += amount / 10
            else:
                match = re.fullmatch(r"1[:：]([\d.]+)", cells[3])
                factor = number(match[1], positive=True) if match else None
                if factor is None:
                    return None
                shares *= factor
            events[day] = cash, shares
    return cash_per_current_unit([(day, *values) for day, values in events.items()], today)


def combine_quotes(primary, backup):
    merged = dict(backup)
    for code, quote in primary.items():
        other = backup.get(code)
        if other:
            delta = (datetime.fromisoformat(other["at"]) - datetime.fromisoformat(quote["at"])).total_seconds()
            if delta > 600 or other["at"][:10] > quote["at"][:10]:
                continue
            if not quote.get("iopv") and other.get("iopv") and abs(delta) <= 300:
                quote = {**quote, "iopv": other["iopv"]}
        merged[code] = quote
    return merged


def benchmark_reference(code, today):
    code = ETF_INDEX_CODES.get(code, code)
    if code in {"HSHYLV", "SPCLLHCP"}:
        from global_reference import get_reference
    else:
        from csi_reference import get_reference
    return get_reference(code, today)


def index_close_quote(code, today):
    """Last published price-index close for display if dated quotes are unavailable."""
    if code == "HSHYLV":
        from global_reference import hang_seng_price_bars
        bars = hang_seng_price_bars(today)
        provider = "恒生指数"
    elif code in {"H30269", "000922", "930914", "980092"}:
        from csi_reference import cni_bars, csi_bars
        loader = cni_bars if code == "980092" else csi_bars
        provider = "国证指数" if code == "980092" else "中证指数"
        bars = loader(code, (today - timedelta(days=35)).isoformat(), today)
    else:
        return None
    market = INDEX_BENCHMARKS[code][3]
    end_hour = 16 if market == "HK" else 15
    now = datetime.now(CST)
    # Official daily endpoints can include today's unfinished bar. Date-only
    # closes carry a session-time label and never qualify as intraday quotes.
    for day, close in reversed(bars):
        at = datetime.fromisoformat(f"{day}T{end_hour:02d}:00:00").replace(tzinfo=CST)
        if at <= now and at.date() <= today:
            return {"price": close, "at": at.isoformat(), "source": provider + "日收盘",
                    "prev_close": None, "pct": None, "quote_type": "close", "date_only": True}
    return None


def collect(watchlist, today, *, references=None):
    exchange = [i["code"] for i in watchlist if i["type"] not in {"场外基金", "指数"}]
    etfs = {i["code"] for i in watchlist if i["type"] == "ETF"}
    indexes = {i["code"] for i in watchlist if i["type"] == "指数" and i["code"] in INDEX_BENCHMARKS}
    funds = [i for i in watchlist if i["type"] == "场外基金"]
    codes = exchange + list(BENCHMARKS.values())
    legacy = {ETF_INDEX_CODES[code] for code in etfs.intersection(ETF_INDEX_CODES)}
    wanted = indexes | legacy
    index_quotes = sorted(code for code in wanted if INDEX_BENCHMARKS[code][2])
    jobs = {"primary": (tencent_quotes, codes, etfs),
            "backup": (eastmoney_quotes, codes + ["csi" + i["index"] for i in funds] + index_quotes)}
    if "SPCLLHCP" in wanted:
        from global_reference import sp_price_quote
        jobs["sp_quote"] = (sp_price_quote, today)
    for code in indexes | etfs.intersection(ETF_BENCHMARKS):
        jobs["benchmark:" + code] = (benchmark_reference, code, today)
    calendar_key = "calendar:" + today.isoformat()
    jobs[calendar_key] = (cn_trading_day, today)
    for code in sorted(set(exchange).union(BENCHMARKS.values())):
        jobs["history:" + code] = (history, code)
    for code in exchange:
        jobs["long:" + code] = (adjusted_history, code, today)
    fund_short_keys = set()
    for item in funds:
        code = item["code"]
        jobs["estimate:" + code] = (estimate, code)
        jobs["nav:" + code] = (reported_navs, code)
        jobs["long:" + code] = (fund_history, code, None)
        if item.get("rsi_max") is not None or item.get("rsi7_max") is not None:
            fund_short_keys.add("history:" + code)
    for item in watchlist:
        code = item["code"]
        if item["type"] in {"ETF", "场外基金"}:
            jobs["cash:" + code] = (fund_distributions, code[-6:], today)
        elif item["type"] != "指数" and code.startswith(("sh", "sz")):
            jobs["cash:" + code] = (dividends, code, today)
    # Intraday clients may reuse daily bars, reported NAV and cash distributions.
    # Quotes, the benchmark and the fund estimate are fetched on every call.
    result = {k: v for k, v in (references or {}).items()
              if k in fund_short_keys or k == calendar_key or (k in jobs and k.startswith(("history:", "long:", "nav:", "cash:", "benchmark:")))}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fn, *args): key for key, (fn, *args) in jobs.items() if key not in result}
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                result[key] = future.result()
            except DATA_ERRORS as exc:
                print(f"Data unavailable: {key} ({type(exc).__name__})")
    result["quotes"] = combine_quotes(result.pop("primary", {}), result.pop("backup", {}))
    quotes = result["quotes"]
    sp_quote = result.pop("sp_quote", None)
    if sp_quote:
        quotes["SPCLLHCP"] = sp_quote
    for code in wanted:
        if code in quotes and quotes[code]["at"][:10] > today.isoformat():
            del quotes[code]
    require_close = {code for code in indexes - {"SPCLLHCP"}
                     if code not in quotes or quotes[code]["at"][:10] < today.isoformat()}
    if require_close:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(require_close)) as pool:
            futures = {pool.submit(index_close_quote, code, today): code for code in require_close}
            for future in concurrent.futures.as_completed(futures):
                try:
                    close = future.result()
                except DATA_ERRORS + (EOFError,):
                    continue
                code = futures[future]
                if close and (code not in quotes or close["at"][:10] > quotes[code]["at"][:10]):
                    quotes[code] = close
    for code in wanted.intersection(quotes):
        q = quotes[code]
        q.update(symbol=code, kind="价格指数", currency="HKD" if INDEX_BENCHMARKS[code][3] == "HK" else "CNY")
        q.setdefault("quote_type", "quote")
        q.pop("iopv", None)
        if code in legacy and INDEX_BENCHMARKS[code][2]:
            quotes["idx:" + INDEX_BENCHMARKS[code][2]] = q
    # A published index level has no traded units. A provider's zero-volume
    # placeholder cannot indicate a suspension as it can for individual stocks.
    for code in (wanted | set(BENCHMARKS.values())).intersection(quotes):
        if quotes[code].get("volume") == 0:
            quotes[code].pop("volume")
    result["cn_trading_day"] = result.get(calendar_key)
    for key in fund_short_keys:
        # Derive the existing normalized 110-day RSI series from the same download.
        series = result.get("long:" + key.split(":", 1)[1])
        if key not in result and series:
            base = series[-111][1] if len(series) > 110 else 1.0
            result[key] = [(day, value / base) for day, value in series[-110:]]
    return result
