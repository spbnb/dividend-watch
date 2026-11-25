import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import alert_format
import market
import local_scan
import monitor
import notify


NOW = datetime(2026, 11, 24, 14, 25, tzinfo=market.CST)
ITEMS = {i["code"]: i for i in json.loads((monitor.ROOT / "watchlist.json").read_text(encoding="utf-8"))}
# Historical H-share example used to exercise HK market hours and notification formatting.
HK_EXAMPLE = dict(ITEMS["sh600938"], code="hk00883", name="中国海油 H", type="港股", first=20.00, second=18.50)


def quote(price=1, at=NOW, **values):
    return {"price": price, "at": at.isoformat(), "pct": 0, "source": "fixture", **values}


def fixture(code, q=None, **extra):
    quotes = {"sh000001": quote(3000), "hkHSI": quote(23000)}
    if q:
        quotes[code] = q
    return {"quotes": quotes, **extra}


class RuleTests(unittest.TestCase):
    def test_watchlist_includes_cash_flow_etf_and_only_the_a_share_of_cnooc(self):
        data = fixture("sh600938", quote(27))
        data["quotes"]["hk00883"] = quote(20)
        snap = monitor.build(list(ITEMS.values()), data, NOW)
        self.assertEqual((12, 12), (snap["groups"], len(snap["rows"])))
        codes = {r["code"] for r in snap["rows"]}
        self.assertIn("sh600938", codes)
        self.assertIn("sz159201", codes)
        self.assertNotIn("hk00883", codes)
        for row in snap["rows"]:
            self.assertEqual((row["rsi_max"], row["rsi_second_max"], row["rsi7_max"], row["rsi7_second_max"]),
                             (35, 30, 25, 20), row["code"])

    def test_all_listed_stocks_can_send_rsi_alerts_on_their_own_exchange(self):
        for item in (i for i in ITEMS.values() if i["type"] in {"A 股", "港股"}):
            with self.subTest(code=item["code"]):
                code, price = item["code"], item["first"] + 2
                dates = [(NOW.date() - timedelta(days=n)).isoformat() for n in range(25, 0, -1)]
                data = fixture(code, quote(price))
                # The last mainland trading day can differ from Hong Kong's.
                hk_bars = [(d, 25000) for d in dates[:-1]] + [(NOW.date().isoformat(), 25001)]
                data["history:hkHSI"] = hk_bars
                data["history:sh000001"] = [(d, 3000) for d in dates] + [(NOW.date().isoformat(), 3001)]
                own_dates = dates[:-1] if code.startswith("hk") else dates
                data["history:" + code] = [(d, price + len(own_dates) - n) for n, d in enumerate(own_dates)]
                snap = monitor.build([item], data, NOW)
                self.assertEqual((snap["rows"][0]["rsi"], snap["rows"][0]["rsi7"]), (0, 0))
                self.assertEqual(local_scan.live_report(snap, NOW)["events"][0]["keys"],
                                 [code + "/rsi7_second", code + "/rsi_second"])
                self.assertIsNotNone(notify.delivery(snap, NOW, "scheduled", set()))
                data["history:" + code].pop()
                self.assertEqual(monitor.evaluate(item, data, NOW)["status"], "partial")

    def test_h_share_uses_adjusted_hong_kong_bars(self):
        code = "hk00883"
        payload = {"day": [["2026-11-23", "21", "21.5"]], "qfqday": [["2026-11-23", "20", "20.5"]]}
        with patch.object(market, "read", return_value=json.dumps({"data": {code: payload}})) as fetch:
            self.assertEqual(market.history(code), [("2026-11-23", 20.5)])
            self.assertIn("/hkfqkline/get?", fetch.call_args.args[0])
            fetch.return_value = json.dumps({"data": {code: {"day": payload["day"]}}})
            self.assertEqual(market.history(code), [])

    def test_fund_rsi_uses_distribution_adjusted_returns_and_only_one_intraday_tick(self):
        item = ITEMS["012708"]
        dates = [NOW - timedelta(days=n) for n in range(45, -1, -1)]
        records = [{"x": int(d.timestamp() * 1000), "y": 1.6 if d < NOW else 1.38, "equityReturn": 1}
                   for d in dates]
        raw = 'var fS_code = "012708"; var Data_netWorthTrend = ' + json.dumps(records) + ';'
        with patch.object(market, "read", return_value=raw):
            bars = market.fund_history("012708")
            self.assertEqual(market.fund_history("012709"), [])
        data = fixture("csi931446", quote(3100, pct=-1.6))
        data["history:sh000001"] = [(d.date().isoformat(), 3000) for d in dates]
        data["history:012708"] = bars
        data["estimate:012708"] = quote(1.38, pct=0.5)
        closes = monitor.indicator_closes(item, data, data["estimate:012708"], "CN")
        self.assertEqual(len(closes), len(bars))
        self.assertAlmostEqual(closes[-1], bars[-2][1] * 1.005)
        row = monitor.evaluate(item, data, NOW)
        self.assertAlmostEqual(row["rsi"], 100)
        self.assertAlmostEqual(row["rsi7"], 100)
        self.assertEqual(row["signals"], ["index"])
        data["estimate:012708"] = quote(1.38, pct=None)
        self.assertIsNone(monitor.evaluate(item, data, NOW)["rsi"])
        data["nav:012708"] = {NOW.date().isoformat(): {"price": 1.38, "pct": None}}
        official = monitor.evaluate(item, data, NOW.replace(hour=17))
        self.assertAlmostEqual(official["rsi7"], 100)
        self.assertEqual(monitor.hit_label(["rsi7_second", "index"]), "RSI 预警 · 二档 / 指数跌幅 · 首档")

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

    def test_domestic_cash_flow_etf_alerts_at_price_tiers_without_hk_quotes(self):
        item = ITEMS["sz159201"]
        for field in ("first", "second"):
            with self.subTest(tier=field):
                data = fixture(item["code"], quote(item[field]))
                del data["quotes"]["hkHSI"]
                snap = monitor.build([item], data, NOW)
                key = item["code"] + "/" + field
                self.assertEqual(local_scan.live_report(snap, NOW)["events"][0]["keys"], [key])
                _, cards, keys = notify.delivery(snap, NOW, "scheduled", set())
                self.assertIn(key, keys)
                self.assertIn("折溢价 待更新（仅观察）", cards[0])
                data["quotes"][item["code"]]["iopv"] = item[field] / 1.02
                snap = monitor.build([item], data, NOW)
                self.assertIn("折溢价 +2.00%（仅观察）", notify.delivery(snap, NOW, "scheduled", set())[1][0])

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

    def test_dual_rsi_tiers_combine_and_cover_only_their_own_series(self):
        code = "sh513530"
        bars = [((NOW.date() - timedelta(days=n)).isoformat(), 1.55) for n in range(20, -1, -1)]
        data = fixture(code, quote(1.52, iopv=1.52))
        data.update({"history:sh000001": bars, "history:" + code: bars})
        def scan(long, short):
            with patch.object(monitor, "wilder_rsi", side_effect=lambda closes, period=14: {14: long, 7: short}[period]):
                return monitor.build([ITEMS[code]], data, NOW)
        seed = scan(35.001, 25.001)
        self.assertEqual(seed["rows"][0]["signals"], [])
        shallow = scan(35, 25)
        receipts = notify.delivery(shallow, NOW, "scheduled", set())[2]
        self.assertEqual(receipts, {code + "/rsi", code + "/rsi7"})
        deep = scan(30, 20)
        row = deep["rows"][0]
        self.assertEqual(row["signals"], ["rsi_second", "rsi7_second"])
        self.assertEqual(row["label"], "RSI 预警 · 二档")
        title, lines, all_keys = notify.delivery(deep, NOW, "scheduled", receipts)
        self.assertIn("GitHub", title)
        self.assertEqual(sum("RSI(14)" in line and "RSI(7)" in line for line in lines), 1)
        self.assertIsNone(notify.delivery(shallow, NOW, "scheduled", all_keys))
        local = local_scan.live_report(deep, NOW)
        self.assertEqual(len(local["events"]), 1)
        self.assertEqual(set(local["events"][0]["covers"]), receipts)
        self.assertEqual(len(local["rows"][0]), len(local["columns"]))
        self.assertEqual(scan(30, 27)["rows"][0]["signals"], ["rsi_second"])
        self.assertEqual(scan(37, 20)["rows"][0]["signals"], ["rsi7_second"])
        data["quotes"][code]["iopv"] = 1.40
        blocked = scan(28, 18)
        self.assertIsNone(notify.delivery(blocked, NOW, "scheduled", set()))
        self.assertEqual(local_scan.live_report(blocked, NOW)["events"], [])

    def test_fund_nav_and_index_are_independent_two_tier_series(self):
        code = "012708"
        for nav, pct, expected in [(1.36, -1.49, ["first"]), (1.30, -1.5, ["second", "index"]),
                                   (1.37, -2.5, ["index_second"]), (1.30, -2.5, ["second", "index_second"])]:
            with self.subTest(nav=nav, pct=pct):
                data = fixture("csi931446", quote(3000, pct=pct))
                data["estimate:" + code] = quote(nav)
                snap = monitor.build([ITEMS[code]], data, NOW)
                self.assertEqual(snap["rows"][0]["signals"], expected)
                packet = notify.delivery(snap, NOW, "scheduled", set())
                self.assertEqual(len(local_scan.live_report(snap, NOW)["events"]), 1)
                if "second" in expected:
                    self.assertIn("估算净值到达二档", " ".join(packet[1]))
                    self.assertIn(code + "/first", packet[2])
                if "index_second" in expected:
                    self.assertIn(code + "/index", packet[2])
        old = NOW - timedelta(minutes=24)
        data["estimate:" + code] = quote(1.30, old)
        snap = monitor.build([ITEMS[code]], data, NOW)
        later = NOW + timedelta(minutes=2)
        packet = notify.delivery(snap, later, "scheduled", set())
        self.assertEqual(packet[2], {code + "/index", code + "/index_second"})
        self.assertNotIn("估算净值到达二档", " ".join(packet[1]))
        self.assertIn("当前估值待更新", "\n".join(packet[1]))
        self.assertNotIn("还需跌", "\n".join(packet[1]))
        self.assertIn("指数跌幅二档", packet[0])

    def test_phone_cards_distinguish_rsi_from_price_and_use_current_price_for_distance(self):
        data = fixture("hk00883", quote(21.08))
        data["quotes"]["sh600938"] = quote(29.56)
        snap = monitor.build([ITEMS["sh600938"], HK_EXAMPLE], data, NOW)
        by_code = {r["code"]: r for r in snap["rows"]}
        a, h = by_code["sh600938"], by_code["hk00883"]
        a.update(status="signal", rsi=34.71, signals=["rsi"])
        h.update(status="signal", rsi=28.74, rsi7=21.76, signals=["rsi_second", "rsi7"])
        title, blocks, _ = notify.delivery(snap, NOW, "scheduled", set())
        self.assertEqual(title, "海油 A/H｜RSI 触发，均未到首档 · GitHub")
        self.assertTrue(blocks[0].startswith("中国海油 H 股 · 00883.HK\n"))
        self.assertIn("1.08 港元（5.12%）", blocks[0])
        self.assertIn("2.56 元（8.66%）", blocks[1])
        self.assertIn("首档 20.00 · 二档 18.50 港元", blocks[0])
        self.assertIn("RSI(14) 二档　28.74 ≤ 30\nRSI(7) 首档　21.76 ≤ 25", blocks[0])
        html = alert_format.mobile_html(blocks)
        self.assertEqual(html.count("border-left:"), 2)
        self.assertNotIn(title, html)
        self.assertIn("甲&lt;乙 &amp; 丙", alert_format.mobile_html(["甲<乙 & 丙\n现价 1"]))
        self.assertEqual(alert_format.plain_body([blocks[0], blocks[1]], len((blocks[0] + "\n\n…其余内容见监控看板").encode())),
                         blocks[0] + "\n\n…其余内容见监控看板")
        h.update(price=19.50, signals=["first", "rsi_second", "rsi7"], status="first")
        before = {"sh600938/rsi", "hk00883/first", "hk00883/rsi7"}
        title, blocks, _ = notify.delivery(snap, NOW, "scheduled", before)
        self.assertEqual(title, "中国海油 H 股｜RSI 触发 · GitHub")
        self.assertIn("现价 19.50 港元 · 已到首档", blocks[0])
        self.assertNotIn("RSI(7)", blocks[0])
        h.update(price=18.50, signals=["second", "rsi_second", "rsi7"], status="second")
        title, blocks, _ = notify.delivery(snap, NOW, "scheduled", before)
        self.assertIn("价格到二档", title)
        self.assertIn("价格到达二档", blocks[0])

    def test_cloud_reserves_before_send_and_retries_only_definite_rejections(self):
        code = "sh600900"
        snap = monitor.build([ITEMS[code]], fixture(code, quote(26)), NOW)
        calls = []
        def unknown(*_):
            calls.append("ntfy")
            raise TimeoutError()
        def reject(*_):
            calls.append("wxpusher")
            raise notify.DeliveryRejected()
        channels = {"ntfy": unknown, "wxpusher": reject}
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(td)
            def run(phase):
                return notify.reserved_delivery(snap, "scheduled", folder, channels, phase, lambda: NOW)
            self.assertEqual(run("reserve"), (True, []))
            self.assertEqual(calls, [])
            ledger = (folder / "claims.json").read_text()
            self.assertNotIn("ntfy", ledger)
            self.assertNotIn(code, ledger)
            self.assertEqual(run("deliver"), (True, ["ntfy", "wxpusher"]))
            self.assertEqual(run("reserve"), (True, []))
            channels["wxpusher"] = lambda *_: calls.append("wxpusher")
            self.assertEqual(run("deliver"), (False, []))
            self.assertEqual(run("reserve"), (False, []))
            self.assertEqual(calls, ["ntfy", "wxpusher", "wxpusher"])

    def test_observe_only_drawdown_and_rsi(self):
        bars = [((NOW.date() - timedelta(days=n)).isoformat(), 2.0) for n in range(65, -1, -1)]
        data = fixture("sh512890", quote(1.7))
        data.update({"history:sh000001": bars, "history:sh512890": bars})
        r = monitor.evaluate(ITEMS["sh512890"], data, NOW)
        self.assertEqual((r["status"], r["signals"]), ("observe_hit", ["rsi_second", "rsi7_second", "drawdown"]))
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
        snap = monitor.build([ITEMS["sh600900"], HK_EXAMPLE], data, first)
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
        both = [ITEMS["sh600900"], HK_EXAMPLE]
        data = {"quotes": {k: quote(15, near) for k in ("sh000001", "hkHSI", "sh600900", "hk00883")}}
        snapshot = monitor.build(both, data, near)
        events = local_scan.live_report(snapshot, near)["events"]
        mainland = next(e for e in events if "sh600900/second" in e["keys"])
        self.assertEqual(datetime.fromisoformat(mainland["expires"]).minute, 0)
        self.assertEqual(mainland["covers"], ["sh600900/first"])
        remaining = local_scan.live_report(snapshot, after)["events"]
        self.assertEqual([e["keys"] for e in remaining], [["hk00883/second"]])

    def test_last_queued_run_reports_only_before_1500_even_for_hk(self):
        item = HK_EXAMPLE
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
                self.assertEqual((body["contentType"], body["summary"]), (2, "测试"))
                self.assertIn("正文", body["content"])
            with patch.object(notify, "post_json", return_value={"code": 1000, "data": [{"code": 1001}]}):
                with self.assertRaises(RuntimeError):
                    notify.wxpusher("测试", ["正文"])
        with patch.dict("os.environ", {"NTFY_TOPIC": "fixture-topic"}, clear=True):
            self.assertEqual(list(notify.configured_channels()), ["ntfy"])


if __name__ == "__main__":
    unittest.main()
