"""Compare close-to-close index strategies. Both signals and returns use the same index."""

import argparse
import csv
import hashlib
import json
import math
from bisect import bisect_left, bisect_right, insort
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from statistics import correlation, mean, median

import csi_reference
import global_reference
import temperature as temp


ROOT = Path(__file__).resolve().parent
SYMBOLS = {"H30269": "H20269", "000922": "H00922", "930914": "H20914", "980092": "480092",
           "HSHYLV": "HSHYLV TRI", "SPCLLHCP": "SPCLLHCP"}
ABSOLUTE = ("current", "ultra", "no_fast", "rank", "rsi", "trend_cold", "monthly", "hold")
ALL = ABSOLUTE + ("cold_pct", "ultra_pct")
FEES = (0.0, .001, .003)


def download(code, cutoff):
    adapter = global_reference if code in ("HSHYLV", "SPCLLHCP") else csi_reference
    return code, adapter.get_reference(code, cutoff)


def factors(bars):
    closes = [v for _, v in bars]
    window, months, result = [], [], []
    monthly = None
    for i, ((day, close), slow, fast) in enumerate(zip(bars, temp.rsi_series(closes, 14), temp.rsi_series(closes, 7))):
        pos = temp.rank(close, window)
        if i >= temp.WINDOW:
            window.pop(bisect_left(window, closes[i - temp.WINDOW]))
        insort(window, close)
        # By the next observation's execution close, the prior month's last observed
        # close is known. Only its successor's calendar month, never its price, is used.
        if i + 1 < len(bars) and day[:7] != bars[i + 1][0][:7]:
            months.append(close)
            if len(months) >= 10:
                monthly = months[-1] > mean(months[-10:])
        if pos is None:
            continue
        score = temp.composite(pos, slow, fast)
        result.append((day, {"current": score <= 25, "ultra": score <= 10,
                             "no_fast": round((pos + slow) / 2, 1) <= 25, "rank": pos <= 25,
                             "rsi": slow <= 30, "trend_cold": score <= 25 and close > mean(closes[i-199:i+1]),
                             "monthly": monthly, "slow": slow, "fast": fast, "score": score}))
    return result


def relative_temperature(readings, window=750):
    """Compare today's rounded score with earlier observations, averaging ties."""
    ordered, queue, result = [], deque(), []
    for day, state in readings:
        score = state["score"]
        if len(queue) == window:
            pct = 50 * (bisect_left(ordered, score) + bisect_right(ordered, score)) / window
            result.append((day, {**state, "cold_pct": pct <= 25, "ultra_pct": pct <= 10}))
            ordered.pop(bisect_left(ordered, queue.popleft()))
        insort(ordered, score)
        queue.append(score)
    return result


def aligned(bars, readings, start, end, *, signal_since=None):
    """Align a completed observation to exactly the next close in its own index series."""
    states = dict(readings)
    rows = []
    for i in range(1, len(bars)):
        day, close = bars[i]
        signal_day = bars[i-1][0]
        state = states.get(signal_day)
        if not start <= day <= end or not state or (signal_since and signal_day < signal_since):
            continue
        previous = states.get(bars[i-2][0], state) if i >= 2 else state
        rows.append((day, close, signal_day, state, previous))
    return rows


def simulate(rows, key, fee):
    nav = peak = 1.0
    worst = 0.0
    exposed = entries = 0
    entry = due = previous_price = None
    finished = []
    for i, (day, price, _, state, prior) in enumerate(rows):
        if previous_price is not None and entry is not None:
            nav *= price / previous_price
            exposed += 1
        if key == "hold":
            target = True
        elif key == "monthly":
            target = bool(state.get(key))
        elif entry is not None:
            target = day < due
        else:
            target = state[key] and not prior.get(key)
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
        previous_price = price
    years = (date.fromisoformat(rows[-1][0]) - date.fromisoformat(rows[0][0])).days / 365.2425
    return {"start": rows[0][0], "end": rows[-1][0], "total_pct": round(100*(nav-1), 3),
            "cagr": round(100*(nav ** (1/years)-1), 3), "drawdown": round(100*worst, 3),
            "exposure": round(100*exposed/(len(rows)-1), 2), "entries": entries, "completed": len(finished),
            "positive": sum(x > 0 for x in finished), "sum_completed_pct": round(sum(finished), 4)}


