"""Present the events still due to one notification destination."""

import html
import json
import os
import sys
from datetime import datetime

from market import CST
from monitor import fresh


def stock_name(row):
    name = row["name"]
    if row["type"] in {"A 股", "港股"} and name.endswith((" A", " H")):
        name += " 股"
    return name


def display_code(code):
    venues = {"hk": "HK", "sh": "SH", "sz": "SZ"}
    return f"{code[2:]}.{venues[code[:2]]}" if code[:2] in venues else code


def live_price(row, now):
    price = row.get("price")
    return price if price and fresh(row.get("at"), now) else None


def alert_card(row, signals, now):
    fund = bool(row.get("index"))
    digits = 4 if fund else 3 if row["type"] == "ETF" else 2
    unit = "港元" if row["currency"] == "HKD" else "元"
    price, first, second = live_price(row, now), row["first"], row.get("second")
    basis = "估算净值" if fund else "价格"
    parts = [f"{stock_name(row)} · {display_code(row['code'])}"]
    if signals & {"cold", "cold_second"}:
        level = "超低温" if "cold_second" in signals else "低温"
        parts.append(f"{level}　综合温度 {row['temperature']:g}°（盘中暂估）")
        comp = row.get("temperature_components")
        if comp:
            parts.append(f"价格位置 {comp['position']:.1f} · RSI(14) {comp['rsi14']:.1f} · RSI(7) {comp['rsi7']:.1f}")

    if "second" in signals or "first" in signals:
        tier = "二档" if "second" in signals else "首档"
        parts.append(f"{basis}到达{tier}")
    for key, label, field, limit1, limit2, suffix in (
            ("rsi", "RSI(14)", "rsi", "rsi_max", "rsi_second_max", ""),
            ("rsi7", "RSI(7)", "rsi7", "rsi7_max", "rsi7_second_max", ""),
            ("index", f"指数 {row.get('index', '')} 日涨跌", "index_pct", "index_drop", "index_drop_second", "%")):
        deep = key + "_second" in signals
        if deep or key in signals:
            tier, limit = ("二档", limit2) if deep else ("首档", limit1)
            parts.append(f"{label} {tier}　{row[field]:.2f}{suffix} ≤ {row[limit]:g}{suffix}")
    if "drawdown" in signals:
        parts.append(f"{row['drawdown_days']} 日回撤　{abs(row['drawdown']):.2f}%（阈值 {row['drawdown_pct']:g}%）")

    if price is not None:
        state = "未到首档" if price > first else "已到二档" if second is not None and price <= second else "已到首档"
        value = "估算净值" if fund else "现价"
        parts.append(f"{value} {price:.{digits}f} {unit}" + (f" · {state}" if not {"first", "second"} & signals else ""))
        if price > first:
            delta = price - first
            parts.append(f"到首档还需跌 {delta:.{digits}f} {unit}（{delta / price:.2%}）")
    else:
        parts.append("当前估值待更新" if fund else "当前报价待更新")
        if fund and row.get("nav") is not None and row.get("nav_day"):
            parts.append(f"最近公布净值 {row['nav']:.{digits}f} 元（{row['nav_day']}）")
    targets = f"首档 {first:.{digits}f}" + (f" · 二档 {second:.{digits}f}" if second is not None else "")
    parts.append(f"{targets} {unit}")
    if row.get("premium_max") is not None and row.get("premium") is not None:
        parts.append(f"盘中溢价 {row['premium']:.2f}%（上限 {row['premium_max']:g}%）")
    elif row.get("show_premium") and price is not None:
        spread = "待更新" if row.get("premium") is None else f"{row['premium']:+.2f}%"
        parts.append(f"折溢价 {spread}（仅观察）")
    stamps = []
    if price is not None:
        stamps.append(f"{'估值' if fund else '行情'} {row['at'][11:19]}")
    if any(s.startswith("index") for s in signals) and row.get("index_at"):
        stamps.append(f"指数行情 {row['index_at'][11:19]}")
    if stamps:
        parts.append(" · ".join(stamps))
    return "\n".join(parts)


