"""Deliver threshold events and the daily review to individually configured channels."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, time as clock_time
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen

from alert_format import format_alerts, mobile_html, plain_body
from market import CST
from monitor import HITS, current, fresh, in_session


class DeliveryRejected(RuntimeError):
    """The provider explicitly refused this message."""


def post_json(url, payload, headers=None):
    req = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})})
    with urlopen(req, timeout=12) as response:
        return json.load(response)


def feishu(title, lines):
    payload = {"msg_type": "interactive", "card": {
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "green"},
        "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": plain_body(lines)}}]}}
    secret = os.getenv("FEISHU_SECRET")
    if secret:
        ts = str(int(time.time()))
        key = (ts + "\n" + secret).encode()
        payload.update(timestamp=ts, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
    result = post_json(os.environ["FEISHU_WEBHOOK"], payload)
    status = result.get("code", result.get("StatusCode"))
    if status is None:
        raise RuntimeError("Feishu acknowledgement missing")
    if status != 0:
        raise DeliveryRejected("Feishu rejected the card")


def pushplus(title, lines):
    result = post_json("https://www.pushplus.plus/send", {"token": os.environ["PUSHPLUS_TOKEN"],
                       "title": title, "content": plain_body(lines), "template": "txt"})
    if result.get("code") is None:
        raise RuntimeError("PushPlus acknowledgement missing")
    if result["code"] != 200:
        raise DeliveryRejected("PushPlus rejected the message")


def ntfy(title, lines):
    topic = os.environ["NTFY_TOPIC"].strip().removeprefix("https://ntfy.sh/")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        raise ValueError("Invalid ntfy topic")
    payload = {"topic": topic, "title": title, "message": plain_body(lines, 4000)}
    if os.getenv("DASHBOARD_URL"):
        payload["click"] = os.environ["DASHBOARD_URL"]
    token = os.getenv("NTFY_TOKEN")
    receipt = post_json("https://ntfy.sh/", payload, {"Authorization": "Bearer " + token} if token else {})
    if receipt.get("event") != "message" or receipt.get("topic") != topic or not receipt.get("id"):
        raise RuntimeError("ntfy did not acknowledge the topic")


def wxpusher(title, lines):
    spt = os.environ["WXPUSHER_SPT"].strip()
    if not re.fullmatch(r"SPT_[A-Za-z0-9_-]+", spt):
        raise ValueError("Invalid personal SPT")
    payload = {"spt": spt, "summary": title[:100], "content": mobile_html(lines), "contentType": 2}
    if os.getenv("DASHBOARD_URL"):
        payload["url"] = os.environ["DASHBOARD_URL"]
    receipt = post_json("https://wxpusher.zjiecode.com/api/send/message/simple-push", payload)
    targets = receipt.get("data")
    rejected = isinstance(targets, list) and (not targets or any(
        isinstance(t, dict) and t.get("code", 1000) != 1000 for t in targets))
    if (receipt.get("code") is not None and receipt["code"] != 1000) or receipt.get("success") is False:
        raise DeliveryRejected("WxPusher rejected the notification")
    if receipt.get("code") != 1000 or rejected:
        raise RuntimeError("WxPusher acknowledgement incomplete")


def email(title, lines):
    host, sender = os.environ["SMTP_HOST"], os.environ["SMTP_USER"]
    recipients = [entry.strip() for entry in os.environ["MAIL_TO"].split(",") if entry.strip()]
    port = int(os.getenv("SMTP_PORT") or "465")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(recipients), title
    msg.set_content(plain_body(lines))
    msg.add_alternative(f'<html lang="zh-CN"><body>{mobile_html(lines)}</body></html>',
                        subtype="html")
    tls = ssl.create_default_context()
    if port == 465:
        connection = smtplib.SMTP_SSL(host, port, timeout=15, context=tls)
    else:
        connection = smtplib.SMTP(host, port, timeout=15)
    with connection:
        if port != 465:
            connection.starttls(context=tls)
        connection.login(sender, os.environ["SMTP_PASS"])
        if connection.send_message(msg, to_addrs=recipients):
            raise RuntimeError("SMTP rejected a recipient")


def configured_channels():
    channels = {}
    if os.getenv("FEISHU_WEBHOOK") or os.getenv("FEISHU_SECRET"):
        channels["feishu"] = feishu
    if os.getenv("PUSHPLUS_TOKEN"):
        channels["pushplus"] = pushplus
    if os.getenv("NTFY_TOPIC") or os.getenv("NTFY_TOKEN"):
        channels["ntfy"] = ntfy
    if os.getenv("WXPUSHER_SPT"):
        channels["wxpusher"] = wxpusher
    if any(os.getenv(key) for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO")):
        channels["email"] = email
    return channels


def signal_time(row, signal):
    return row.get("index_at") if signal in {"index", "index_second"} else row.get("at")


def covered_keys(keys):
    # Keep the original first-tier keys so receipts from earlier versions still apply.
    shallow = {"second": "first", "rsi_second": "rsi", "rsi7_second": "rsi7", "index_second": "index"}
    covered = set()
    for key in keys:
        code, signal = key.split("/", 1)
        if signal in shallow:
            covered.add(f"{code}/{shallow[signal]}")
    return covered


def event_keys(row, now, benchmarks):
    market = row["market"]
    if not (in_session(now, market) and current(benchmarks.get(market), now, market)):
        return set()
    if "premium_max" in row and not current(benchmarks.get("HK"), now, "HK"):
        return set()
    if row["status"] in HITS:
        return {row["code"] + "/" + s for s in row["signals"] if fresh(signal_time(row, s), now)}
    return set()


def line_for(row):
    """Compact level listing for the optional end-of-day review."""
    digits = 4 if row.get("index") else (3 if row["type"] == "ETF" else 2)
    price = f"{row['price']:.{digits}f} {row['currency']}" if row["price"] else "暂无"
    label = row["label"]
    reasons = row["reasons"]
    details = "、".join(reasons)
    if row.get("premium_max") is not None and row.get("premium") is not None and row["status"] in HITS:
        details += f"，盘中溢价 {row['premium']:.2f}%"
    if row["status"] == "blocked":
        details += f"，溢价 {row['premium']:.2f}% 超过上限 {row['premium_max']:.2f}%"
    if row.get("index"):
        details = "，".join(filter(None, [row.get("source"), details]))
    targets = f"{row['first']:.{digits}f}" + (f" / {row['second']:.{digits}f}" if row.get("second") else "")
    return (f"{row['name']}（{row['code']}）　{price}｜目标 {targets}"
            f"｜{label}" + (f" · {details}" if details else ""))


def delivery(snapshot, now, mode, sent):
    if mode == "test":
        return "红利观察台 · 渠道测试", ["这是一条手动测试消息。", f"北京时间 {now:%Y-%m-%d %H:%M}"], set()
    if snapshot["generated"][:10] != now.date().isoformat():
        return None
    if mode == "scheduled" and not (clock_time(8) <= now.time() < clock_time(14, 31)):
        return None
    if mode == "scheduled-final" and not (clock_time(14, 30) <= now.time() < clock_time(15)):
        return None
    benchmarks = snapshot["benchmarks"]
    summary = (mode == "tail" and "summary" not in sent and now.weekday() < 5
               and now.hour * 60 + now.minute >= 14 * 60 + 20 and current(benchmarks.get("CN"), now, "CN"))
    events, keys = [], set()
    for row in snapshot["rows"]:
        active = event_keys(row, now, benchmarks)
        if active - sent:
            events.append({"row": row, "keys": active - sent})
            keys.update(active)
            keys.update(covered_keys(active))
    if summary:
        keys.add("summary")
    if not keys:
        return None
    if events:
        title, lines = format_alerts(events, now, "GitHub" if mode.startswith("scheduled") else "红利观察台")
    if summary:
        title = "红利观察台 · " + ("收盘后复盘" if now.hour >= 15 else "尾盘巡检")
        if not events:
            lines = [f"北京时间 {now:%Y-%m-%d %H:%M}"]
        lines += ["全部监控水位"] + [line_for(r) for r in snapshot["rows"]]
    link = os.getenv("DASHBOARD_URL")
    if link:
        lines.append(link)
    return title, lines, keys


def send_all(snapshot, mode, path, channels, clock=lambda: datetime.now(CST)):
    today = clock().date().isoformat()
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    sent = state.get("sent", {}) if state.get("date") == today else {}
    errors, changed = [], False
    for channel, send in channels.items():
        known = set(sent.get(channel, []))
        packet = delivery(snapshot, clock(), mode, known)
        if not packet:
            continue
        title, lines, keys = packet
        try:
            send(title, lines)
        except Exception as exc:
            # Provider errors can contain tokens, webhook URLs or email addresses.
            print(f"::error::{channel} delivery failed ({type(exc).__name__})")
            errors.append(channel)
            continue
        print(f"{channel}: message accepted")
        if keys:
            sent[channel] = sorted(known | keys)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({"date": today, "sent": sent}), encoding="utf-8")
            temp.replace(path)
            changed = True
    return changed, errors


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record), encoding="utf-8")
    temp.replace(path)


def claim_id(day, channel, key):
    # The cross-run artifact contains only opaque hashes, never recipient or alert details.
    destination = {"ntfy": "NTFY_TOPIC", "wxpusher": "WXPUSHER_SPT", "feishu": "FEISHU_WEBHOOK",
                   "pushplus": "PUSHPLUS_TOKEN", "email": "MAIL_TO"}[channel]
    return hmac.new(os.getenv(destination, "").strip().encode(), f"{day}/{channel}/{key}".encode(),
                    hashlib.sha256).hexdigest()


def reserved_delivery(snapshot, mode, folder, channels, phase, clock=lambda: datetime.now(CST)):
    """The workflow publishes the cumulative reservation before allowing any network send."""
    day = clock().date().isoformat()
    checkpoint, plan_path = folder / "claims.json", folder / "plan.json"
    stored = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
    claims = set(stored["claims"]) if stored.get("date") == day else set()
    if phase == "reserve":
        legacy_path = folder / "sent.json"
        legacy = json.loads(legacy_path.read_text(encoding="utf-8")) if legacy_path.exists() else {}
        plan = {}
        for channel in channels:
            if legacy.get("date") == day:
                claims.update(claim_id(day, channel, k) for k in legacy["sent"].get(channel, []))
            now = clock()
            all_events = delivery(snapshot, now, mode, set())
            if not all_events:
                continue
            known = {k for k in all_events[2] if claim_id(day, channel, k) in claims}
            packet = delivery(snapshot, now, mode, known)
            if packet:
                new = packet[2] - known
                plan[channel] = {"known": sorted(known), "new": sorted(new)}
                claims.update(claim_id(day, channel, k) for k in new)
        if plan:
            write_record(checkpoint, {"date": day, "claims": sorted(claims)})
            write_record(plan_path, {"date": day, "channels": plan})
        return bool(plan), []

    consumed = folder / "dispatching.json"
    try:
        plan_path.replace(consumed)
    except FileNotFoundError:
        if consumed.exists():
            return False, []
        raise
    plan = json.loads(consumed.read_text(encoding="utf-8"))
    if plan["date"] != day:
        return False, []
    errors, released = [], set()
    for channel, entry in plan["channels"].items():
        known, new = set(entry["known"]), set(entry["new"])
        packet = delivery(snapshot, clock(), mode, known)
        active = packet[2] - known if packet else set()
        if not active <= new or not all(claim_id(day, channel, k) in claims for k in active):
            raise RuntimeError("Missing delivery reservation")
        if packet:
            try:
                channels[channel](*packet[:2])
                print(f"{channel}: message accepted")
            except DeliveryRejected:
                errors.append(channel)
                print(f"::error::{channel} explicitly rejected delivery")
                active.clear()
            except Exception as exc:
                errors.append(channel)
                print(f"::error::{channel} receipt unknown ({type(exc).__name__}); reserved to prevent duplicates")
        released.update(claim_id(day, channel, k) for k in new - active)
    if released:
        write_record(checkpoint, {"date": day, "claims": sorted(claims - released)})
    return bool(released), errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("alerts", "scheduled", "scheduled-final", "tail", "test"), default="alerts")
    parser.add_argument("--channel", choices=("all", "ntfy", "wxpusher", "feishu", "pushplus", "email"), default="all")
    parser.add_argument("--snapshot", type=Path, default=Path("docs/snapshot.json"))
    parser.add_argument("--state", type=Path, default=Path(".monitor-state/sent.json"))
    parser.add_argument("--phase", choices=("direct", "reserve", "deliver"), default="direct")
    args = parser.parse_args()
    channels = configured_channels()
    if args.mode == "test" and args.channel != "all":
        channels = {k: v for k, v in channels.items() if k == args.channel}
    if not channels:
        print("::notice::所选渠道尚未配置。请检查仓库 Actions Secrets。")
        return 1 if args.mode == "test" else 0
    snapshot = {} if args.mode == "test" else json.loads(args.snapshot.read_text(encoding="utf-8"))
    if args.phase == "direct":
        changed, errors = send_all(snapshot, args.mode, args.state, channels)
    else:
        changed, errors = reserved_delivery(snapshot, args.mode, args.state.parent, channels, args.phase)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            print("changed=" + str(changed).lower(), file=out)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
