"""Read public market data. All observation times use the exchange's UTC+8 clock."""

import concurrent.futures
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


def read(url, *, encoding="utf-8", referer="https://finance.qq.com/"):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    for attempt in range(2):
        try:
            with urlopen(request, timeout=6) as response:
                return response.read().decode(encoding)
        except (OSError, http.client.HTTPException):
            if attempt:
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
    if code.startswith("csi"):
        return "2." + code[3:]
    if code == "hkHSI":
        return "100.HSI"
    prefix = {"sh": "1.", "sz": "0.", "hk": "116."}[code[:2]]
    return prefix + code[2:]


def eastmoney_quotes(codes):
    ids = {secid(code): code for code in codes}
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
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + query
    data = json.loads(read(url))["data"][code]
    bars = data.get("qfqday") or data.get("day") or []
    closes = {}
    for bar in bars:
        if isinstance(bar, list) and len(bar) >= 3 and number(bar[2], positive=True):
            closes[date.fromisoformat(bar[0]).isoformat()] = float(bar[2])
    return sorted(closes.items())


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


def dividends(code, today):
    symbol = code[2:] + {"sh": ".SS", "sz": ".SZ"}[code[:2]]
    params = urlencode({"interval": "1mo", "range": "2y", "events": "div"})
    raw = read(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?{params}",
               referer="https://finance.yahoo.com/")
    chart = json.loads(raw)["chart"]["result"][0]
    if chart["meta"].get("currency") != "CNY" or chart["meta"].get("symbol") != symbol:
        return None
    distributions = chart.get("events", {}).get("dividends", {}).values()
    # Use the ex-date inside the event, not the monthly bucket's dictionary key.
    cash = [float(d["amount"]) for d in distributions if number(d.get("amount"), positive=True)
            and today - timedelta(days=365) < datetime.fromtimestamp(d["date"], CST).date() <= today]
    return sum(cash) if cash else None


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


def collect(watchlist, today, *, references=None):
    exchange = [i["code"] for i in watchlist if i["type"] != "场外基金"]
    etfs = {i["code"] for i in watchlist if i["type"] == "ETF"}
    funds = [i for i in watchlist if i["type"] == "场外基金"]
    codes = exchange + list(BENCHMARKS.values())
    jobs = {"primary": (tencent_quotes, codes, etfs),
            "backup": (eastmoney_quotes, codes + ["csi" + i["index"] for i in funds])}
    for code in sorted(etfs | {BENCHMARKS["CN"]}):
        jobs["history:" + code] = (history, code)
    for item in funds:
        code = item["code"]
        jobs["estimate:" + code] = (estimate, code)
        jobs["nav:" + code] = (reported_navs, code)
    for item in watchlist:
        if item.get("yield_min"):
            jobs["cash:" + item["code"]] = (dividends, item["code"], today)
    # Intraday clients may reuse daily bars, reported NAV and cash distributions.
    # Quotes, the benchmark and the fund estimate are fetched on every call.
    result = {k: v for k, v in (references or {}).items()
              if k in jobs and k.startswith(("history:", "nav:", "cash:"))}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fn, *args): key for key, (fn, *args) in jobs.items() if key not in result}
        for future in concurrent.futures.as_completed(futures):
            key = futures[future]
            try:
                result[key] = future.result()
            except DATA_ERRORS as exc:
                print(f"Data unavailable: {key} ({type(exc).__name__})")
    result["quotes"] = combine_quotes(result.pop("primary", {}), result.pop("backup", {}))
    return result