def format_alerts(events, now, source, *, preview=False, dashboard_url=None):
    now = now.astimezone(CST)
    selected = []
    for event in events:
        row = event["row"]
        signals = {s for s in row["signals"] if f"{row['code']}/{s}" in event["keys"]}
        if signals:
            selected.append((row, signals))

    def priority(item):
        signals = item[1]
        if "second" in signals:
            return 0
        if "first" in signals:
            return 1
        return 2 if any(s.endswith("_second") for s in signals) else 3

    selected.sort(key=priority)
    if not selected:
        raise ValueError("No selected events")
    rows = [row for row, _ in selected]
    all_signals = set.union(*(s for _, s in selected))
    prices = [s for _, s in selected if s & {"first", "second"}]
    has_rsi = any(s.startswith("rsi") for s in all_signals)
    has_index = any(s.startswith("index") for s in all_signals)
    if len(rows) == 1:
        subject = stock_name(rows[0])
    elif {row["code"] for row in rows} == {"sh600938", "hk00883"}:
        subject = "海油 A/H"
    else:
        subject = f"{len(rows)} 只标的"
    topics = []
    if prices:
        if len(rows) == 1:
            tier = "二档" if "second" in prices[0] else "首档"
            topics.append(("估算净值" if rows[0].get("index") else "价格") + "到" + tier)
        else:
            topics.append(f"{len(prices)} 只到价")
    if has_rsi:
        topics.append("RSI 触发")
    if all_signals & {"cold", "cold_second"}:
        topics.append("超低温" if "cold_second" in all_signals else "低温")
    if has_index:
        topics.append("指数跌幅" + ("二档" if "index_second" in all_signals else "首档"))
    if "drawdown" in all_signals:
        topics.append("回撤触发")
    if not prices and all(live_price(row, now) is not None and row["price"] > row["first"] for row in rows):
        topics.append("均未到首档" if len(rows) > 1 else "未到首档")
    title = ("版式预览｜" if preview else "") + subject + "｜" + "，".join(topics) + " · " + source
    stamp = f"北京时间 {now:%Y-%m-%d %H:%M}"
    lines = ["历史示例 · " + stamp] if preview else []
    for row, signals in selected:
        card = alert_card(row, signals, now)
        if dashboard_url and dashboard_url.startswith("https://") and signals & {"cold", "cold_second"}:
            card += "\n" + dashboard_url.rstrip("/") + "/thermometers/" + row["code"] + ".html"
        lines.append(card)
    note = "RSI 为盘中日线暂估值" if has_rsi else ""
    footer = " · ".join(filter(None, ["" if preview else stamp, note]))
    if footer:
        lines.append(footer)
    return title, lines


def mobile_html(lines):
    blocks = []
    for block in filter(None, lines):
        heading, *details = [html.escape(line) for line in block.splitlines()]
        if not details:
            if block.startswith("https://"):
                blocks.append(f'<p><a href="{heading}">打开监控看板</a></p>')
            else:
                blocks.append(f'<p style="font-size:13px;color:#4b5563">{heading}</p>')
            continue
        body = [f'<p style="margin:0 0 8px;font-size:18px"><strong>{heading}</strong></p>']
        for line in details:
            if line.startswith("https://"):
                content = f'<a href="{line}">查看温度计和历史走势</a>'
            elif line.startswith(("RSI(", "指数 ", "价格到达", "估算净值到达", "低温", "超低温")):
                content = f'<strong style="color:#134e4a">{line}</strong>'
            elif line.startswith(("行情 ", "估值 ", "指数行情 ")):
                content = f'<small style="color:#4b5563">{line}</small>'
            else:
                content = line
            body.append(f'<p style="margin:4px 0;line-height:1.5">{content}</p>')
        blocks.append('<div style="margin:12px 0;padding:12px 14px;border:1px solid #cbd5e1;'
                      'border-left:4px solid #31766c;border-radius:8px">' + "".join(body) + "</div>")
    return "".join(blocks)


def plain_body(lines, max_bytes=None):
    blocks = list(filter(None, lines))
    text = "\n\n".join(blocks)
    if not max_bytes or len(text.encode()) <= max_bytes:
        return text
    tail = "…其余内容见监控看板"
    while blocks and len(("\n\n".join([*blocks, tail])).encode()) > max_bytes:
        blocks.pop()
    return "\n\n".join([*blocks, tail])


if __name__ == "__main__":
    request = json.load(sys.stdin.buffer)
    title, lines = format_alerts(request["events"], datetime.fromisoformat(request["now"]), "本机",
                                dashboard_url=os.getenv("DASHBOARD_URL", "https://spbnb.github.io/dividend-watch/"))
    print(json.dumps({"title": title, "lines": lines, "html": mobile_html(lines)}, ensure_ascii=True))
