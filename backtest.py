"""Compare temperature entry rules with next-close execution on the monitored securities."""

import argparse
import csv
import hashlib
import json
from bisect import bisect_left, insort
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from statistics import correlation, mean, median

import market
import temperature as temp


ROOT = Path(__file__).resolve().parent
RULES = {"current": "现行综合温度 ≤25", "ultra": "现行超低温 ≤10", "no_fast": "价格位置和 RSI14 各半 ≤25",
         "rank": "250 日价格位置 ≤25", "rsi": "RSI14 ≤30",
         "trend_cold": "综合低温且高于 200 日均线", "monthly": "10 月均线择时", "hold": "买入持有"}


def download(code, cutoff):
    prices = market.adjusted_history(code, cutoff)
    ref = market.benchmark_reference(code, cutoff) if code in market.ETF_BENCHMARKS else {}
    if len(prices) < 2:
        raise ValueError(f"{code} missing ETF/stock trading history")
    return code, {"prices": prices, "ref": ref}


def factors(bars):
    closes = [v for _, v in bars]
    window, months, result = [], [], []
    trend = None
    for i, ((day, close), slow, fast) in enumerate(zip(bars, temp.rsi_series(closes, 14), temp.rsi_series(closes, 7))):
        pos = temp.rank(close, window)
        if i >= temp.WINDOW:
            window.pop(bisect_left(window, closes[i - temp.WINDOW]))
        insort(window, close)
        # Only a completed calendar month can set the monthly allocation.
        # Its end-of-month signal is first tradable at a strictly later ETF close.
        if i + 1 < len(bars) and day[:7] != bars[i + 1][0][:7]:
            months.append(close)
            if len(months) >= 10:
                trend = close > mean(months[-10:])
        if pos is None or trend is None:
            continue
        score = temp.composite(pos, slow, fast)
        result.append((day, {"current": score <= 25, "no_fast": round((pos + slow) / 2, 1) <= 25,
                             "rank": pos <= 25, "rsi": slow <= 30,
                             "trend_cold": score <= 25 and close > mean(closes[i-199:i+1]),
                             "monthly": trend, "slow": slow, "fast": fast, "ultra": score <= 10}))
    return result


def aligned(prices, readings, start, end):
    """Decision available strictly before each execution close, including cross-market holidays."""
    days = [d for d, _ in readings]
    rows = []
    for day, close in prices:
        if not start <= day <= end or not days or day <= days[0]:
            continue
        j = bisect_left(days, day) - 1
        signal_day, state = readings[j]
        fresh = (date.fromisoformat(day) - date.fromisoformat(signal_day)).days <= 10
        if not fresh and not rows:
            continue
        # Ignore signal levels already active before the test/listing start.
        prev = rows[-1][3] if rows else readings[j-1][1] if j else {}
        rows.append((day, close, signal_day, state if fresh else {}, prev))
    return rows


def simulate(rows, key, fee):
    nav = peak = 1.0
    worst = 0.0
    exposed = entries = intervals = 0
    entry = due = previous_price = last_signal = None
    finished = []
    for i, (day, price, signal_day, state, prior) in enumerate(rows):
        if previous_price is not None:
            intervals += 1
            if entry is not None:
                nav *= price / previous_price
                exposed += 1
        # Intraperiod drawdown is measured at daily closes, including costs.
        new_signal = signal_day != last_signal
        active = state.get(key, False)
        if key == "hold":
            target = True
        elif key == "monthly":
            target = active if state else entry is not None
        else:
            target = day < due if entry is not None else bool(new_signal and active and not prior.get(key))
        if entry is not None and not target:
            nav *= 1 - fee
            finished.append(100 * (price / entry * (1 - fee)**2 - 1))
            entry = due = None
        elif entry is None and target and i < len(rows) - 1:
            nav *= 1 - fee
            entry = price
            due = temp.anniversary(date.fromisoformat(day), 1).isoformat()
            entries += 1
        if i == len(rows) - 1 and entry is not None:
            nav *= 1 - fee
        peak = max(peak, nav)
        worst = min(worst, nav / peak - 1)
        previous_price, last_signal = price, signal_day
    years = (date.fromisoformat(rows[-1][0]) - date.fromisoformat(rows[0][0])).days / 365.2425
    return {"start": rows[0][0], "end": rows[-1][0], "cagr": round(100*(nav ** (1 / years)-1), 3),
            "drawdown": round(100*worst, 3), "exposure": round(100*exposed/intervals, 2),
            "entries": entries, "completed": len(finished),
            "positive": sum(x > 0 for x in finished), "sum_completed_pct": round(sum(finished), 4)}


