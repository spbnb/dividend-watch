import unittest

import backtest


class BacktestTests(unittest.TestCase):
    def test_after_signal_close_holidays_and_nonoverlap(self):
        prices = [("2023-01-03", 500), ("2023-01-04", 100), ("2023-01-05", 90),
                  ("2024-01-03", 130), ("2024-01-04", 110)]
        signal = [(d, {"current": v}) for d, v in [
            ("2022-12-29", False), ("2023-01-03", True), ("2023-01-04", False),
            ("2023-01-05", True), ("2024-01-02", True), ("2024-01-03", True)]]
        trades = backtest.aligned(prices, signal, "2023", "2025")
        result = backtest.simulate(trades, "current", .001)
        self.assertEqual((result["entries"], result["completed"]), (1, 1))
        self.assertAlmostEqual(result["sum_completed_pct"], 100*(1.1*.999**2-1), places=4)
        self.assertAlmostEqual(result["exposure"], 75)

    def test_both_sides_charged_and_open_trade_not_counted_as_mature(self):
        bars = [(d, p, d, {"current": i == 0}, {}) for i, (d, p) in enumerate([
            ("2025-01-01", 100), ("2025-02-01", 90), ("2025-03-01", 100)])]
        result = backtest.simulate(bars, "current", .01)
        self.assertEqual((result["entries"], result["completed"], result["exposure"]), (1, 0, 100))
        self.assertAlmostEqual(result["drawdown"], -10.9)
        self.assertLess(result["cagr"], 0)
