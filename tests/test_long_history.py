import json
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

import market


class LongHistoryTests(unittest.TestCase):
    def test_adjusted_only_with_volume_and_matching_identity(self):
        days = [datetime(2026, 11, 20, tzinfo=market.CST) + timedelta(days=i) for i in range(4)]
        chart = {
            "meta": {"symbol": "512890.SS", "currency": "CNY", "dataGranularity": "1d"},
            "timestamp": [int(day.timestamp()) for day in days],
            "indicators": {
                "adjclose": [{"adjclose": [0.5, 0.52, 0.53, 0.54]}],
                "quote": [{"close": [1, 1.04, 0.53, 0.54], "volume": [0, 600, 800, 500]}],
            },
        }
        def response():
            return json.dumps({"chart": {"result": [chart]}})
        with patch.object(market, "read", side_effect=lambda *_a, **_k: response()) as fetch:
            self.assertEqual(market.adjusted_history("sh512890", days[2].date()),
                             [(days[1].date().isoformat(), 0.52), (days[2].date().isoformat(), 0.53)])
            self.assertEqual(fetch.call_count, 1)
            chart["meta"]["symbol"] = "OTHER.SS"
            self.assertEqual(market.adjusted_history("sh512890", days[2].date()), [])
            chart["meta"]["symbol"] = "512890.SS"
            chart["indicators"].pop("adjclose")
            self.assertEqual(market.adjusted_history("sh512890", days[2].date()), [])

    def test_fund_short_and_long_series_share_one_download_and_cache(self):
        start = datetime(2025, 1, 1, tzinfo=market.CST)
        trend = [{"x": int((start + timedelta(days=i)).timestamp() * 1000),
                  "y": 1.35 if i < 100 else 1.12, "equityReturn": 0.7} for i in range(150)]
        response = 'var fS_code = "012708";var Data_netWorthTrend = ' + json.dumps(trend) + ';'
        item = {"code": "012708", "type": "场外基金", "index": "931446", "rsi_max": 35}
        with patch.object(market, "read", return_value=response) as fetch, \
                patch.object(market, "history", return_value=[]), \
                patch.object(market, "estimate", return_value=None), \
                patch.object(market, "reported_navs", return_value={}), \
                patch.object(market, "eastmoney_quotes", return_value={}), \
                patch.object(market, "tencent_quotes", return_value={}):
            data = market.collect([item], date(2026, 1, 1))
            self.assertEqual(fetch.call_count, 1)
            full, recent = data["long:012708"], data["history:012708"]
            self.assertEqual((len(full), len(recent)), (150, 110))
            self.assertAlmostEqual(full[100][1] / full[99][1], 1.007)
            self.assertAlmostEqual(recent[0][1], 1.007)
            self.assertAlmostEqual(recent[-1][1] / recent[0][1], full[-1][1] / full[-110][1])
            reused = market.collect([item], date(2026, 1, 1), references={"long:012708": full})
            self.assertEqual(reused["history:012708"], recent)
            self.assertEqual(fetch.call_count, 1)