def table(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def compare(inputs, cutoff, dest):
    rows, diagnostics, basis_checks, index_checks = [], [], [], []
    for code, data in sorted(inputs.items()):
        prices = [(d, p) for d, p in data["prices"] if d <= cutoff]
        ref = data.get("ref") or {}
        idx = [(d, p) for d, p in (ref.get("bars") or []) if d <= cutoff]
        use_index = len(idx) > temp.WINDOW and idx[-1][0] >= prices[-1][0][:7] + "-01"
        bars = idx if use_index else prices
        basis = ref["symbol"] if use_index else code + "复权"
        study_start = max("2023-01-01", (ref.get("launch") or "1900") if use_index else "1900")
        if use_index and code == "sz159201":
            study_start = max(study_start, "2024-08-15")
        signal = factors(bars)
        total = aligned(prices, signal, study_start, cutoff)
        if len(total) < 2:
            raise ValueError(f"{code} insufficient aligned data")
        used = {s: v for _, _, s, v, _ in total if v}
        states = list(used.values())
        def signature(series):
            return hashlib.sha256(json.dumps(series, separators=(",", ":")).encode()).hexdigest()[:16]
        diagnostics.append({"code": code, "basis": basis, "start": total[0][0], "end": total[-1][0],
                            "signal_days": len(states), "cold_days": sum(s["current"] for s in states),
                            "ultra_days": sum(s["ultra"] for s in states),
                            "rsi_correlation": round(correlation([s["slow"] for s in states], [s["fast"] for s in states]), 4),
                            "signal_hash": signature(bars), "trade_hash": signature(prices)})
        for period, start, end, fee in (("full", study_start, cutoff, .001), ("early", study_start, "2024-12-31", .001),
                                        ("late", max(study_start, "2025-01-01"), cutoff, .001),
                                        ("high_cost", study_start, cutoff, .003)):
            sample = aligned(prices, signal, start, end)
            if len(sample) < 2:
                continue
            for key in RULES:
                rows.append({"code": code, "basis": basis, "period": period, "fee": fee, "rule": key,
                             **simulate(sample, key, fee)})
        if use_index:
            live_since = max(ref.get("launch") or idx[0][0], ref.get("method_since") or "1900")
            for period, end in (("published", cutoff), ("pre_2023", "2022-12-31")):
                sample = aligned(idx, signal, live_since, end)
                if len(sample) < 250:
                    continue
                for key in RULES:
                    index_checks.append({"code": code, "basis": basis, "kind": ref["kind"], "currency": ref["currency"],
                                         "period": period, "rule": key, **simulate(sample, key, .001)})
            fund_signal = factors(prices)
            etf_days = aligned(prices, fund_signal, study_start, cutoff)
            if len(etf_days) > 1:
                start = max(etf_days[0][0], total[0][0])
                for label, series in ((basis, signal), (code + "复权", fund_signal)):
                    sample = aligned(prices, series, start, cutoff)
                    basis_checks.append({"code": code, "basis": label, **simulate(sample, "current", .001)})
    dest.mkdir(parents=True, exist_ok=True)
    table(dest / "strategy-results.csv", rows)
    table(dest / "strategy-inputs.csv", diagnostics)
    if basis_checks:
        table(dest / "strategy-basis.csv", basis_checks)
    if index_checks:
        table(dest / "strategy-indices.csv", index_checks)
    for cohort in ("ETF", "stock"):
        for period in ("full", "early", "late", "high_cost"):
            for key in RULES:
                part = [r for r in rows if (r["code"] in market.ETF_BENCHMARKS) == (cohort == "ETF")
                        and r["rule"] == key and r["period"] == period]
                if part:
                    print(cohort, period, key, len(part), *[round(median(r[k] for r in part), 2)
                          for k in ("cagr", "drawdown", "exposure")], sum(r["entries"] for r in part),
                          sum(r["completed"] for r in part), sum(r["positive"] for r in part))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--cache", type=Path, help="private input cache outside the public dashboard")
    parser.add_argument("--output", type=Path, default=ROOT / "research")
    args = parser.parse_args()
    if args.cache and args.cache.is_file():
        payload = json.loads(args.cache.read_text(encoding="utf-8"))
        if payload["as_of"] != args.as_of.isoformat():
            raise ValueError("cache date differs")
        inputs = payload["data"]
    else:
        items = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
        codes = [item["code"] for item in items if item["type"] != "场外基金"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            inputs = dict(pool.map(lambda code: download(code, args.as_of), codes))
        if args.cache:
            args.cache.parent.mkdir(exist_ok=True, parents=True)
            args.cache.write_text(json.dumps({"as_of": args.as_of.isoformat(), "data": inputs}), encoding="utf-8")
    compare(inputs, args.as_of.isoformat(), args.output)


if __name__ == "__main__":
    main()