def table(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def verified_series(code, ref, cutoff):
    symbol = SYMBOLS[code]
    if ref.get("symbol") != symbol or ref.get("kind") != ("价格指数" if code == "SPCLLHCP" else "全收益指数"):
        raise ValueError(f"{code} expected verified index series {symbol}")
    currency = "HKD" if code in ("930914", "HSHYLV") else "CNY"
    if ref.get("currency") != currency:
        raise ValueError(f"{code} unexpected currency")
    bars = []
    for day, raw in ref["bars"]:
        value = float(raw)
        if not math.isfinite(value) or value <= 0 or (bars and day <= bars[-1][0]):
            raise ValueError(f"{symbol} invalid close or ordering on {day}")
        if date.fromisoformat(day).isoformat() <= cutoff:
            bars.append((day, value))
    if len(bars) <= temp.WINDOW+1:
        raise ValueError(f"{symbol} insufficient daily closes")
    return bars


def common_bounds(samples, codes):
    dates = set.intersection(*({row[0] for row in samples[code]} for code in codes))
    if len(dates) < 2:
        raise ValueError("Need two shared observed endpoints for the comparison")
    return min(dates), max(dates)


def summary(records):
    groups = {}
    for r in records:
        groups.setdefault((r["period"], r["fee"], r["rule"]), []).append(r)
    output = []
    for (period, fee, rule), group in groups.items():
        holds = {r["monitor_code"]: r for r in groups[(period, fee, "hold")]}
        output.append({"period": period, "fee": fee, "rule": rule, "indices": len(group),
                       **{f"median_{k}": round(median(r[k] for r in group), 3)
                          for k in ("total_pct", "cagr", "drawdown", "exposure")},
                       **{k: sum(r[k] for r in group) for k in ("entries", "completed", "positive")},
                       "outperformed_hold": sum(r["cagr"] > holds[r["monitor_code"]]["cagr"] for r in group)})
    return output


def compare(refs, cutoff, dest):
    results, diagnostics, sensitivity, counts = [], [], [], []
    absolute, relative = {}, {}
    for code in SYMBOLS:
        ref = refs[code]
        bars = verified_series(code, ref, cutoff)
        live = max(ref["launch"], ref.get("method_since") or ref["launch"])
        signal = factors(bars)
        ranked = relative_temperature(signal)
        absolute[code] = aligned(bars, signal, live, cutoff, signal_since=live)
        relative[code] = aligned(bars, ranked, live, cutoff, signal_since=live)
        if min(len(absolute[code]), len(relative[code])) < 2:
            raise ValueError(f"{code} insufficient common-rule sample for {cutoff}")
        states = [r[3] for r in absolute[code]]
        metadata = {"monitor_code": code, "return_series": ref["symbol"], "kind": ref["kind"], "currency": ref["currency"]}
        diagnostics.append({**metadata, "name": ref["name"], "provider": ref["provider"], "source": ref["url"],
                            "history_start": bars[0][0], "history_end": bars[-1][0], "bars": len(bars),
                            "published": ref["launch"], "method_since": ref.get("method_since") or "",
                            "first_execution": absolute[code][0][0], "first_rank_execution": relative[code][0][0],
                            "eligible_signal_days": len(states), "min_score": min(s["score"] for s in states),
                            "cold_days": sum(s["current"] for s in states), "ultra_days": sum(s["ultra"] for s in states),
                            "rsi_correlation": round(correlation([s["slow"] for s in states], [s["fast"] for s in states]), 4),
                            "sha256": hashlib.sha256(json.dumps(bars, separators=(",", ":")).encode()).hexdigest()})
        # Restrict even the warm-up data to the published/current methodology period.
        strict_bars = [(d, p) for d, p in bars if d >= live]
        strict = factors(strict_bars)
        for scope, observations, keys in (("absolute", strict, ABSOLUTE), ("relative", relative_temperature(strict), ALL)):
            sample = aligned(strict_bars, observations, live, cutoff, signal_since=live)
            for rule in keys:
                m = simulate(sample, rule, .001) if len(sample) >= 2 else {}
                sensitivity.append({**metadata, "warmup": "published_only", "scope": scope, "rule": rule,
                                    "points": len(sample), **{k: m.get(k) for k in
                                      ("start", "end", "cagr", "drawdown", "exposure", "entries", "completed")}})
    # Signal days outside either end never enter simulations. The two endpoints are
    # actual closes on all included markets; within the window each keeps its own calendar.
    common = common_bounds(absolute, SYMBOLS)
    common_rank = common_bounds(relative, SYMBOLS)
    csi_long = ("H30269", "000922", "930914")
    csi_span = common_bounds(relative, csi_long)
    for code in SYMBOLS:
        ref = refs[code]
        meta = {"monitor_code": code, "return_series": ref["symbol"], "kind": ref["kind"], "currency": ref["currency"]}
        periods = [("published", absolute[code], None, ABSOLUTE),
                   ("rank_ready", relative[code], None, ALL),
                   ("common", absolute[code], common, ABSOLUTE),
                   ("common_rank", relative[code], common_rank, ALL)]
        if code in csi_long:
            periods.append(("csi_long", relative[code], csi_span, ALL))
        for period, observations, bounds, rules in periods:
            sample = [r for r in observations if bounds is None or bounds[0] <= r[0] <= bounds[1]]
            for rule in rules:
                for fee in FEES:
                    results.append({**meta, "period": period, "fee": fee, "rule": rule, **simulate(sample, rule, fee)})
                if period in ("rank_ready", "common_rank") and rule in ("current", "ultra", "cold_pct", "ultra_pct"):
                    counts.append({**meta, "period": period, "rule": rule,
                                   "start": sample[0][0], "end": sample[-1][0], "signal_days": len(sample),
                                   "hit_days": sum(bool(r[3][rule]) for r in sample),
                                   "crossings": sum(bool(r[3][rule] and not r[4][rule]) for r in sample)})
    dest.mkdir(parents=True, exist_ok=True)
    for name, records in (("results", results), ("inputs", diagnostics), ("indices", summary(results)),
                          ("basis", sensitivity), ("percentiles", counts)):
        table(dest / f"strategy-{name}.csv", records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--cache", type=Path, help="private cache of reference indices only")
    parser.add_argument("--output", type=Path, default=ROOT / "research")
    args = parser.parse_args()
    if args.cache and args.cache.is_file():
        payload = json.loads(args.cache.read_text(encoding="utf-8"))
        if payload.get("as_of") != args.as_of.isoformat() or set(payload.get("indices", {})) != set(SYMBOLS):
            raise ValueError("cache must match the requested date and six index identities")
        refs = payload["indices"]
    else:
        with ThreadPoolExecutor(max_workers=6) as pool:
            refs = dict(pool.map(lambda code: download(code, args.as_of), SYMBOLS))
        for code, ref in refs.items():
            verified_series(code, ref, args.as_of.isoformat())
        if args.cache:
            args.cache.parent.mkdir(exist_ok=True, parents=True)
            args.cache.write_text(json.dumps({"as_of": args.as_of.isoformat(), "indices": refs}), encoding="utf-8")
    compare(refs, args.as_of.isoformat(), args.output)


if __name__ == "__main__":
    main()
