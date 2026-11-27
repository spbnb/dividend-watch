from datetime import date, datetime, timedelta
import unittest

import local_scan
import market
import monitor
import notify
import temperature as t
from test_monitor import ITEMS, NOW, fixture, quote


class TemperatureTests(unittest.TestCase):
    def test_rolling_rank_uses_only_earlier_days_and_averages_ties(self):
        start = date(2020, 1, 1)
        bars = [((start + timedelta(days=i)).isoformat(), 17) for i in range(251)]
        before = t.historical_series(bars)
        self.assertIsNone(before[249][2])
        self.assertEqual(before[250][2], 50)
        extended = t.historical_series(bars + [("2022-01-01", 16), ("2022-01-02", 18)])
        self.assertEqual(extended[:251], before)
        self.assertEqual([r[2] for r in extended[-2:]], [0, 100])
        self.assertEqual([t.band(v) for v in (10, 10.2, 25, 25.2, 75, 90, 90.2)], [
            "超低温", "低温", "低温", "中温", "中温", "高温", "超高温"])

    def test_forward_returns_use_next_close_anniversary_and_only_mature_samples(self):
        series = [("2019-02-26", 900, 5), ("2019-02-27", 100, None),
                  ("2020-02-28", 110, None), ("2021-03-01", 90, None), ("2022-02-28", 100, None),
                  ("2022-03-01", 101, 20)]
        result = t.holding_stats(series)
        self.assertEqual([(c["n"], c["mean"], c["win"]) for c in result[0]], [(1,10,100),(1,-10,0),(1,0,0)])
        self.assertIsNone(result[1][0]["win"])
        self.assertEqual(t.anniversary(date(2024,2,29),1), date(2025,2,28))

    def test_two_temperature_tiers_escalate_without_leaking_observe_only_signals(self):
        item = ITEMS["sh512890"]
        dates = [(NOW.date() - timedelta(days=i)).isoformat() for i in range(250, 0, -1)]
        bars = list(zip(dates, [1 + i/1000 for i in range(250)]))
        code = item["code"]
        data = fixture(code, quote(1.04, prev_close=bars[-1][1]))
        data["long:" + code] = bars + [(NOW.date().isoformat(), 10)]  # Unfinished daily bar.
        data["history:sh000001"] = [(dates[-1], 10), (NOW.date().isoformat(), 10)]
        def check(level, sent):
            snap = monitor.build([item], data, NOW)
            local = local_scan.live_report(snap, NOW)["events"]
            key = code + "/" + level
            self.assertEqual(local[0]["keys"], [key])
            title, lines, keys = notify.delivery(snap, NOW, "scheduled", sent)
            self.assertIn(key, keys)
            self.assertIn("温", title)
            self.assertNotIn("价格到达", lines[0])
            self.assertIn("盘中暂估", lines[0])
            self.assertIsNone(notify.delivery(snap, NOW, "scheduled", keys))
            return keys
        sent = check("cold", set())
        data["quotes"][code]["price"] = 1.005
        sent = check("cold_second", sent)
        self.assertIn(code+"/cold", sent)
        data["quotes"][code]["at"] = (NOW - timedelta(days=1)).isoformat()
        snap = monitor.build([item], data, NOW)
        self.assertEqual(local_scan.live_report(snap, NOW)["events"], [])
        self.assertIsNone(notify.delivery(snap, NOW, "scheduled", set()))

    def test_fund_live_uses_estimated_return_and_requires_matching_previous_day(self):
        item = ITEMS["012708"]
        bars = [((NOW.date()-timedelta(days=i)).isoformat(), 100+i) for i in range(250,0,-1)]
        q = quote(1.15, pct=3)
        expected = t.rank(bars[-1][1]*1.03, sorted(v for _, v in bars))
        self.assertEqual(t.reading(item, bars, q, NOW, bars[-1][0], True), (expected, q["at"], True))
        self.assertFalse(t.reading(item, bars, q, NOW, None, True)[2])
        confirmed = bars + [(NOW.date().isoformat(), 106)]
        self.assertEqual(t.closed_history({"long:012708":confirmed}, item, NOW), bars)
        close = datetime.combine(NOW.date(), datetime.min.time(), market.CST).replace(hour=19)
        self.assertEqual(t.closed_history({"long:012708":confirmed}, item, close), confirmed)
        self.assertEqual(t.reading(item, confirmed, q, close, bars[-1][0], False)[1:], (NOW.date().isoformat(), False))
