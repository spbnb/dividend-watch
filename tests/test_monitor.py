import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import market
import monitor
import notify


NOW = datetime(2026, 11, 24, 14, 25, tzinfo=market.CST)
ITEMS = {i["code"]: i for i in json.loads((monitor.ROOT / "watchlist.json").read_text(encoding="utf-8"))}


def quote(price=1, at=NOW, **values):
    return {"price": price, "at": at.isoformat(), "pct": 0, "source": "fixture", **values}


def fixture(code, q=None, **extra):
    quotes = {"sh000001": quote(3000), "hkHSI": quote(23000)}
    if q:
        quotes[code] = q
    return {"quotes": quotes, **extra}


class RuleTests(unittest.TestCase):
    def test_11_groups_with_separate_currencies(self):
        data = fixture("sh600938", quote(27))
        data["quotes"]["hk00883"] = quote(20)
        snap = monitor.build(list(ITEMS.values()), data, NOW)
        self.assertEqual((11, 12), (snap["groups"], len(snap["rows"])))
        self.assertEqual({"CNY", "HKD"}, {r["currency"] for r in snap["rows"] if r.get("group") == "cnooc"})

    def test_premium_boundaries_and_missing_iopv(self):
        item = ITEMS["sh513530"]
        for premium, status in [(1.49, "second"), (1.50, "second"), (1.51, "blocked"), (None, "verify")]:
            with self.subTest(premium=premium):
                q = quote(1.3, iopv=None if premium is None else 1.3 / (1 + premium / 100))
                self.assertEqual(monitor.evaluate(item, fixture(item["code"], q), NOW)["status"], status)

    def test_prior_hk_session_blocks_cross_border_signal_only(self):
        data = fixture("sz159545", quote(1.2, iopv=1.2))
        data["quotes"].update(hkHSI=quote(at=NOW - timedelta(days=1)), sh600036=quote(41))
        self.assertEqual(monitor.evaluate(ITEMS["sz159545"], data, NOW)["status"], "verify")
        self.assertEqual(monitor.evaluate(ITEMS["sh600036"], data, NOW)["status"], "first")

    def test_stale_suspended_and_future_rows_never_hit(self):
        for q in (quote(20, NOW - timedelta(days=1)), quote(20, NOW - timedelta(minutes=26)),
                  quote(20, volume=0), quote(20, NOW + timedelta(minutes=5))):
            self.assertEqual(monitor.evaluate(ITEMS["sh600900"], fixture("sh600900", q), NOW)["status"], "stale")

    def test_nav_proxy_needs_matching_previous_session(self):
        data = fixture("csi931446", quote(1200, pct=-2))
        data["history:sh000001"] = [("2026-11-23", 3100)]
        data["nav:012708"] = {"2026-11-23": {"price": 1.38, "pct": 0}}
        r = monitor.evaluate(ITEMS["012708"], data, NOW)
        self.assertAlmostEqual(r["price"], 1.3524)
        self.assertEqual(r["signals"], ["first", "index"])
        data["nav:012708"] = {"2026-11-20": {"price": 1.2, "pct": 0}}
        r = monitor.evaluate(ITEMS["012708"], data, NOW)
        self.assertEqual((r["signals"], r["gap"]), (["index"], None))

    def test_fund_estimate_change_is_not_index_change(self):
        data = fixture("csi931446", quote(1200, pct=-0.1))
        data["estimate:012708"] = quote(1.5, pct=-3.2)
        self.assertEqual(monitor.evaluate(ITEMS["012708"], data, NOW)["signals"], [])

    def test_history_timestamp_and_wilder_extremes(self):
        self.assertEqual(market.timestamp("2026/11/24 14:25:00"), NOW.isoformat())
        self.assertEqual(market.timestamp("20261124142500"), NOW.isoformat())
        self.assertEqual(monitor.wilder_rsi(list(range(20))), 100)
        self.assertEqual(monitor.wilder_rsi(list(range(20, 0, -1))), 0)
        self.assertEqual(monitor.wilder_rsi([2] * 20), 50)
        self.assertIsNone(monitor.wilder_rsi([1] * 14))

    def test_observe_only_drawdown(self):
        bars = [((NOW.date() - timedelta(days=n)).isoformat(), 2.0) for n in range(65, -1, -1)]
        data = fixture("sh512890", quote(1.7))
        data.update({"history:sh000001": bars, "history:sh512890": bars})
        r = monitor.evaluate(ITEMS["sh512890"], data, NOW)
        self.assertEqual((r["status"], r["signals"]), ("observe_hit", ["drawdown"]))
        self.assertFalse(notify.event_keys(r, NOW, {"CN": quote()}))

    def test_receipts_are_independent_and_deeper_level_escalates(self):
        data = fixture("sh600900", quote(28.5))
        snap = monitor.build([ITEMS["sh600900"]], data, NOW)
        counts = []
        def ok(*_):
            counts.append("ok")
        def fail(*_):
            raise OSError()
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
            state = Path(td) / "sent.json"
            channels = {"email": ok, "feishu": fail}
            self.assertEqual(notify.send_all(snap, "alerts", state, channels, lambda: NOW), (True, ["feishu"]))
            channels["feishu"] = ok
            notify.send_all(snap, "alerts", state, channels, lambda: NOW)
            self.assertEqual(len(counts), 2)
            data["quotes"]["sh600900"] = quote(26.8)
            deep = monitor.build([ITEMS["sh600900"]], data, NOW)
            notify.send_all(deep, "alerts", state, channels, lambda: NOW)
            notify.send_all(snap, "alerts", state, channels, lambda: NOW)
            self.assertEqual(len(counts), 4)

    def test_per_channel_cutoff_and_late_summary(self):
        first, later = NOW.replace(hour=14, minute=59), NOW.replace(hour=15, minute=1)
        data = {"quotes": {k: quote(at=first) for k in ("sh000001", "hkHSI", "sh600900", "hk00883")}}
        snap = monitor.build([ITEMS["sh600900"], ITEMS["hk00883"]], data, first)
        times, received = iter([first, first, later]), []
        with tempfile.TemporaryDirectory() as td:
            send = lambda title, body: received.append((title, body))
            path = Path(td) / "sent.json"
            notify.send_all(snap, "tail", path, {"a": send, "b": send}, lambda: next(times))
            sent = json.loads(path.read_text())["sent"]
            self.assertIn("sh600900/second", sent["a"])
            self.assertNotIn("sh600900/second", sent["b"])
            self.assertIn("hk00883/second", sent["b"])
            self.assertIn("收盘后复盘", received[1][0])
            self.assertIsNone(notify.delivery(snap, later, "tail", set(sent["b"])))

    def test_business_error_does_not_count_as_delivery(self):
        with patch.dict("os.environ", {"FEISHU_WEBHOOK": "test", "FEISHU_SECRET": "example"}):
            with patch.object(notify, "post_json", return_value={"code": 19021}) as post:
                with self.assertRaises(RuntimeError):
                    notify.feishu("test", ["body"])
                self.assertIn("sign", post.call_args.args[1])
        with patch.dict("os.environ", {"PUSHPLUS_TOKEN": "example"}):
            with patch.object(notify, "post_json", return_value={"code": 500}):
                with self.assertRaises(RuntimeError):
                    notify.pushplus("test", ["body"])


if __name__ == "__main__":
    unittest.main()
