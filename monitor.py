"""Build the static dashboard from a single exchange-time snapshot."""

import argparse
import html
import json
import shutil
import os
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path

from market import BENCHMARKS, CST, collect
import temperature
from temperature import wilder_rsi


ROOT = Path(__file__).resolve().parent
LABELS = {"first": "触及首档", "second": "触及二档", "signal": "指标触发",
          "watch": "未触发", "blocked": "溢价拦截", "verify": "待核验溢价",
          "observe": "仅观察", "observe_hit": "观察条件已到", "missing": "缺少行情",
          "stale": "历史行情", "partial": "部分数据待更新"}
HITS = {"first", "second", "signal"}


def session_end(now, market):
    t = now.time()
    lunch, close = (time(12), time(16)) if market == "HK" else (time(11, 30), time(15))
    if now.weekday() < 5:
        for start, end in ((time(9, 30), lunch), (time(13), close)):
            if start <= t < end:
                return datetime.combine(now.date(), end, now.tzinfo)
    return None


def in_session(now, market):
    return session_end(now, market) is not None


def fresh(at, now):
    if not at or len(at) <= 10:
        return False
    stamp = datetime.fromisoformat(at)
    return stamp.date() == now.date() and -120 <= (now - stamp).total_seconds() <= 25 * 60


def current(q, now, market):
    if not q or not q.get("at") or q.get("volume") == 0 or now.weekday() >= 5:
        return False
    if q["at"][:10] != now.date().isoformat():
        return False
    if len(q["at"]) == 10:
        return now.time() >= time(15)
    stamp = datetime.fromisoformat(q["at"])
    if stamp > now + timedelta(minutes=2) or stamp.time() < time(9, 30):
        return False
    return not in_session(now, market) or fresh(q["at"], now)


def with_tick(bars, quote):
    if not quote:
        return list(bars)
    day = quote["at"][:10]
    return [bar for bar in bars if bar[0] < day] + [(day, quote["price"])]


def previous_session(data, day, market="CN"):
    benchmark = BENCHMARKS[market]
    bars = data.get("history:" + benchmark, [])
    older = [(d, v) for d, v in bars if d < day]
    if not older:
        return None
    if any(d == day for d, _ in bars):
        return older[-1][0]
    # Some intraday feeds append today's bar only after the close. In that
    # case verify the last historical close against today's benchmark quote.
    index = data["quotes"].get(benchmark) or {}
    close = index.get("prev_close")
    if index.get("at", "")[:10] == day and close and abs(older[-1][1] - close) < 0.005:
        return older[-1][0]
    return None


def indicator_closes(item, data, quote, market):
    at = quote["at"]
    day = at[:10]
    bars = data.get("history:" + item["code"], [])
    prior = [(d, value) for d, value in bars if d < day]
    if not prior or prior[-1][0] != previous_session(data, day, market):
        return []
    closes = [value for _, value in prior]
    if item["type"] == "场外基金":
        # Latest unit NAV and the compounded return series have different scales.
        published = next((value for d, value in bars if d == day), None) if len(at) == 10 else None
        if published is not None:
            closes.append(published)
        elif quote.get("pct") is not None and quote["pct"] > -100:
            closes.append(closes[-1] * (1 + quote["pct"] / 100))
        else:
            return []
    else:
        closes.append(quote["price"])
    return closes


def macd_histogram(closes):
    if len(closes) < 35:
        return None
    fast = slow = closes[0]
    signal = 0.0
    for price in closes[1:]:
        fast += (price - fast) * 2 / 13
        slow += (price - slow) * 2 / 27
        diff = fast - slow
        signal += (diff - signal) * 2 / 10
    return 2 * (diff - signal)


def add_indicator(row, key, label, value, first, second, unit=""):
    if value is None or first is None or round(value, 8) > first:
        return
    deep = second is not None and round(value, 8) <= second
    row["signals"].append(key + "_second" if deep else key)
    threshold = second if deep else first
    row["reasons"].append(f"{label}{'二档' if deep else '首档'} {value:.2f}{unit} ≤ {threshold:g}{unit}")


def hit_label(signals):
    if "second" in signals:
        return LABELS["second"]
    if "first" in signals:
        return LABELS["first"]
    labels = []
    if "cold_second" in signals or "cold" in signals:
        labels.append("超低温" if "cold_second" in signals else "低温")
    for prefix, name in (("rsi", "RSI 预警"), ("index", "指数跌幅")):
        matches = [key for key in signals if key.startswith(prefix)]
        if matches:
            tier = "二档" if any(key.endswith("_second") for key in matches) else "首档"
            labels.append(f"{name} · {tier}")
    return " / ".join(labels) or LABELS["signal"]


