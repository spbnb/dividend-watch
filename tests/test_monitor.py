import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import market
import local_scan
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
        data["history:sh000001"] = [("2026-11-23", 3100), ("2026-11-24", 3000)]
        data["nav:012708"] = {"2026-11-23": {"price": 1.38, "pct": 0}}
        r = monitor.evaluate(ITEMS["012708"], data, NOW)
        self.assertAlmostEqual(r["price"], 1.3524)
        self.assertEqual(r["signals"], ["first", "index"])
        data["nav:012708"] = {"2026-11-20": {"price": 1.2, "pct": 0}}
        r = monitor.evaluate(ITEMS["012708"], data, NOW)
        self.assertEqual((r["signals"], r["gap"]), (["index"], None))

    def test_stale_history_and_stale_nav_cannot_validate_each_other(self):
        data = fixture("csi931446", quote(1200, pct=-2))
        data["history:sh000001"] = [("2026-11-20", 3100)]
        data["nav:012708"] = {"2026-11-20": {"price": 1.2, "pct": 0}}
        data["quotes"]["sh000001"]["prev_close"] = 3050
        row = monitor.evaluate(ITEMS["012708"], data, NOW)
        self.assertEqual(row["signals"], ["index"])
        self.assertIn("已公布", row["source"])

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

    def test_half_hour_schedule_includes_both_endpoints(self):
        import re
        workflow = (monitor.ROOT / ".github/workflows/monitor.yml").read_text(encoding="utf-8")
        slots = re.findall(r"^\s*- cron: '([^']+)'", workflow, re.M)
        planned = []
        for cron in slots:
            minute, hours, *days = cron.split()
            self.assertEqual(days, ["*", "*", "1-5"])
            span = [int(h) for h in hours.split("-")]
            planned.extend((h + 8) * 60 + int(minute) for h in range(span[0], span[-1] + 1))
        self.assertEqual(sorted(planned), list(range(8 * 60, 14 * 60 + 31, 30)))
        self.assertIn("github.event.schedule == '30 6 * * 1-5' && 'scheduled-final'", workflow)

    def test_scheduled_mode_only_reports_new_buy_points_until_1430(self):
        item = ITEMS["sh600900"]
        for hour, minute, allow in ((8, 0, False), (11, 40, False), (14, 30, True), (14, 31, False)):
            instant = NOW.replace(hour=hour, minute=minute, second=40)
            data = {"quotes": {k: quote(27, instant) for k in ("sh000001", item["code"])}}
            snap = monitor.build([item], data, instant)
            self.assertEqual(bool(notify.delivery(snap, instant, "scheduled", set())), allow)
            self.assertIsNone(notify.delivery(snap, instant, "scheduled", {"sh600900/first"}))
        blocked = monitor.build([ITEMS["sh513530"]], fixture("sh513530", quote(1.3, iopv=1.2)), NOW)
        self.assertIsNone(notify.delivery(blocked, NOW, "scheduled", set()))
        self.assertEqual(local_scan.live_report(blocked, NOW)["events"], [])

    def test_local_scan_keeps_hk_open_after_cn_close_and_caps_expiry(self):
        near = NOW.replace(hour=14, minute=59, second=56)
        after = near.replace(hour=15, minute=2)
        both = [ITEMS["sh600900"], ITEMS["hk00883"]]
        data = {"quotes": {k: quote(15, near) for k in ("sh000001", "hkHSI", "sh600900", "hk00883")}}
        snapshot = monitor.build(both, data, near)
        events = local_scan.live_report(snapshot, near)["events"]
        mainland = next(e for e in events if "sh600900/second" in e["keys"])
        self.assertEqual(datetime.fromisoformat(mainland["expires"]).minute, 0)
        self.assertEqual(mainland["covers"], ["sh600900/first"])
        remaining = local_scan.live_report(snapshot, after)["events"]
        self.assertEqual([e["keys"] for e in remaining], [["hk00883/second"]])

    def test_last_queued_run_reports_only_before_1500_even_for_hk(self):
        item = ITEMS["hk00883"]
        for hour, minute, allow in ((14, 29, False), (14, 30, True), (14, 35, True), (14, 59, True), (15, 0, False)):
            instant = NOW.replace(hour=hour, minute=minute)
            data = {"quotes": {k: quote(19, instant) for k in ("hkHSI", item["code"])}}
            snap = monitor.build([item], data, instant)
            self.assertEqual(bool(notify.delivery(snap, instant, "scheduled-final", set())), allow)

    def test_local_cache_reuses_only_reference_inputs_until_due(self):
        items = [ITEMS["sh600900"]]
        with tempfile.TemporaryDirectory() as td, patch.object(local_scan, "collect") as collect:
            path = Path(td) / "refs.json"
            collect.return_value = {"quotes": {"live": 1}, "history:sh000001": [["2026-11-23", 3000]]}
            local_scan.collect_local(items, NOW, path)
            local_scan.collect_local(items, NOW + timedelta(seconds=30), path)
            self.assertEqual(set(collect.call_args.kwargs["references"]), {"history:sh000001"})
            local_scan.collect_local(items, NOW + timedelta(minutes=15), path)
            self.assertNotIn("references", collect.call_args.kwargs)

    def test_ntfy_uses_the_subscribed_topic_and_checks_receipt(self):
        config = {"NTFY_TOPIC": "fixture-topic", "NTFY_TOKEN": "fixture-token", "DASHBOARD_URL": "https://example.org/"}
        with patch.dict("os.environ", config, clear=True):
            with patch.object(notify, "post_json", return_value={"id": "123", "event": "message", "topic": "fixture-topic"}) as post:
                notify.ntfy("测试", ["通知" * 1000])
                url, body, headers = post.call_args.args
                self.assertEqual(url, "https://ntfy.sh/")
                self.assertEqual(headers["Authorization"], "Bearer fixture-token")
                self.assertEqual(body["click"], config["DASHBOARD_URL"])
                self.assertLessEqual(len(body["message"].encode()), 4096)
            with patch.object(notify, "post_json", return_value={"id": "123", "event": "message", "topic": "wrong"}):
                with self.assertRaises(RuntimeError):
                    notify.ntfy("测试", ["通知"])

    def test_wxpusher_and_ntfy_are_independently_configurable(self):
        with patch.dict("os.environ", {"WXPUSHER_SPT": "SPT_test_only"}, clear=True):
            self.assertEqual(list(notify.configured_channels()), ["wxpusher"])
            with patch.object(notify, "post_json", return_value={"code": 1000, "data": [{"code": 1000}]}) as post:
                notify.wxpusher("测试", ["正文"])
                url, body = post.call_args.args
                self.assertEqual(url, "https://wxpusher.zjiecode.com/api/send/message/simple-push")
                self.assertEqual(body["spt"], "SPT_test_only")
            with patch.object(notify, "post_json", return_value={"code": 1000, "data": [{"code": 1001}]}):
                with self.assertRaises(RuntimeError):
                    notify.wxpusher("测试", ["正文"])
        with patch.dict("os.environ", {"NTFY_TOPIC": "fixture-topic"}, clear=True):
            self.assertEqual(list(notify.configured_channels()), ["ntfy"])


if __name__ == "__main__":
    unittest.main()
