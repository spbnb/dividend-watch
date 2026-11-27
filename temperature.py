"""Composite market temperature and forward calendar-year return samples."""

from bisect import bisect_left, bisect_right, insort
from datetime import date, datetime, time


WINDOW = 250
LIMITS = (10, 25, 75, 90)
BANDS = ("超低温", "低温", "中温", "高温", "超高温")
METHOD = ("综合温度 = 50% × 价格位置 + 35% × RSI(14) + 15% × RSI(7)。三项均为 0–100 分。"
          "价格位置取相对此前 250 个交易日的百分位，同值计半。两个 RSI 用同一复权日线按 Wilder 法平滑。"
          "组合分数保留一位小数后分档，缺少此前 250 日收盘时不出分。三项都来自价格序列，反映相对位置与动量。"
          "10°、25° 是综合分数的提醒线。两个 RSI 都衡量动量，分红率单列供对照。权重是自定义观察规则，未经收益优化。")
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
    return closed_bars(data.get("long:" + item["code"], []), now,
                       "HK" if item["code"].startswith("hk") else "CN")


def closed_bars(bars, now, market):
    today = now.date().isoformat()
    close = time(16, 5) if market == "HK" else time(15, 5)
    after_close = now.time() >= close
    return [(d, v) for d, v in bars
            if d < today or (after_close and d == today)]


def rsi_series(closes, period):
    """Wilder RSI, seeded with simple average gains and losses over the first period."""
    previous = None
    gain = loss = 0.0
    for i, price in enumerate(closes):
        if previous is not None:
            delta = price - previous
            up, down = max(delta, 0), max(-delta, 0)
            if i <= period:
                gain += up
                loss += down
                if i == period:
                    gain /= period
                    loss /= period
            else:
                gain = (gain * (period - 1) + up) / period
                loss = (loss * (period - 1) + down) / period
        previous = price
        yield None if i < period else (100 * gain / (gain + loss) if gain + loss else 50.0)


def wilder_rsi(closes, period=14):
    last = None
    for last in rsi_series(closes, period):
        pass
    return last


def composite(position, rsi14, rsi7):
    if None in (position, rsi14, rsi7):
        return None
    return round(0.50 * position + 0.35 * rsi14 + 0.15 * rsi7, 1)


def reading(item, bars, quote, now, previous, valid):
    """Only a verified current session may supply an intraday point or a new alert."""
    today = now.date().isoformat()
    current = None
    at = quote.get("at") or ""
    close = time(16) if item["code"].startswith("hk") else time(15)
    # A lagging adjusted daily feed can be extended with a verified close-to-close return.
    official_nav = bool(item.get("index") and at == today)
    closing_quote = (not item.get("index") and len(at) > 10 and close <= datetime.fromisoformat(at).time()
                     and datetime.fromisoformat(at) <= now and quote.get("volume") != 0)
    final = now.time() >= close and (official_nav or closing_quote)
    if (valid or final) and at[:10] == today and bars and bars[-1][0] != today and bars[-1][0] == previous:
        if item.get("index") and quote.get("pct") is not None and quote["pct"] > -100:
            current = bars[-1][1] * (1 + quote["pct"] / 100)
        elif not item.get("index") and quote.get("prev_close") and quote.get("price"):
            current = bars[-1][1] * (quote["price"] / quote["prev_close"])
    live = current is not None and not final
    points = [*bars, (today, current)] if current is not None else bars
    closes = [v for _, v in points]
    components = {}
    value = ytd = None
    if len(closes) > WINDOW:
        position = rank(closes[-1], sorted(closes[-WINDOW-1:-1]))
        r14, r7 = wilder_rsi(closes, 14), wilder_rsi(closes, 7)
        components = {"position": position, "rsi14": r14, "rsi7": r7}
        value = composite(position, r14, r7)
    if points and points[-1][0][:4] == today[:4]:
        base = next((v for d, v in reversed(bars) if d[:4] == str(now.year - 1)), None)
        if base:
            ytd = 100 * (closes[-1] / base - 1)
    change = 100 * (closes[-1] / closes[-2] - 1) if len(closes) >= 2 else None
    return {"temperature": value, "temperature_components": components, "temperature_band": band(value),
            "rank_day_pct": change, "reference_close": current if final else None,
            "temperature_at": at if live else (points[-1][0] if points else None),
            "temperature_live": live, "ytd_pct": ytd}


def historical_series(bars):
    window, series = [], []
    closes = [v for _, v in bars]
    for i, ((day, value), r14, r7) in enumerate(zip(bars, rsi_series(closes, 14), rsi_series(closes, 7))):
        series.append((day, value, composite(rank(value, window), r14, r7)))
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
    if row.get("reference_is_index") and not row.get("reading_fallback") and row.get("reference_close") is not None:
        note = "已用同币种价格指数的收盘涨跌幅暂估，历史曲线截至最新已公布指数日线。"
    if row.get("temperature") is None:
        note = "可用历史不足或行情缺失，暂无温度读数。"
    index_history = row.get("reference_is_index", False)
    note_stats = STATS_NOTE
    if index_history:
        basis = {"价格指数": "价格指数点位变化，不含现金分红", "全收益指数": "全收益指数累计收益，含分红再投资",
                 "净收益指数": "净收益指数累计收益，按指数规则扣除股息相关税费"}.get(row["reference_kind"], row["reference_kind"])
        note_stats = STATS_NOTE.replace("复权序列累计收益", basis)
    stats = holding_stats(series)
    # Only derived indicators and aggregate outcomes are published for an index.
    if index_history:
        series = [(day, None, value) for day, _, value in series]
    return {**{key: row.get(key) for key in ("code", "name", "type", "currency", "temperature", "yield_pct")},
            **{k: v for k, v in row.items() if k.startswith(("benchmark_", "reference_", "reading_")) and k != "reference_close"},
            "at": at, "band": band(row.get("temperature")), "components": row.get("temperature_components") or {},
            "price": row.get("price"), "price_at": row.get("at"),
            "bands": BANDS, "thresholds": LIMITS, "series": series, "stats": stats,
            "method": METHOD.replace("同一复权日线", "同一组日收盘序列") if row.get("benchmark_code") else METHOD,
            "stats_note": note_stats, "asof_note": note, "source": source}
