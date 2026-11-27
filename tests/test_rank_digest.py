import tempfile
import unittest
from pathlib import Path

import notify
from test_monitor import NOW


def snapshot(stamp, period):
    today = stamp.date().isoformat()
    day = today if period == "postmarket" else "2026-11-23"

    def row(code, temperature, band, observed):
        return {"name": code, "code": code, "temperature": temperature, "temperature_band": band,
                "temperature_at": observed, "temperature_live": False, "rank_day_pct": 0}

    return {"generated": stamp.isoformat(), "cn_trading_day": True, "cn_previous_close": "2026-11-23",
            "benchmarks": {"CN": {"at": today + "T15:00:00+08:00"}},
            "rows": [row("甲", 0, "超低温", day), row("乙", 23, "低温", day), row("丙", 99, "超高温", "2026-11-20")]}


class DigestTests(unittest.TestCase):
    def test_pre_and_post_only_rank_matching_closes_and_keep_independent_receipts(self):
        with tempfile.TemporaryDirectory() as td:
            for phase, mode, hour, minute in (("premarket", "scheduled", 8, 0), ("postmarket", "postmarket", 16, 30)):
                stamp = NOW.replace(hour=hour, minute=minute)
                snap = snapshot(stamp, phase)
                inbox = []
                channels = {c: (lambda title, lines, c=c: inbox.append((c, lines))) for c in ("ntfy", "wxpusher")}
                folder = Path(td)
                def run(action):
                    return notify.reserved_delivery(snap, mode, folder, channels, action, lambda: stamp)
                self.assertEqual(run("reserve"), (True, []))
                self.assertEqual(run("deliver"), (False, []))
                self.assertCountEqual([c for c, _ in inbox], channels)
                msg = "\n".join(inbox[0][1])
                for text in ("已更新 2/3", "最热　乙 23.0°", "最冷　甲 0.0°", "低温 1 只 · 超低温 1 只", "+0.00%", "待更新\n丙"):
                    self.assertIn(text, msg)
                self.assertNotIn("99.0°", msg)
                self.assertEqual(run("reserve"), (False, []))

    def test_calendar_missing_holiday_weekend_and_delays(self):
        am = NOW.replace(hour=8, minute=0)
        snap = snapshot(am, "premarket")
        for closed in (None, False):
            self.assertIsNone(notify.delivery(dict(snap, cn_trading_day=closed), am, "scheduled", set()))
        for stamp in (am.replace(hour=7), am.replace(hour=9, minute=30)):
            self.assertIsNone(notify.rank_digest(snap, stamp, "premarket", set()))
        saturday = am.replace(day=28)
        self.assertIsNone(notify.delivery(snapshot(saturday, "premarket"), saturday, "scheduled", set()))
        pm = NOW.replace(hour=16, minute=30)
        snap = snapshot(pm, "postmarket")
        self.assertIsNone(notify.delivery(dict(snap, cn_trading_day=False), pm, "postmarket", set()))
        self.assertIsNotNone(notify.delivery(dict(snap, cn_trading_day=None), pm, "postmarket", set()))
        self.assertIsNone(notify.delivery(dict(snap, cn_trading_day=None, benchmarks={}), pm, "postmarket", set()))
        for row in snap["rows"]:
            row["temperature_at"] = None
        self.assertIsNone(notify.delivery(snap, pm, "postmarket", set()))
        self.assertIn("已更新 0/3", notify.delivery(snap, pm.replace(hour=17), "postmarket", set())[1][0])
        self.assertIsNone(notify.delivery(snap, pm.replace(day=25), "postmarket", set()))
