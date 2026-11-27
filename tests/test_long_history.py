import json
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from unittest.mock import patch

import market


class LongHistoryTests(unittest.TestCase):
    def test_direct_index_collection_has_no_fund_prices_or_distributions(self):
        day = date(2026, 11, 27)
        indexes = list(market.INDEX_BENCHMARKS)
        items = [{"code": code, "type": "指数"} for code in indexes]
        tick = {"price": 3969.05, "at": "2026-11-27T15:52:00+08:00", "prev_close": 4008.24, "pct": -0.98}
        ref = {"bars": [("2026-11-26", 143.47)], "kind": "价格指数"}
        with ExitStack() as stack:
            def mock(obj, name, **kw):
                return stack.enter_context(patch.object(obj, name, **kw))
            tencent = mock(market, "tencent_quotes", return_value={"hkHSI": {**tick, "volume": 0}})
            east = mock(market, "eastmoney_quotes", side_effect=lambda codes: {
                code: {**tick, "price": 5001.99, "volume": 0, "iopv": 0} for code in indexes if code in codes})
            history = mock(market, "history", return_value=[])
            stock = mock(market, "adjusted_history")
            cash = mock(market, "fund_distributions")
            stock_cash = mock(market, "dividends")
            mock(market, "cn_trading_day", return_value=True)
            bench = mock(market, "benchmark_reference", return_value=ref)
            sp = stack.enter_context(patch("global_reference.sp_price_quote", return_value=tick.copy()))
            result = market.collect(items, day)
            cached = market.collect(items, day, references=result)
        self.assertEqual(set(tencent.call_args.args[0]), set(market.BENCHMARKS.values()))
        self.assertEqual(set(east.call_args.args[0]), set(indexes) - {"SPCLLHCP"} | set(market.BENCHMARKS.values()))
        self.assertEqual({call.args[0] for call in history.call_args_list}, set(market.BENCHMARKS.values()))
        stock.assert_not_called()
        cash.assert_not_called()
        stock_cash.assert_not_called()
        self.assertEqual(bench.call_count, len(indexes))
        self.assertEqual(sp.call_count, 2)
        self.assertEqual(set(cached["quotes"]), set(indexes) | {"hkHSI"})
        self.assertNotIn("volume", cached["quotes"]["hkHSI"])
        self.assertEqual(result["quotes"]["SPCLLHCP"]["price"], 3969.05)
        self.assertEqual(result["benchmark:SPCLLHCP"]["bars"][-1][1], 143.47)
        for code in indexes:
            quote = result["quotes"][code]
            self.assertEqual((quote["symbol"], quote["kind"], quote["quote_type"]), (code, "价格指数", "quote"))
            self.assertEqual(quote["currency"], "HKD" if code in {"930914", "HSHYLV"} else "CNY")
            self.assertNotIn("iopv", quote)
            self.assertNotIn("volume", quote)
        self.assertFalse(any(k.startswith(("cash:", "long:")) for k in result))

    def test_zero_stock_volume_still_indicates_a_suspension(self):
        quote = {"at": "2026-11-27T10:15:00+08:00", "price": 51.5, "volume": 0}
        with patch.object(market, "tencent_quotes", return_value={"sh601318": dict(quote)}), \
                patch.object(market, "eastmoney_quotes", return_value={}), \
                patch.object(market, "history", return_value=[]), \
                patch.object(market, "adjusted_history", return_value=[]), \
                patch.object(market, "dividends", return_value=None), \
                patch.object(market, "cn_trading_day", return_value=True):
            result = market.collect([{"code": "sh601318", "type": "A 股"}], date(2026, 11, 27))
        self.assertEqual(result["quotes"]["sh601318"]["volume"], 0)

    def test_missing_tick_can_only_fall_back_to_real_price_index_closes(self):
        day = date(2025, 11, 28)
        with patch("csi_reference.csi_bars", return_value=[("2025-11-27", 5000)]) as load:
            quote = market.index_close_quote("H30269", day)
        self.assertEqual(load.call_args.args[0], "H30269")
        self.assertEqual((quote["price"], quote["quote_type"], quote["date_only"]), (5000, "close", True))
        self.assertIsNone(market.index_close_quote("SPCLLHCP", day))

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
                patch.object(market, "cn_trading_day", return_value=False), \
                patch.object(market, "fund_distributions", return_value=0.0), \
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
