"""Per-instrument trailing price ranks and forward calendar-year return samples."""

from bisect import bisect_left, bisect_right, insort
from datetime import date, time


WINDOW = 250
LIMITS = (10, 25, 75, 90)
BANDS = ("超低温", "低温", "中温", "高温", "超高温")
METHOD = ("本项目的价格温度衡量标的在自身历史中的位置。用当前复权参考值与此前 250 个完整交易日比较。温度 = 100 ×"
          "（低于当前值的天数 + 等于当前值的天数 × 0.5）÷ 250。历史不足 250 日不出温度。")
STATS_NOTE = ("以有温度的历史收盘日分组，从下一个有报价日的收盘起算，至满 1、2、3 个日历年后的首个可用收盘。"
              "表中为复权序列累计收益，胜率为收益大于零的样本比例。仅计已到期样本，持有期可重叠，"
              "未扣费税。历史结果不代表未来收益。")


def band(value):
    return BANDS[bisect_left(LIMITS, value)] if value is not None else None


def rank(value, ordered):
    if len(ordered) != WINDOW:
        return None
    return round((bisect_left(ordered, value) + bisect_right(ordered, value)) * 50 / WINDOW, 1)


def closed_history(data, item, now):
    today = now.date().isoformat()
    close = time(16, 5) if item["code"].startswith("hk") else time(15, 5)
    after_close = now.time() >= close
    return [(d, v) for d, v in data.get("long:" + item["code"], [])
            if d < today or (after_close and d == today)]


def reading(item, bars, quote, now, previous, valid):
    """Only a verified current session may supply an intraday point or a new alert."""
    today = now.date().isoformat()
    if bars and bars[-1][0] == today:
        day, value = bars[-1]
        return rank(value, sorted(v for _, v in bars[-WINDOW-1:-1])), day, False
    current = None
    at = quote.get("at") or ""
    if valid and at[:10] == today and bars and bars[-1][0] == previous:
        if item.get("index") and quote.get("pct") is not None and quote["pct"] > -100:
            current = bars[-1][1] * (1 + quote["pct"] / 100)
        elif not item.get("index") and quote.get("prev_close") and quote.get("price"):
            current = bars[-1][1] * (quote["price"] / quote["prev_close"])
    if current is not None:
        return rank(current, sorted(v for _, v in bars[-WINDOW:])), at, True
    if bars:
        return rank(bars[-1][1], sorted(v for _, v in bars[-WINDOW-1:-1])), bars[-1][0], False
    return None, None, False


def historical_series(bars):
    window, series = [], []
    for i, (day, value) in enumerate(bars):
        series.append((day, value, rank(value, window)))
        if i >= WINDOW:
            window.pop(bisect_left(window, bars[i - WINDOW][1]))
        insort(window, value)
    return series


def anniversary(day, years):
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # Leap day in a non-leap anniversary year.
        return date(day.year + years, 2, 28)


def holding_stats(series):
    dates = [date.fromisoformat(day) for day, _, _ in series]
    totals = [[dict(n=0, positive=0, total=0.0) for _ in range(3)] for _ in BANDS]
    for i, (_, _, value) in enumerate(series[:-1]):
        if value is None:
            continue
        group = totals[bisect_left(LIMITS, value)]
        entry = i + 1
        for years, cell in enumerate(group, 1):
            end = bisect_left(dates, anniversary(dates[entry], years), lo=entry + 1)
            if end == len(dates):
                break
            change = series[end][1] / series[entry][1] - 1
            cell["n"] += 1
            cell["positive"] += int(change > 0)
            cell["total"] += change
    return [[{"n": c["n"], "mean": round(100*c["total"]/c["n"], 2) if c["n"] else None,
              "win": round(100*c["positive"]/c["n"], 2) if c["n"] else None} for c in group] for group in totals]


def page_data(row, bars, source):
    series = historical_series(bars)
    at = row.get("temperature_at")
    note = "盘中暂估，收盘后可能变化。" if row.get("temperature_live") else "最近可用的历史收盘观测。"
    if row.get("temperature") is None:
        note = "可用历史不足或行情缺失，暂无温度读数。"
    return {**{key: row.get(key) for key in ("code", "name", "type", "currency", "temperature")},
            "at": at, "band": band(row.get("temperature")), "price": row.get("price"), "price_at": row.get("at"),
            "bands": BANDS, "thresholds": LIMITS, "series": series, "stats": holding_stats(series),
            "method": METHOD, "stats_note": STATS_NOTE, "asof_note": note, "source": source}
