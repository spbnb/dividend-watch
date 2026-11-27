import json
import unittest
from datetime import date
from unittest.mock import patch

import market


DAY = date(2026, 11, 27)


class RankMarketTests(unittest.TestCase):
    def test_exchange_calendar_uses_exact_published_date_and_flag(self):
        calendar = {"data": [{"jyrq": "2026-10-01", "jybz": "0"},
                             {"jyrq": "2026-10-10", "jybz": "0"},
                             {"jyrq": "2026-10-12", "jybz": "1"}]}
        with patch.object(market, "read", return_value=json.dumps(calendar)) as fetch:
            self.assertFalse(market.cn_trading_day(date(2026, 10, 1)))
            self.assertFalse(market.cn_trading_day(date(2026, 10, 10)))
            self.assertTrue(market.cn_trading_day(date(2026, 10, 12)))
            self.assertIsNone(market.cn_trading_day(date(2026, 10, 13)))
            self.assertIn("month=2026-10", fetch.call_args.args[0])
            calendar["data"].append({"jyrq": "2026-10-12", "jybz": "0"})
            fetch.return_value = json.dumps(calendar)
            self.assertIsNone(market.cn_trading_day(date(2026, 10, 12)))
            fetch.side_effect = TimeoutError()
            self.assertIsNone(market.cn_trading_day(DAY))

    def test_stock_cash_ex_dates_share_basis_and_incomplete_records(self):
        def row(day, cash, shares=None):
            return {"SECUCODE": "600900.SH", "EX_DIVIDEND_DATE": day, "ASSIGN_PROGRESS": "实施分配",
                    "PRETAX_BONUS_RMB": cash, "BONUS_IT_RATIO": shares}
        rows = [row("2026-11-28", 200), row("2025-11-27", 100), row("2025-11-28", 10),
                row("2026-06-01", None, 10), row("2026-11-27", 2),
                {**row(None, 35), "ASSIGN_PROGRESS": "预披露"}]
        payload = {"success": True, "result": {"count": len(rows), "data": rows}}
        with patch.object(market, "read", side_effect=lambda *_a, **_k: json.dumps(payload)):
            self.assertAlmostEqual(market.dividends("sh600900", DAY), 0.7)
            self.assertIsNone(market.dividends("sh600036", DAY))
            self.assertEqual(market.dividends("sh600900", date(2029, 1, 1)), 0.0)
            payload["result"]["count"] += 1
            self.assertIsNone(market.dividends("sh600900", DAY))

    def test_fund_explicit_zero_distinct_from_unknown_and_rebases_cash_after_split(self):
        def table(header, rows):
            tr = "".join("<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>" for row in rows)
            return f"<table><thead><tr><th>{header}</th></tr></thead><tbody>{tr}</tbody></table>"
        prefix = "<title>华夏国证自由现金流ETF(159201)基金分红送配 _ 基金档案</title>"
        cash_row = ["2026年", "2026-03-01", "2026-03-02", "每10份派现金1.2000元", "2026-03-03"]
        split = table("拆分折算比例", [["2026年", "2026-06-01", "份额分拆", "1:2.0000"]])
        with patch.object(market, "read") as fetch:
            fetch.return_value = prefix + table("每10份分红", [cash_row]) + split
            self.assertAlmostEqual(market.fund_distributions("159201", DAY), 0.06)
            self.assertIsNone(market.fund_distributions("159545", DAY))
            fetch.return_value = prefix + table("每10份分红", [["暂无分红信息!"]]) + split
            self.assertEqual(market.fund_distributions("159201", DAY), 0.0)
            fetch.return_value = prefix + table("每10份分红", []) + split
            self.assertIsNone(market.fund_distributions("159201", DAY))
            cash_row[3] = "待公告"
            fetch.return_value = prefix + table("每10份分红", [cash_row]) + split
            self.assertIsNone(market.fund_distributions("159201", DAY))
