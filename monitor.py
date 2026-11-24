"""Build the static dashboard from a single exchange-time snapshot."""

import argparse
import html
import json
import shutil
from datetime import datetime, time, timedelta
from pathlib import Path

from market import BENCHMARKS, CST, collect


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


def previous_session(data, day):
    bars = data.get("history:sh000001", [])
    older = [(d, v) for d, v in bars if d < day]
    if not older:
        return None
    if any(d == day for d, _ in bars):
        return older[-1][0]
    # Some intraday feeds append today's bar only after the close. In that
    # case verify the last historical close against today's benchmark quote.
    index = data["quotes"].get(BENCHMARKS["CN"]) or {}
    close = index.get("prev_close")
    if index.get("at", "")[:10] == day and close and abs(older[-1][1] - close) < 0.005:
        return older[-1][0]
    return None


def wilder_rsi(closes, period=14):
    if len(closes) <= period:
        return None
    diffs = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(d, 0) for d in diffs[:period]) / period
    loss = sum(max(-d, 0) for d in diffs[:period]) / period
    for d in diffs[period:]:
        gain = (gain * (period - 1) + max(d, 0)) / period
        loss = (loss * (period - 1) + max(-d, 0)) / period
    return 100 * gain / (gain + loss) if gain + loss else 50.0


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
           "premium": None, "iopv": q.get("iopv"), "rsi": None, "drawdown": None,
           "macd": None, "yield_pct": None, "gap": None,
           "signals": [], "reasons": [], "signal_at": None}
    valid = current(q, now, market)
    if price:
        if not item.get("index") or valid:
            row["gap"] = (price / item["first"] - 1) * 100
        if row["iopv"]:
            row["premium"] = (price - row["iopv"]) * 100 / row["iopv"]
        cash = data.get("cash:" + code)
        if cash:
            row["yield_pct"] = 100 * cash / price
        bars = data.get("history:" + code, [])
        # A cached series ending before the last session must not generate an indicator signal.
        previous = previous_session(data, at[:10])
        if previous and any(d == previous for d, _ in bars):
            closes = [v for _, v in with_tick(bars, q)]
            row["rsi"] = wilder_rsi(closes)
            row["macd"] = macd_histogram(closes)
            window = item.get("drawdown_days")
            if window and len(closes) >= window:
                row["drawdown"] = (price / max(closes[-window:]) - 1) * 100
    signals, reasons = row["signals"], row["reasons"]
    if valid:
        if item.get("second") and price <= item["second"]:
            signals.append("second")
            reasons.append("价格到达二档")
        elif price <= item["first"]:
            signals.append("first")
            reasons.append("净值到达目标" if item.get("index") else "价格到达首档")
        if "rsi_max" in item and row["rsi"] is not None and row["rsi"] <= item["rsi_max"]:
            signals.append("rsi")
            reasons.append(f"RSI(14) ≤ {item['rsi_max']}")
        if row["drawdown"] is not None and round(row["drawdown"], 8) < -item["drawdown_pct"]:
            signals.append("drawdown")
            reasons.append(f"{item['drawdown_days']} 日收盘回撤超过 {item['drawdown_pct']}%")
        row["signal_at"] = at
    idx = data["quotes"].get("csi" + item["index"]) if item.get("index") else None
    valid_index = current(idx, now, "CN")
    if valid_index and idx["pct"] is not None and idx["pct"] <= item["index_drop"]:
        signals.append("index")
        reasons.append(f"跟踪指数跌幅达到 {abs(item['index_drop'])}%")
        row["signal_at"] = idx["at"]
    if not (valid or valid_index):
        status = "stale" if price else "missing"
    elif item.get("observe"):
        status = "observe_hit" if signals else "observe"
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
                          or (item.get("index") and (not valid or not valid_index or idx["pct"] is None))):
        status = "partial"
    else:
        status = "signal" if signals else "watch"
    row.update(status=status, label=LABELS[status])
    return row


def build(watchlist, data, now):
    rows = [evaluate(item, data, now) for item in watchlist]
    if not any(r["price"] or r.get("index_at") for r in rows):
        raise RuntimeError("所有监控行情源均无可用数据，本次停止发布。")
    rows.sort(key=lambda r: (r["gap"] is None, r["gap"] or 0, r["code"]))
    benchmarks = {market: data["quotes"].get(code) for market, code in BENCHMARKS.items()}
    series = with_tick(data.get("history:sh000001", []), benchmarks["CN"])[-60:]
    return {"generated": now.isoformat(timespec="seconds"), "rows": rows,
            "groups": len({r.get("group", r["code"]) for r in rows}),
            "benchmarks": benchmarks, "series": series,
            "session": {m: in_session(now, m) and current(q, now, m) for m, q in benchmarks.items()}}


def render(snapshot, output):
    output.mkdir(parents=True, exist_ok=True)
    template = ROOT / "dashboard"
    serialized = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    index = (template / "index.html").read_text(encoding="utf-8")
    table = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in
                    (r["name"], r["code"], r["price"] or "—", r["first"], r["label"])) + "</tr>"
                    for r in snapshot["rows"])
    index = index.replace("__ROWS__", table).replace("__SNAPSHOT__", serialized.replace("<", "\\u003c"))
    for asset in ("style.css", "app.js"):
        shutil.copyfile(template / asset, output / asset)
    (output / "snapshot.json").write_text(serialized, encoding="utf-8")
    (output / "index.html").write_text(index, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs")
    args = parser.parse_args()
    items = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    now = datetime.now(CST)
    data = collect(items, now.date())
    snapshot = build(items, data, datetime.now(CST))
    render(snapshot, args.output)
    print(f"Built {len(snapshot['rows'])} rows, {snapshot['groups']} groups at {snapshot['generated']}")


if __name__ == "__main__":
    main()
