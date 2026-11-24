"""Deliver threshold events and the daily review to individually configured channels."""

import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen

from market import CST
from monitor import HITS, current, fresh, in_session


def post_json(url, payload):
    req = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Content-Type": "application/json; charset=utf-8"})
    with urlopen(req, timeout=12) as response:
        return json.load(response)


def feishu(title, lines):
    payload = {"msg_type": "interactive", "card": {
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "green"},
        "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(lines)}}]}}
    secret = os.getenv("FEISHU_SECRET")
    if secret:
        ts = str(int(time.time()))
        key = (ts + "\n" + secret).encode()
        payload.update(timestamp=ts, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
    result = post_json(os.environ["FEISHU_WEBHOOK"], payload)
    if result.get("code", result.get("StatusCode")) != 0:
        raise RuntimeError("Feishu rejected the card")


def pushplus(title, lines):
    result = post_json("https://www.pushplus.plus/send", {"token": os.environ["PUSHPLUS_TOKEN"],
                       "title": title, "content": "\n".join(lines), "template": "txt"})
    if result.get("code") != 200:
        raise RuntimeError("PushPlus rejected the message")


def email(title, lines):
    host, sender = os.environ["SMTP_HOST"], os.environ["SMTP_USER"]
    recipients = [entry.strip() for entry in os.environ["MAIL_TO"].split(",") if entry.strip()]
    port = int(os.getenv("SMTP_PORT") or "465")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(recipients), title
    msg.set_content("\n".join(lines))
    blocks = "".join(f"<p>{html.escape(line)}</p>" for line in lines if line)
    msg.add_alternative(f'<html lang="zh-CN"><body><h2>{html.escape(title)}</h2>{blocks}</body></html>',
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
    if any(os.getenv(key) for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO")):
        channels["email"] = email
    return channels


def event_keys(row, now, benchmarks):
    market = row["market"]
    if not (in_session(now, market) and current(benchmarks.get(market), now, market)
            and fresh(row.get("signal_at"), now)):
        return set()
    if row["status"] == "blocked":
        return {row["code"] + "/premium"}
    if "premium_max" in row and not current(benchmarks.get("HK"), now, "HK"):
        return set()
    if row["status"] in HITS:
        return {row["code"] + "/" + signal for signal in row["signals"]}
    return set()


def line_for(row):
    digits = 4 if row.get("index") else (3 if row["type"] == "ETF" else 2)
    price = f"{row['price']:.{digits}f} {row['currency']}" if row["price"] else "暂无"
    details = "、".join(row["reasons"])
    if row["status"] == "blocked":
        details += f"，溢价 {row['premium']:.2f}% 超过上限 {row['premium_max']:.2f}%"
    if row.get("index"):
        details = "，".join(filter(None, [row.get("source"), details]))
    return (f"{row['name']}（{row['code']}）　{price}｜首档 {row['first']:.{digits}f}"
            f"｜{row['label']}" + (f" · {details}" if details else ""))


def delivery(snapshot, now, mode, sent):
    if mode == "test":
        return "红利观察台 · 渠道测试", ["这是一条手动测试消息。", f"北京时间 {now:%Y-%m-%d %H:%M}"], set()
    if snapshot["generated"][:10] != now.date().isoformat():
        return None
    benchmarks = snapshot["benchmarks"]
    summary = (mode == "tail" and "summary" not in sent and now.weekday() < 5
               and now.hour * 60 + now.minute >= 14 * 60 + 20 and current(benchmarks.get("CN"), now, "CN"))
    rows, keys = [], set()
    for row in snapshot["rows"]:
        active = event_keys(row, now, benchmarks)
        if active - sent:
            rows.append(row)
            keys.update(active)
            if row["code"] + "/second" in active:
                keys.add(row["code"] + "/first")
    if summary:
        keys.add("summary")
    if not keys:
        return None
    title = "收盘后复盘" if summary and now.hour >= 15 else "尾盘巡检" if summary else "阈值提醒"
    lines = [f"北京时间 {now:%Y-%m-%d %H:%M}"]
    if rows:
        lines += ["", "新触发"] + [line_for(r) for r in rows]
    if summary:
        lines += ["", "全部监控水位"] + [line_for(r) for r in snapshot["rows"]]
    link = os.getenv("DASHBOARD_URL")
    if link:
        lines.extend(["", link])
    return f"红利观察台 · {title}", lines, keys


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("alerts", "tail", "test"), default="alerts")
    parser.add_argument("--snapshot", type=Path, default=Path("docs/snapshot.json"))
    parser.add_argument("--state", type=Path, default=Path(".monitor-state/sent.json"))
    args = parser.parse_args()
    channels = configured_channels()
    if not channels:
        print("::notice::尚未配置通知渠道。请在仓库 Actions Secrets 中设置飞书、PushPlus 或 SMTP。")
        return 1 if args.mode == "test" else 0
    snapshot = {} if args.mode == "test" else json.loads(args.snapshot.read_text(encoding="utf-8"))
    changed, errors = send_all(snapshot, args.mode, args.state, channels)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            print("changed=" + str(changed).lower(), file=out)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
