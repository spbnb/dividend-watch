import unittest
from datetime import timedelta

import monitor
import temperature
from test_monitor import ITEMS, NOW, fixture, quote


class IndexObservationTests(unittest.TestCase):
    def setUp(self):
        self.code = "H30269"
        self.days = [(NOW.date()-timedelta(days=i)).isoformat() for i in range(255, 0, -1)]
        self.bars = [(d, 1255-i) for i, d in enumerate(self.days)]
        self.data = fixture(self.code, quote(95, prev_close=100))
        self.data["long:sh512890"] = [(d, 1 + i/250) for i, d in enumerate(self.days)]
        self.ref = {"kind": "全收益指数", "symbol": "H20269", "currency": "CNY", "provider": "中证指数",
                    "bars": self.bars + [(NOW.date().isoformat(), 99999)], "yield_pct": 4.68}
        self.data["benchmark:"+self.code] = self.ref
        self.data["history:sh000001"] = [(self.days[-1], 3000), (NOW.date().isoformat(), 3000)]
        self.data["quotes"]["sh512890"] = quote(2, prev_close=1.9)

    def row(self):
        return monitor.evaluate(ITEMS[self.code], self.data, NOW)

    def test_true_price_index_points_and_total_return_indicators(self):
        row = self.row()
        self.assertEqual((row["price"], row["rsi"], row["rsi7"], row["temperature"]), (95, 0, 0, 0))
        self.assertTrue(row["temperature_live"])
        self.assertFalse(row["reading_fallback"])
        self.assertAlmostEqual(row["drawdown"], 100*(self.bars[-1][1]*.95/self.bars[-59][1]-1))
        self.assertEqual(row["rsi_at"], NOW.isoformat())
        self.assertIsNone(row["yield_pct"])
        self.assertEqual(row["reference_yield"], 4.68)
        self.assertNotIn("first", row["signals"])
        page = temperature.page_data(row, self.bars, "official")
        self.assertTrue(all(p is None for _, p, _ in page["series"]))
        self.assertIn("含分红再投资", page["stats_note"])
        self.assertNotIn("_index_closes", row)

    def test_stale_or_incompatible_tick_keeps_only_last_closed_index_reading(self):
        for bad_tick in ("currency", "stale", "unverified prior"):
            with self.subTest(bad_tick):
                self.setUp()
                if bad_tick == "currency":
                    self.ref["currency"] = "HKD"
                elif bad_tick == "stale":
                    self.data["quotes"][self.code] = quote(95, at=NOW-timedelta(minutes=35), prev_close=100)
                else:
                    self.ref["bars"] = self.bars[:-1]
                row = self.row()
                self.assertFalse(row["reading_fallback"])
                self.assertTrue(row["reference_is_index"])
                self.assertEqual(row["temperature"], 0)
                self.assertFalse(row["temperature_live"])
                self.assertEqual(row["signals"], [])
                self.assertIn("指数收盘", row["reading_basis"])

    def test_missing_history_and_index_yield_are_independent(self):
        self.ref["bars"] = []
        self.ref["yield_pct"] = 0.0
        row = self.row()
        self.assertTrue(row["reference_is_index"])
        self.assertIsNone(row["temperature"])
        self.assertIsNone(row["rsi"])
        self.assertEqual(row["reference_yield"], 0.0)
        self.assertIsNone(row["yield_pct"])

    def test_provisional_index_close_never_replaces_published_history(self):
        self.ref["bars"] = self.bars
        closed = NOW.replace(hour=15, minute=0)
        self.data["quotes"][self.code] = quote(95, at=closed, prev_close=100)
        row = monitor.evaluate(ITEMS[self.code], self.data, closed + timedelta(minutes=45))
        self.assertFalse(row["temperature_live"])
        self.assertFalse(row["reading_fallback"])
        self.assertIn("收盘涨跌暂估", row["reading_basis"])
        self.assertEqual(row["temperature_at"], closed.date().isoformat())
        self.assertEqual(self.bars[-1][0], self.days[-1])
        self.assertNotIn("cold_second", row["signals"])