def fund_quote(item, data, now):
    code, today = item["code"], now.date().isoformat()
    idx = data["quotes"].get("csi" + item["index"])
    navs = data.get("nav:" + code) or {}
    past = [day for day in navs if day <= today]
    nav_day = max(past, default=None)
    nav = navs.get(nav_day)
    direct = data.get("estimate:" + code)
    official = {**nav, "at": nav_day, "source": "天天基金 · 已公布净值"} if nav else None
    extra = {"nav": nav["price"] if nav else None, "nav_day": nav_day,
             "index_pct": idx.get("pct") if idx else None, "index_at": idx.get("at") if idx else None}
    if nav_day == today and now.time() >= time(15):
        return official, extra
    if direct and current(direct, now, "CN"):
        return direct, extra
    if idx and current(idx, now, "CN") and idx.get("pct") is not None:
        # A one-day index move requires the immediately preceding trading day's NAV.
        prior = navs.get(previous_session(data, today))
        if prior:
            return {"price": prior["price"] * (1 + idx["pct"] / 100), "pct": idx["pct"],
                    "at": idx["at"], "source": "前一交易日净值 × 指数涨跌"}, extra
    candidates = [q for q in (official, direct) if q and q["at"][:10] <= today]
    return max(candidates, key=lambda q: q["at"], default=None), extra


def evaluate(item, data, now):
    code = item["code"]
    market = "HK" if code.startswith("hk") else "CN"
    q = data["quotes"].get(code)
    extras = {}
    if item.get("index"):
        q, extras = fund_quote(item, data, now)
    q = q or {}
    price, at = q.get("price"), q.get("at")
    row = {**item, **extras, "market": market, "currency": "HKD" if market == "HK" else "CNY",
           "price": price, "pct": q.get("pct"), "at": at, "source": q.get("source"),
           "premium": None, "iopv": q.get("iopv"), "rsi": None, "rsi7": None, "drawdown": None,
           "macd": None, "yield_pct": None, "gap": None,
           "signals": [], "reasons": [], "signal_at": None}
    valid = current(q, now, market)
    bars = temperature.closed_history(data, item, now)
    row.update(temperature.reading(
        item, bars, q, now, previous_session(data, now.date().isoformat(), market),
        valid and in_session(now, market) and fresh(at, now)))
    if price:
        if not item.get("index") or valid:
            row["gap"] = (price / item["first"] - 1) * 100
        if row["iopv"]:
            row["premium"] = (price - row["iopv"]) * 100 / row["iopv"]
        cash = data.get("cash:" + code)
        if cash is not None:
            row["yield_pct"] = 100 * cash / price
        closes = indicator_closes(item, data, q, market)
        if closes:
            row["rsi"] = wilder_rsi(closes)
            if item.get("rsi7_max") is not None:
                row["rsi7"] = wilder_rsi(closes, 7)
            if item["type"] == "ETF":
                row["macd"] = macd_histogram(closes)
            window = item.get("drawdown_days")
            if window and len(closes) >= window:
                row["drawdown"] = (price / max(closes[-window:]) - 1) * 100
        factors = row["temperature_components"]
        if factors and at and row["temperature_at"][:10] == at[:10]:
            row["rsi"], row["rsi7"] = factors["rsi14"], factors["rsi7"]
    signals, reasons = row["signals"], row["reasons"]
    if valid:
        basis = ("公布净值" if len(at) == 10 else "估算净值") if item.get("index") else "价格"
        if item.get("second") and price <= item["second"]:
            signals.append("second")
            reasons.append(f"{basis}到达二档 ≤ {item['second']:g}")
        elif price <= item["first"]:
            signals.append("first")
            reasons.append(f"{basis}到达首档 ≤ {item['first']:g}")
        add_indicator(row, "rsi", "RSI(14)", row["rsi"], item.get("rsi_max"), item.get("rsi_second_max"))
        add_indicator(row, "rsi7", "RSI(7)", row["rsi7"], item.get("rsi7_max"), item.get("rsi7_second_max"))
        temp = row["temperature"]
        if row["temperature_live"] and temp is not None and temp <= temperature.LIMITS[1]:
            signals.append("cold_second" if temp <= temperature.LIMITS[0] else "cold")
            reasons.append(f"{temperature.band(temp)} {temp:g}°")
        if row["drawdown"] is not None and round(row["drawdown"], 8) < -item["drawdown_pct"]:
            signals.append("drawdown")
            reasons.append(f"{item['drawdown_days']} 日收盘回撤超过 {item['drawdown_pct']}%")
        row["signal_at"] = at
    idx = data["quotes"].get("csi" + item["index"]) if item.get("index") else None
    valid_index = current(idx, now, "CN")
    if valid_index:
        add_indicator(row, "index", f"{item['index']} 指数日涨跌", idx.get("pct"),
                      item["index_drop"], item.get("index_drop_second"), "%")
        if any(s.startswith("index") for s in signals):
            row["signal_at"] = idx["at"]
    if not (valid or valid_index):
        status = "stale" if price else "missing"
    elif item.get("observe"):
        cold_alert = item.get("temperature_alerts") and any(s.startswith("cold") for s in signals)
        status = "signal" if cold_alert else "observe_hit" if signals else "observe"
    elif signals and "premium_max" in item and not current(data["quotes"].get(BENCHMARKS["HK"]), now, "HK"):
        reasons.append("港股基准行情未更新")
        status = "verify"
    elif signals and "premium_max" in item and row["premium"] is None:
        status = "verify"
    elif signals and "premium_max" in item and round(row["premium"], 8) > item["premium_max"]:
        status = "blocked"
    elif "second" in signals:
        status = "second"
    elif "first" in signals:
        status = "first"
    elif not signals and ((item.get("rsi_max") and row["rsi"] is None)
                          or (item.get("rsi7_max") and row["rsi7"] is None)
                          or (item.get("index") and (not valid or not valid_index or idx["pct"] is None))):
        status = "partial"
    else:
        status = "signal" if signals else "watch"
    active_signals = [s for s in signals if s.startswith("cold")] if item.get("observe") else signals
    row.update(status=status, label=hit_label(active_signals) if status in HITS else LABELS[status])
    return row


