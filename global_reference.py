"""Public Hang Seng and S&P index series and S&P price-index quotes."""

import gzip
import html
import json
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from market import CST, DATA_ERRORS, ETF_INDEX_CODES, number


HANG_SENG = "https://www.hsi.com.hk"
HSI_INDEXES = HANG_SENG + "/api/wsit-hsi-ddoc-ea-public-website-proxy/v1/product-data/pub/indexes/metadata/v2"
HSI_FACTSHEET = HANG_SENG + "/static/uploads/contents/en/dl_centre/factsheets/hshylve.pdf"
SOUTHERN = "https://www.nffund.com"
SOUTHERN_PAGE = SOUTHERN + "/new/personal-financing/detail.html?fundCode=515450"
SOUTHERN_YIELD_NOTE = ("https://edu.nffund.com/html/moneyschool/monerymiddledetail.html"
                       "?article_id=1a1f1a719b73b206af178594bdaaf8b5&id=2-2")
SP_NAME = "标普中国A股大盘红利低波50指数"
FT_SP = "https://markets.ft.com/data/indices/tearsheet/summary?s=SPCLLHCP:IOM"
SP_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/%5ESPCLLHCP?interval=1d&range=1d"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _fetch(url, referer, form=None):
    body = urlencode(form).encode() if form else None
    request = Request(url, data=body, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    with urlopen(request, timeout=8) as response:
        content = response.read()
    # Southern sometimes sends gzip even without an Accept-Encoding header.
    return gzip.decompress(content) if content.startswith(b"\x1f\x8b") else content


def _observations(rows, date_key, value_key, today, *, cumulative=False, start=date.min):
    levels = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = row.get(date_key)
        if not isinstance(stamp, str):
            continue
        if cumulative and re.fullmatch(r"[0-9]{8}", stamp):
            stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", stamp):
            continue
        try:
            day = date.fromisoformat(stamp)
        except ValueError:
            continue
        quoted = number(row.get(value_key))
        if quoted is None or not start <= day <= today or day.weekday() >= 5:
            continue
        # Southern publishes cumulative returns, in percentage points, relative
        # to one common base. Zero is a valid return; blanks are missing.
        level = 100.0 + quoted if cumulative else quoted
        if level > 0:
            levels[day.isoformat()] = level
    return sorted(levels.items())


def _hang_seng_bars(today, symbol="12033.00"):
    name = "Hang Seng SCHK High Dividend Low Volatility Index"
    if symbol == "12033.00":
        name += " Total Return Index"
    query = urlencode({"data": "valueHistoryPub", "language": "en-hk", "indexCodes": symbol})
    report = json.loads(_fetch(f"{HSI_INDEXES}?{query}", HANG_SENG))
    if isinstance(report, dict) and report.get("code") == "0":
        for series in report.get("data") or []:
            if isinstance(series, dict) and series.get("indexCode") == symbol and series.get("indexName") == name:
                return _observations(series.get("valueHistory") or [], "date", "close", today)
    return []


def hang_seng_price_bars(today):
    return _hang_seng_bars(today, "02033.00")


def _ft_sp_quote(today):
    page = _fetch(FT_SP, "https://markets.ft.com/").decode("utf-8")
    header = re.search(r'class="mod-tearsheet-overview__header__symbol"[^>]*>\s*<span>SPCLLHCP:IOM</span>', page)
    if not header:
        return None
    block = page[header.end():].partition('class="mod-ui-show-more"')[0]
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", block)).split())
    price_match = re.search(r"\bPrice \(CNY\) ([\d,]+(?:\.\d+)?)\b", text)
    stamp = re.search(r"\bas of ([A-Z][a-z]{2}) (\d{1,2}) (\d{4}) (\d\d):(\d\d) (GMT|UTC|BST)\b", text)
    if not price_match or not stamp or stamp[1] not in MONTHS:
        return None
    price = number(price_match[1].replace(",", ""), positive=True)
    zone = timezone(timedelta(hours=1 if stamp[6] == "BST" else 0))
    at = datetime(int(stamp[3]), MONTHS.index(stamp[1]) + 1, int(stamp[2]), int(stamp[4]), int(stamp[5]),
                  tzinfo=zone).astimezone(CST)
    if price is None or at.date() > today or at.weekday() >= 5:
        return None
    quote = {"price": price, "at": at.isoformat(), "prev_close": None, "pct": None,
             "source": "FT Markets（延时指数行情）"}
    change = re.search(r"Today['’]s Change ([+\-−]?[\d,.]+) / ([+\-−]?[\d.]+)%", text)
    if change:
        move = number(change[1].replace(",", "").replace("−", "-"))
        pct = number(change[2].replace("−", "-"))
        if move is not None and pct is not None:
            prev_close = round(price - move, 6)
            if prev_close > 0 and abs(100 * move / prev_close - pct) < 0.025:
                quote.update(prev_close=prev_close, pct=pct)
    return quote


def _yahoo_sp_quote(today):
    chart = json.loads(_fetch(SP_YAHOO, "https://finance.yahoo.com/"))["chart"]["result"][0]
    meta = chart["meta"]
    if not isinstance(meta, dict):
        return None
    identity = tuple(meta.get(key) for key in ("symbol", "currency", "instrumentType", "exchangeName"))
    if identity != ("^SPCLLHCP", "CNY", "INDEX", "SNP"):
        return None
    price = number(meta.get("regularMarketPrice"), positive=True)
    stamp = number(meta.get("regularMarketTime"), positive=True)
    if price is None or stamp is None:
        return None
    at = datetime.fromtimestamp(stamp, CST)
    if at.date() > today or at.weekday() >= 5:
        return None
    # Yahoo's snapshot level and timestamp match FT, but its previous close
    # for this index can refer to a different session. Do not infer a return.
    return {"price": price, "at": at.isoformat(), "prev_close": None, "pct": None,
            "source": "Yahoo Finance（标普价格指数点位）"}


def sp_price_quote(today):
    try:
        quote = _ft_sp_quote(today)
    except DATA_ERRORS + (EOFError, OverflowError):
        quote = None
    if quote and quote["at"][:10] == today.isoformat():
        now = datetime.now(CST)
        observed = datetime.fromisoformat(quote["at"])
        if now.date() != today or now - observed <= timedelta(minutes=20):
            return quote
    try:
        backup = _yahoo_sp_quote(today)
    except DATA_ERRORS + (EOFError, OverflowError):
        backup = None
    # FT timestamps have minute precision. Prefer its verified previous close
    # for the same minute, while allowing a newer Yahoo price through.
    if backup and (not quote or backup["at"][:16] > quote["at"][:16]):
        return backup
    return quote


def _factsheet_yield(text, today):
    # Read the HSHYLV row, not the objective, an HSI comparator, or an ETF yield.
    table = text.partition("INDEX FUNDAMENTALS")[2].partition("CORRELATION")[0]
    if "Dividend Yield (%)" not in table:
        return None
    value = re.search(r"\bHSHYLV\s+(\d+(?:\.\d+)?)\s+\d+(?:\.\d+)?\b", table)
    stamp = re.search(r"\bAll data as at\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})\b", text)
    if not value or not stamp or stamp[2] not in MONTHS:
        return None
    try:
        day = date(int(stamp[3]), MONTHS.index(stamp[2]) + 1, int(stamp[1]))
    except ValueError:
        return None
    pct = number(value[1])
    if day <= today and pct is not None and 0 <= pct <= 100:
        return day.isoformat(), pct
    return None


def _hang_seng_yield(today):
    binary = shutil.which("pdftotext")
    if not binary:
        return None
    pdf = _fetch(HSI_FACTSHEET, HANG_SENG)
    if not pdf.startswith(b"%PDF-"):
        return None
    # The Windows Xpdf binary bundled with Git requires a named input file.
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "hshylv.pdf"
        source.write_bytes(pdf)
        converted = subprocess.run([binary, "-layout", str(source), "-"], capture_output=True,
                                   timeout=10, check=True)
    return _factsheet_yield(converted.stdout.decode("utf-8", errors="replace"), today)


def _southern_data(endpoint, **fields):
    reply = json.loads(_fetch(f"{SOUTHERN}/nfwebApi/fund/{endpoint}", SOUTHERN_PAGE,
                              {"fundCode": "515450", **fields}))
    if not isinstance(reply, dict) or reply.get("code") != "ETS-5BP00000" or not isinstance(reply.get("data"), dict):
        return {}
    return reply["data"]


def _southern_bars(today):
    details = _southern_data("overreview").get("fund_info") or {}
    if details.get("fundCode") != "515450" or details.get("yjbjjz") != SP_NAME + "收益率":
        return []
    # The website sends selector 7 for 成立以来. brList is the benchmark;
    # jzlist is a separate CSI 300 comparator, and thisfundlist is the ETF.
    data = _southern_data("earningsMovements", months=7)
    return _observations(data.get("brList") or [], "fdate", "upratio", today, cumulative=True,
                         start=date(2020, 2, 25))


def get_reference(code, today):
    code = ETF_INDEX_CODES.get(code, code)
    if code not in ("HSHYLV", "SPCLLHCP"):
        return {}
    result = {"bars": [], "yield_pct": None, "yield_at": None, "yield_history": []}
    if code == "HSHYLV":
        result.update(name="恒生港股通高股息低波动全收益指数", symbol="HSHYLV TRI", kind="全收益指数",
                      currency="HKD", market="HK", provider="恒生指数有限公司", launch="2017-05-08",
                      url=HANG_SENG + "/en-hk/indexes/all-indexes/index-details/?seriesCode=hshylv",
                      note="官方图表提供约五年港元全收益日线。盘中点位使用港元价格指数。")
        try:
            result["bars"] = _hang_seng_bars(today)
        except DATA_ERRORS + (EOFError,):
            pass
        try:
            observed = _hang_seng_yield(today)
        except DATA_ERRORS + (EOFError, subprocess.SubprocessError):
            observed = None
        if observed:
            result.update(yield_at=observed[0], yield_pct=observed[1], yield_history=[observed],
                          yield_method="恒生指数月报成分股股息率", yield_history_source=HSI_FACTSHEET)
    else:
        result.update(name=SP_NAME, symbol="SPCLLHCP", kind="价格指数", currency="CNY", market="CN",
                      provider="南方基金·业绩比较基准（重定基100）", launch="2019-04-01", url=SOUTHERN_PAGE,
                      note="按南方基金披露的指数累计收益率重定基100，原始涨幅精度0.01个百分点。"
                           "日线从2020-02-25起，缺失日留空。价格口径不含红利再投资。")
        try:
            result["bars"] = _southern_bars(today)
        except DATA_ERRORS + (EOFError,):
            pass
        # This dated, rounded Wind observation in the manager's 19 Nov article
        # is available only from the publication date. It is not a daily feed.
        if today >= date(2026, 11, 19):
            result.update(yield_pct=4.4, yield_at="2026-11-18", yield_published_at="2026-11-19", yield_approx=True,
                          yield_method="南方基金引述Wind的指数股息率（约数，单次披露）",
                          yield_history_source=SOUTHERN_YIELD_NOTE)
    return result
