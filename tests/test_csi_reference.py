import json
import unittest
from datetime import date
from unittest.mock import patch

import csi_reference as ref


TODAY = date(2026, 11, 26)


class IndexReferenceTests(unittest.TestCase):
    def test_csi_uses_requested_symbol_and_only_valid_completed_dates(self):
        def row(day, close, symbol="H20269"):
            return {"indexCode": symbol, "tradeDate": day, "close": close}
        payload = {"success": True, "data": [
            row("20261126", 25389.77), row("20261125", 25201.56),
            row("20261124", "NaN"), row("20261123", 0), row("20261121", 77),
            row("20261127", 25400), row("20261124", 100, "H30269"),
            row("20051229", 1000), row("20051230", 1000),
        ]}
        with patch.object(ref, "_download", return_value=json.dumps(payload).encode()) as get:
            bars = ref.csi_bars("H20269", "2005-12-30", TODAY)
        self.assertEqual(bars, [("2005-12-30", 1000), ("2026-11-25", 25201.56), ("2026-11-26", 25389.77)])
        self.assertIn("indexCode=H20269&startDate=20051230", get.call_args.args[0])

    def test_cni_uses_named_close_and_validates_index_identity(self):
        content = {"code": 200, "data": {"indexCode": "480092", "item": ["close", "timestamp", "open"],
                                          "data": [[48.1, "2026-11-26", 47], [46, "2026-11-25", 45.3]]}}
        with patch.object(ref, "_download", return_value=json.dumps(content).encode()):
            self.assertEqual(ref.cni_bars("480092", "2012-12-31", TODAY),
                             [("2026-11-25", 46), ("2026-11-26", 48.1)])
            self.assertEqual(ref.cni_bars("980092", "2012-12-31", TODAY), [])

    def test_dated_yield_uses_calculation_shares_preserving_zero_and_percent_units(self):
        rows = iter([
            ["日期Date", "股息率2（计算用股本）D/P2", "股息率1（总股本）D/P1", "指数代码Index Code"],
            ["20261126", 4.68, 4.39, "H30269"], ["20261125", 0, 3.5, "H30269"],
            ["20261124", None, 4, "H30269"], ["20261124", 5, 5, "000922"],
            ["20261127", 4.69, 4.6, "H30269"],
        ])
        self.assertEqual(ref._yield_rows(rows, "H30269", TODAY), [("2026-11-25", 0), ("2026-11-26", 4.68)])

    def test_price_and_yield_series_can_fail_independently(self):
        with patch.object(ref, "csi_bars", side_effect=TimeoutError), \
                patch.object(ref, "csi_yields", return_value=[("2026-11-25", 6.45)]):
            reference = ref.get_reference("sh513530", TODAY)
        self.assertEqual(reference["bars"], [])
        self.assertEqual((reference["symbol"], reference["market"], reference["currency"]), ("H20914", "HK", "HKD"))
        self.assertEqual((reference["yield_at"], reference["yield_pct"]), ("2026-11-25", 6.45))
        with patch.object(ref, "csi_bars", return_value=[("2026-11-26", 999)]), \
                patch.object(ref, "csi_yields", side_effect=ValueError):
            reference = ref.get_reference("012708", TODAY)
        self.assertEqual(reference["bars"], [("2026-11-26", 999)])
        self.assertIsNone(reference["yield_pct"])
        self.assertIsNone(reference["yield_at"])

    def test_cni_discloses_method_change_and_declines_unavailable_yield(self):
        with patch.object(ref, "cni_bars", return_value=[]), patch.object(ref, "csi_yields") as wrong_source:
            result = ref.get_reference("sz159201", TODAY)
        self.assertEqual((result["symbol"], result["kind"]), ("480092", "全收益指数"))
        self.assertIn("2024-08-15", result["note"])
        self.assertIsNone(result["yield_pct"])
        wrong_source.assert_not_called()

    def test_unknown_fund_does_not_make_requests(self):
        with patch.object(ref, "_download") as get:
            self.assertEqual(ref.get_reference("sh600000", TODAY), {})
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