def build(watchlist, data, now):
    rows = [evaluate(item, data, now) for item in watchlist]
    if not any(r["price"] or r.get("index_at") for r in rows):
        raise RuntimeError("所有监控行情源均无可用数据，本次停止发布。")
    rows.sort(key=lambda r: (r["gap"] is None, r["gap"] or 0, r["code"]))
    benchmarks = {market: data["quotes"].get(code) for market, code in BENCHMARKS.items()}
    history = data.get("history:sh000001", [])
    series = with_tick(history, benchmarks["CN"])[-60:]
    day = now.date().isoformat()
    last_dates = [d for d, _ in history if d < day]
    benchmark_day = ((benchmarks["CN"] or {}).get("at") or "")[:10]
    if benchmark_day and benchmark_day < day:
        last_dates.append(benchmark_day)
    return {"generated": now.isoformat(timespec="seconds"), "rows": rows, "cn_trading_day": data.get("cn_trading_day"),
            "cn_previous_close": max(last_dates, default=None),
            "groups": len({r.get("group", r["code"]) for r in rows}),
            "benchmarks": benchmarks, "series": series,
            "session": {m: in_session(now, m) and current(q, now, m) for m, q in benchmarks.items()}}


def write_page(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as f:
        f.write(content)
    try:
        os.replace(f.name, path)
    finally:
        Path(f.name).unlink(missing_ok=True)


def render(snapshot, output, data=None):
    output.mkdir(parents=True, exist_ok=True)
    template = ROOT / "dashboard"
    serialized = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    index = (template / "index.html").read_text(encoding="utf-8")
    table = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in
                    (r["name"], r["code"], r["price"] or "—", r["first"], r["label"])) + "</tr>"
                    for r in snapshot["rows"])
    index = index.replace("__ROWS__", table).replace("__SNAPSHOT__", serialized.replace("<", "\\u003c"))
    for asset in ("style.css", "app.js", "thermometer.css", "thermometer.js"):
        if not (output / asset).exists() or (template / asset).stat().st_mtime_ns != (output / asset).stat().st_mtime_ns:
            shutil.copy2(template / asset, output / asset)
    folder = output / "thermometers"
    folder.mkdir(exist_ok=True)
    template_text = (template / "thermometer.html").read_text(encoding="utf-8")
    now = datetime.fromisoformat(snapshot["generated"])
    for row in snapshot["rows"]:
        bars = temperature.closed_history(data or {}, row, now)
        if row.get("reference_close") is not None:
            bars.append((now.date().isoformat(), row["reference_close"]))
        source = "天天基金 · 公布净值日增长率连乘" if row.get("index") else "Yahoo Finance · 复权日收盘"
        page = temperature.page_data(row, bars, source)
        payload = json.dumps(page, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")
        write_page(folder / (row["code"] + ".html"), template_text.replace("__THERMO_DATA__", payload))
    write_page(output / "snapshot.json", serialized)
    write_page(output / "index.html", index)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs")
    args = parser.parse_args()
    items = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    now = datetime.now(CST)
    data = collect(items, now.date())
    snapshot = build(items, data, datetime.now(CST))
    render(snapshot, args.output, data)
    print(f"Built {len(snapshot['rows'])} rows, {snapshot['groups']} groups at {snapshot['generated']}")


if __name__ == "__main__":
    main()
