import json
import unittest
from datetime import date, datetime
from unittest.mock import patch

import global_reference as reference


class GlobalReferenceTests(unittest.TestCase):
    today = date(2026, 11, 26)

    def test_southern_uses_only_identity_checked_benchmark_and_keeps_zero(self):
        info = {"fund_info": {"fundCode": "515450", "yjbjjz": reference.SP_NAME + "收益率"}}
        data = {"brList": [{"fdate": day, "upratio": pct} for day, pct in (
            ("20200123", -3.77), ("20200225", ""), ("20261123", 0), ("20261124", "-2.70"),
            ("20261125", "NaN"), ("20261126", "-100"), ("20261127", 10))],
            "thisfundlist": [{"fdate": "20261125", "upratio": 9.2}], "jzlist": []}
        with patch.object(reference, "_southern_data", side_effect=[info, data]) as fetch:
            result = reference.get_reference("sh515450", self.today)
        self.assertEqual(result["bars"], [("2026-11-23", 100), ("2026-11-24", 97.3)])
        self.assertEqual((result["kind"], result["symbol"]), ("价格指数", "SPCLLHCP"))
        self.assertEqual(fetch.call_args_list[-1].kwargs, {"months": 7})
        info["fund_info"]["yjbjjz"] = "95%×指数收益率+5%×现金收益率"
        with patch.object(reference, "_southern_data", return_value=info) as fetch:
            self.assertEqual(reference._southern_bars(self.today), [])
            self.assertEqual(fetch.call_count, 1)

    def test_hsi_rejects_price_index_and_uses_gross_return_even_if_report_misses(self):
        payload = {"code": "0", "data": [{"indexCode": "02033.00", "indexName": "Price",
                                           "valueHistory": [{"date": "2026-11-24", "close": 4000}]}]}
        code = "12033.00"
        name = "Hang Seng SCHK High Dividend Low Volatility Index Total Return Index"
        payload["data"].append({"indexCode": code, "indexName": name, "valueHistory": [
            {"date": "2026-11-26", "close": "10259.94"}, {"date": "2026-11-27", "close": "10313.57"},
            {"date": "2026-11-25", "close": ""}]})
        with patch.object(reference, "_fetch", return_value=json.dumps(payload).encode()), \
                patch.object(reference, "_hang_seng_yield", side_effect=OSError):
            result = reference.get_reference("sz159545", self.today)
        self.assertEqual(result["bars"], [("2026-11-26", 10259.94)])
        self.assertEqual((result["currency"], result["kind"], result["yield_at"]), ("HKD", "全收益指数", None))
        self.assertNotIn("quote_key", result)

    def test_factsheet_yield_uses_correct_row_and_printed_as_of(self):
        content = ("INDEX FUNDAMENTALS\nIndex   Dividend Yield (%)  PE Ratio (Times)\n"
                   "HSI   3.60   10.20\nHKD    HSHYLV    6.75    6.87    13.66\n"
                   "CORRELATION\nAll data as at 30 Oct 2026")
        self.assertEqual(reference._factsheet_yield(content, self.today), ("2026-10-30", 6.75))
        self.assertIsNone(reference._factsheet_yield(content, date(2026, 10, 29)))
        self.assertIsNone(reference._factsheet_yield(content.replace("HSHYLV    6.75", "HSI  8.25"), self.today))

    def test_unknown_fund_and_yield_only(self):
        with patch.object(reference, "_hang_seng_bars", side_effect=OSError), \
                patch.object(reference, "_hang_seng_yield", return_value=("2026-10-30", 6.75)):
            self.assertEqual(reference.get_reference("sh513530", self.today), {})
            result = reference.get_reference("sz159545", self.today)
            self.assertEqual((result["bars"], result["yield_pct"], result["yield_at"]), ([], 6.75, "2026-10-30"))

    def test_archived_sp_yield_is_available_only_after_publication(self):
        with patch.object(reference, "_southern_bars", return_value=[]):
            self.assertIsNone(reference.get_reference("sh515450", date(2026, 11, 18))["yield_pct"])
            published = reference.get_reference("sh515450", date(2026, 11, 19))
        self.assertEqual((published["yield_pct"], published["yield_at"]), (4.4, "2026-11-18"))
        self.assertEqual(published["yield_history"], [])
        self.assertIn("单次披露", published["yield_method"])

    def test_ft_price_index_identity_date_and_previous_close(self):
        page = ('''<div class="mod-tearsheet-overview__header__symbol"><span>SPCLLHCP:IOM</span></div>'''
                "<div>Price (CNY) <span>3,969.05</span> Today's Change -39.19 / -0.98%</div>"
                '<div class="mod-disclaimer">Data delayed at least 10 minutes, as of Nov 27 2026 07:52 GMT.</div>')
        cutoff = date(2026, 11, 27)
        with patch.object(reference, "_fetch", return_value=page.encode()):
            quote = reference.sp_price_quote(cutoff)
            self.assertEqual((quote["price"], quote["pct"], quote["prev_close"]), (3969.05, -0.98, 4008.24))
            self.assertEqual(quote["at"], "2026-11-27T15:52:00+08:00")
            self.assertIsNone(reference._ft_sp_quote(self.today))
        for wrong in [page.replace("SPCLLHCP", "SPCLLHCT"), page.replace("CNY", "USD")]:
            with patch.object(reference, "_fetch", return_value=wrong.encode()):
                self.assertIsNone(reference._ft_sp_quote(cutoff))
        with patch.object(reference, "_fetch", return_value=page.replace("-39.19", "-14.78").encode()):
            self.assertIsNone(reference._ft_sp_quote(cutoff)["prev_close"])

    def test_yahoo_fallback_keeps_real_level_only(self):
        cutoff = date(2026, 11, 27)
        stamp = int(datetime(2026, 11, 27, 15, 52, 47, tzinfo=reference.CST).timestamp())
        meta = {"symbol": "^SPCLLHCP", "currency": "CNY", "instrumentType": "INDEX", "exchangeName": "SNP",
                "regularMarketPrice": 3969.05, "regularMarketTime": stamp, "chartPreviousClose": 3983.83,
                "regularMarketChangePercent": -0.371}
        def payload():
            return json.dumps({"chart": {"result": [{"meta": meta}]}}).encode()
        with patch.object(reference, "_ft_sp_quote", side_effect=OSError), \
                patch.object(reference, "_fetch", side_effect=lambda *_: payload()):
            result = reference.sp_price_quote(cutoff)
            self.assertEqual((result["price"], result["at"]), (3969.05, "2026-11-27T15:52:47+08:00"))
            self.assertEqual((result["prev_close"], result["pct"]), (None, None))
            self.assertIsNone(reference.sp_price_quote(self.today))
            meta["symbol"] = "^SPCLLHCT"
            self.assertIsNone(reference.sp_price_quote(cutoff))

    def test_price_and_total_return_bars_have_separate_hsi_identities(self):
        payload = {"code": "0", "data": [
            {"indexCode": "12033.00", "indexName": "Hang Seng SCHK High Dividend Low Volatility Index Total Return Index",
             "valueHistory": [{"date": "2026-11-26", "close": 10259.94}]},
            {"indexCode": "02033.00", "indexName": "Hang Seng SCHK High Dividend Low Volatility Index",
             "valueHistory": [{"date": "2026-11-26", "close": 4114.59}]}]}
        with patch.object(reference, "_fetch", return_value=json.dumps(payload).encode()), \
                patch.object(reference, "_hang_seng_yield", return_value=None):
            self.assertEqual(reference.hang_seng_price_bars(self.today), [("2026-11-26", 4114.59)])
            self.assertEqual(reference.get_reference("HSHYLV", self.today)["bars"], [("2026-11-26", 10259.94)])
