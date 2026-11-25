"""One live polling cycle for the local Vibe AStock notification service."""

import argparse
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from alert_format import alert_card
from market import CST, collect
from monitor import ROOT, build, session_end
from notify import covered_keys, event_keys, signal_time


def collect_local(items, now, cache_path):
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(cache["checked"])
        references = cache["references"] if checked.date() == now.date() else {}
        reusable = timedelta(0) <= now - checked < timedelta(minutes=15)
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        references, reusable = {}, False
    using_cache = bool(reusable and references)
    data = collect(items, now.date(), references=references) if using_cache else collect(items, now.date())
    # Keep same-day reference data if an occasional history request fails.
    observed = {k: v for k, v in data.items() if k.startswith(("history:", "nav:", "cash:")) and v}
    if using_cache and observed.keys() <= references.keys():
        return data
    references.update(observed)
    data.update(references)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_path.parent, delete=False) as f:
        json.dump({"checked": (checked if using_cache else now).isoformat(), "references": references}, f)
    try:
        os.replace(f.name, cache_path)
    finally:
        Path(f.name).unlink(missing_ok=True)
    return data


def live_report(snapshot, now):
    benchmarks = snapshot["benchmarks"]
    events, table = [], []
    for row in snapshot["rows"]:
        digits = 4 if row.get("index") else 3 if row["type"] == "ETF" else 2
        value = f"{row['price']:.{digits}f} {row['currency']}" if row["price"] is not None else "—"
        if row.get("index") and row["price"] and row["source"] and "已公布" not in row["source"]:
            value += "（估）"
        targets = f"{row['first']:.{digits}f}" + (f" / {row['second']:.{digits}f}" if row.get("second") else "")
        indicators = []
        for label, field, first, second, unit in (
                ("RSI(14)", "rsi", "rsi_max", "rsi_second_max", ""),
                ("RSI(7)", "rsi7", "rsi7_max", "rsi7_second_max", ""),
                (row.get("index", "") + " 日涨跌", "index_pct", "index_drop", "index_drop_second", "%")):
            if row.get(first) is not None:
                reading = "—" if row[field] is None else f"{row[field]:.2f}{unit}"
                limits = f"{row[first]:g}{unit}" + (f" / {row[second]:g}{unit}" if row.get(second) is not None else "")
                indicators.append(f"{label} {reading}（≤ {limits}）")
        if row.get("show_premium"):
            spread = "—" if row["premium"] is None else f"{row['premium']:+.2f}%"
            indicators.append(f"折溢价 {spread}（仅观察）")
        table.append([f"{row['name']} {row['code']}", value, targets, "；".join(indicators) or "—", row["label"],
                      (row["at"] or "—")[:19].replace("T", " ")])
        keys = event_keys(row, now, benchmarks)
        if not keys:
            continue
        market = row["market"]
        event_stamps = [signal_time(row, k.split("/", 1)[1]) for k in keys]
        stamps = [*event_stamps, benchmarks[market]["at"]]
        if "premium_max" in row:
            stamps.append(benchmarks["HK"]["at"])
        expires = min([session_end(now, market)] +
                      [datetime.fromisoformat(t) + timedelta(minutes=25) for t in stamps])
        events.append({"keys": sorted(keys), "covers": sorted(covered_keys(keys)), "expires": expires.isoformat(),
                       "row": row, "line": alert_card(row, {k.split("/", 1)[1] for k in keys}, now)})
    return {"day": now.date().isoformat(), "checked": now.isoformat(timespec="seconds"),
            "groups": snapshot["groups"], "total": len(snapshot["rows"]),
            "priced": sum(r["price"] is not None for r in snapshot["rows"]),
            "open": [k for k, active in snapshot["session"].items() if active], "events": events,
            "columns": ["标的", "现价 / 净值", "价格首档 / 二档", "指标现值（首档 / 二档）", "状态", "行情时间（北京时间）"],
            "rows": table}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    items = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
    with contextlib.redirect_stdout(sys.stderr):
        data = collect_local(items, datetime.now(CST), args.cache)
    now = datetime.now(CST)
    print(json.dumps(live_report(build(items, data, now), now), ensure_ascii=True))


if __name__ == "__main__":
    main()
