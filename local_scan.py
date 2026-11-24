"""One live polling cycle for the local Vibe AStock notification service."""

import argparse
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from market import CST, collect
from monitor import ROOT, build, session_end
from notify import event_keys, line_for


def collect_local(items, now, cache_path):
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(cache["checked"])
        references = cache["references"] if checked.date() == now.date() else {}
        reusable = timedelta(0) <= now - checked < timedelta(minutes=15)
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        references, reusable = {}, False
    if reusable and references:
        return collect(items, now.date(), references=references)
    data = collect(items, now.date())
    # Keep same-day reference data if an occasional history request fails.
    references.update({k: v for k, v in data.items() if k.startswith(("history:", "nav:", "cash:")) and v})
    data.update(references)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_path.parent, delete=False) as f:
        json.dump({"checked": now.isoformat(), "references": references}, f)
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
        table.append([f"{row['name']} {row['code']}", value, f"{row['first']:.{digits}f}", row["label"],
                      (row["at"] or "—")[:19].replace("T", " ")])
        keys = event_keys(row, now, benchmarks)
        if not keys:
            continue
        market = row["market"]
        stamps = [row["signal_at"], benchmarks[market]["at"]]
        if "premium_max" in row:
            stamps.append(benchmarks["HK"]["at"])
        expires = min([session_end(now, market)] +
                      [datetime.fromisoformat(t) + timedelta(minutes=25) for t in stamps])
        covers = [row["code"] + "/first"] if row["code"] + "/second" in keys else []
        events.append({"keys": sorted(keys), "covers": covers, "expires": expires.isoformat(),
                       "line": line_for(row) + "｜行情 " + row["signal_at"][11:19]})
    return {"day": now.date().isoformat(), "checked": now.isoformat(timespec="seconds"),
            "groups": snapshot["groups"], "total": len(snapshot["rows"]),
            "priced": sum(r["price"] is not None for r in snapshot["rows"]),
            "open": [k for k, active in snapshot["session"].items() if active], "events": events, "rows": table}


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
