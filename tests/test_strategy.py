import unittest

import backtest


class BacktestTests(unittest.TestCase):
    def test_temperature_percentile_uses_past_observations_and_averages_ties(self):
        rows = [(str(i), {"score": v}) for i, v in enumerate([10, 30, 30, 50, 15, 15])]
        ranked = backtest.relative_temperature(rows, window=4)
        self.assertEqual([day for day, _ in ranked], ["4", "5"])
        self.assertEqual([(s["cold_pct"], s["ultra_pct"]) for _, s in ranked], [(True, False), (True, False)])

    def test_after_index_signal_close_and_nonoverlap(self):
        prices = [("2022-12-30", 600), ("2023-01-03", 500), ("2023-01-04", 100), ("2023-01-05", 90),
                  ("2024-01-03", 130), ("2024-01-04", 110)]
        signal = [(d, {"current": v}) for d, v in [
            ("2022-12-30", False), ("2023-01-03", True), ("2023-01-04", False),
            ("2023-01-05", True), ("2024-01-03", True)]]
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

    def test_regime_change_excludes_older_signals_and_uses_next_own_close(self):
        bars = [("2024-08-14", 100), ("2024-08-15", 200), ("2024-08-16", 220)]
        signals = [(bars[0][0], {"v": False}), (bars[1][0], {"v": True})]
        rows = backtest.aligned(bars, signals, "2024-08-15", "2024-08-16", signal_since="2024-08-15")
        self.assertEqual([(r[0], r[1], r[2]) for r in rows], [("2024-08-16", 220, "2024-08-15")])

    def test_common_endpoints_are_observations_of_every_included_market(self):
        samples = {"cn": [(d,) for d in ("2025-01-02", "2025-01-03", "2025-01-06")],
                   "hk": [(d,) for d in ("2025-01-03", "2025-01-06", "2025-01-07")]}
        self.assertEqual(backtest.common_bounds(samples, samples), ("2025-01-03", "2025-01-06"))

    def test_price_only_series_cannot_replace_a_total_return_index(self):
        with self.assertRaisesRegex(ValueError, "H00922"):
            backtest.verified_series("000922", {"symbol": "000922", "kind": "价格指数"}, "2026-01-01")
        with self.assertRaisesRegex(ValueError, "currency"):
            backtest.verified_series("930914", {"symbol": "H20914", "kind": "全收益指数",
                                                "currency": "CNY"}, "2026-01-01")
