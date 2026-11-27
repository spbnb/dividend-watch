"""Compare dividend valuation and temperature signals using published index series."""

import argparse
import calendar
import json
import re
import statistics
import tempfile
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from csi_reference import SERIES, csi_bars
from temperature import anniversary, historical_series, rsi_series


CATALOGUE = "https://www.csindex.com.cn/csindex-home/research/queryResearchByVo"
MOF = "https://yield.chinabond.com.cn/cbweb-czb-web/czb/historyQuery"
INDICES = {"000922": "中证红利指数", "H30269": "中证红利低波动指数"}
RULES = ("无条件", "原温度 ≤25°", "利差前 25%", "利差前 25% 且 RSI(14) ≤35")
ONE_WAY_COST = 0.001
SPLIT = "2023-01-01"
PERCENT = r"[-+]?\d+(?:\.\d+)?\s*[%％]"
DIVIDEND_ROW = re.compile(r"中证红利\s+(?:[\d,.]+\s+){4}(?:" + PERCENT + r"\s*){7}(" + PERCENT + ")")


def request(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(quote(url, safe=":/?&=%"), data=data, headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
    with urlopen(req, timeout=35) as response:
        return response.read()


def cached(folder, name, loader):
    path = folder / (name + ".json")
    if path.exists():
        result = json.loads(path.read_text(encoding="utf8"))
        if not isinstance(result, dict) or "skip" not in result or result.get("parser") == 2:
            return result
    result = loader()
    if not result:
        raise ValueError("Empty data: " + name)
    temp = path.with_suffix(".part")
    temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf8")
    temp.replace(path)
    return result


def reports():
    filters = {"updateFreq": ["monthly"], "reportSubclass": [], "report": [], "analysis": ["index_running"],
               "theme": [], "publishDateRange": [], "lang": "cn"}
    rows = []
    page = 1
    while True:
        result = json.loads(request(CATALOGUE, {"pager": {"pageNum": page, "pageSize": 50}, "filter": filters}))
        if not result.get("success") or not result.get("data"):
            raise ValueError("CSI monthly research catalogue unavailable")
        rows.extend(result["data"])
        if len(rows) >= result["total"]:
            return rows
        page += 1


def report_value(entry):
    import fitz  # PyMuPDF, only needed for the research PDF archive.
    period = re.search(r"(20\d\d)\s*年\s*(\d{1,2})\s*月", entry["fileName"])
    if not period or entry["fileType"].lower() != "pdf":
        return {"skip": entry["fileName"], "parser": 2}
    year, month = map(int, period.groups())
    month_end = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    candidates = set()
    with fitz.open(stream=request(entry["filePath"]), filetype="pdf") as doc:
        for page in doc:
            # Some files concatenate the previous row's last percentage and the index
            # name into a single PDF word. The text order still preserves all 12 cells.
            for match in DIVIDEND_ROW.finditer(page.get_text()):
                candidates.add(float(match[1].rstrip("%％").strip()))
            words = page.get_text("words")
            for hit in words:
                if hit[4] != "中证红利":
                    continue
                # On the same table row: close, 3 capital/distribution totals, 6 returns,
                # unweighted average constituent yield and the final index dividend yield.
                row = sorted((w for w in words if w[0] > hit[0] and abs((w[1]+w[3]-hit[1]-hit[3])/2) < 3),
                             key=lambda w: w[0])
                cells = re.findall(r"[-+]?\d+(?:\.\d+)?%", "".join(w[4] for w in row).replace("％", "%"))
                if len(cells) == 8:
                    candidates.add(float(cells[-1][:-1]))
    if len(candidates) != 1:
        return {"skip": entry["fileName"], "reason": "unverified dividend table row", "parser": 2}
    return {"month_end": month_end, "published": entry["fileDate"], "rate": candidates.pop(), "url": entry["filePath"]}


def bond_half(start, end):
    query = urlencode({"startDate": start, "endDate": end, "gjqx": "0", "locale": "cn_ZH", "qxmc": "1"})
    rows = json.loads(request(MOF + "?" + query, {})).get("heList") or []
    return sorted((r["workTime"], float(r["tenYear"])) for r in rows if r.get("tenYear") not in (None, ""))


def daily_proxy(price, total, bonds):
    price, total = dict(price), dict(total)
    days = sorted(set(price).intersection(total))
    signal = []
    for i, d in enumerate(days):
        if d not in bonds:
            continue
        target = date.fromisoformat(d) - timedelta(days=365)
        j = bisect_right(days, target.isoformat(), hi=i) - 1
        if j < 0 or (target - date.fromisoformat(days[j])).days > 14:
            continue
        a = days[j]
        contribution = 100 * ((total[d] / total[a]) / (price[d] / price[a]) - 1)
        signal.append((d, contribution - bonds[d]))
    return signal


def percentile(value, prior):
    return 100 * (sum(x < value for x in prior) + .5 * sum(x == value for x in prior)) / len(prior)


def proxy_scores(spreads, sessions):
    days = [r[0] for r in spreads]
    scores = {}
    for d in sessions:
        i = bisect_left(days, d) - 1  # Both index and yield must already have closed.
        if i < 0 or (date.fromisoformat(d) - date.fromisoformat(days[i])).days > 14:
            continue
        start = anniversary(date.fromisoformat(days[i]), -3).isoformat()
        j = bisect_left(days, start)
        if days[0] <= start and i - j >= 600:
            scores[d] = percentile(spreads[i][1], [v for _, v in spreads[j:i]])
    return scores


def monthly_scores(entries, bonds, sessions):
    bond_days = sorted(bonds)
    monthly = []
    for r in entries:
        i = bisect_right(bond_days, r["month_end"]) - 1
        if i < 0 or (date.fromisoformat(r["month_end"]) - date.fromisoformat(bond_days[i])).days > 10:
            continue
        monthly.append((r["month_end"], r["published"], r["rate"] - bonds[bond_days[i]]))
    scores = {}
    for d in sessions:
        known = {month: spread for month, published, spread in sorted(monthly, key=lambda r: r[1]) if published < d}
        if not known:
            continue
        obs = max(known)
        start = anniversary(date.fromisoformat(obs), -3).isoformat()
        prior = [v for m, v in known.items() if start <= m < obs]
        if min(known) <= start and len(prior) >= 30 and (date.fromisoformat(d) - date.fromisoformat(obs)).days <= 62:
            scores[d] = percentile(known[obs], prior)
    return scores


def evaluate(bars, score, start, end):
    days = [d for d, _ in bars]
    price = dict(bars)
    temp = {d: t for d, _, t in historical_series(bars)}
    rsi = dict(zip(days, rsi_series([p for _, p in bars], 14)))
    candidates = [[] for _ in RULES]
    universe = []
    for i, d in enumerate(days[:-1]):
        if d < start or d not in score or temp[d] is None or rsi[d] is None:
            continue
        entry = days[i + 1]
        anniversary_day = anniversary(date.fromisoformat(entry), 1).isoformat()
        end_i = bisect_left(days, anniversary_day)
        if end_i >= len(days):
            continue
        exit_day = days[end_i]
        if exit_day >= end:
            continue
        ret = 100 * (price[exit_day] * (1 - ONE_WAY_COST) / (price[entry] * (1 + ONE_WAY_COST)) - 1)
        universe.append(d)
        value = score[d] >= 75
        for group, hit in zip(candidates, (True, temp[d] <= 25, value, value and rsi[d] <= 35)):
            if hit:
                group.append((entry, exit_day, ret))
    out = []
    for rule, matches in zip(RULES, candidates):
        last_exit = ""
        kept = []
        for entry, end, ret in matches:
            if entry > last_exit:
                kept.append(ret)
                last_exit = end
        out.append((rule, len(matches), kept))
    return universe, out


def table(name, history, scores):
    lines = [f"### {name}", "", "| 期间 | 规则 | 符合天数 | 不重叠笔数 | 平均净收益 | 胜率 |",
             "| --- | --- | ---: | ---: | ---: | ---: |"]
    coverage = []
    for period, start, end in (("全期", "", "9999"), ("前期", "", SPLIT), ("后期", SPLIT, "9999")):
        days, results = evaluate(history, scores, start, end)
        if days:
            coverage.append(f"{period} {days[0]} 至 {days[-1]}（{len(days)} 个可评估信号日）")
        for rule, matches, returns in results:
            perf = (f"{statistics.mean(returns):+.2f}% | {100 * sum(v > 0 for v in returns)/len(returns):.1f}%"
                    if returns else "— | —")
            lines.append(f"| {period} | {rule} | {matches} | {len(returns)} | {perf} |")
    lines.extend(["", "。".join(coverage) + "。"] if coverage else ["", "缺少持有满一年的指数观察。"])
    return lines + [""]


def run(cutoff, cache):
    cache.mkdir(parents=True, exist_ok=True)
    catalogue = cached(cache, "monthly-archive-" + str(cutoff), reports)
    def extract(report):
        key = Path(report["filePath"]).stem
        return cached(cache, "yield-" + key, lambda: report_value(report))
    with ThreadPoolExecutor(max_workers=3) as pool:
        parsed = list(pool.map(extract, (r for r in catalogue if r["fileDate"] <= str(cutoff))))
    entries = sorted((r for r in parsed if "rate" in r and r["month_end"] <= r["published"]), key=lambda r: r["published"])
    if not entries:
        raise ValueError("No verified published monthly CSI yields")
    bonds = {}
    for y in range(2014, cutoff.year + 1):
        for m, finish in ((1, date(y,6,30)), (7, date(y,12,31))):
            start, end = date(y,m,1), min(finish,cutoff)
            if start <= end:
                bonds.update(cached(cache, f"bond-{start}-{end}", lambda: bond_half(start,end)))
    lines = ["# 两条红利指数的估值信号回测", "", f"数据截止 {cutoff}。实验使用中证红利、中证红利低波动的人民币价格指数和全收益指数。"
             "温度、RSI 和持有期收益均来自各自的全收益指数。", "",
             f"**真实股息率。** 中证归档的 {len(parsed)} 份月报中，可核验 {len(set(r['month_end'] for r in entries))} 个不同月末，"
             f"从 {min(r['month_end'] for r in entries)} 至 {max(r['month_end'] for r in entries)}。"
             "取中证红利指数在月报红利表中的末列「指数股息率」，减去同月末最近的财政部 10 年期国债收益率。"
             "月报发布日期过后才使用。每次观察最多沿用至该月末后 62 天。只用当时已发布的此前 3 年数据计算百分位，至少需要 30 个月。",
             "", "**分红贡献代理。** 对每条指数，用过去 365 天的全收益指数涨幅因子除以同币种价格指数涨幅因子再减一。"
             "这衡量期间已实现的指数分红再投资贡献，和月末公布的指数股息率定义不同。"
             f"同期取得财政部国债曲线 {len(bonds)} 个日观察。从分红贡献减同日 10 年国债收益率，取此前完整 3 年、至少 600 个配对日的百分位。"
             "信号日只引用此前已收盘的指数和国债，跨休市间隔最长 14 天。", "",
             "**信号与持有期。** 对每个指数和每种估值口径，四条规则共享同一组信号日。只用指数正式发布后的日线，剔除发布前回溯值。"
             "原温度按 50% 的 250 日指数位置、35% 的 RSI(14)、15% 的 RSI(7) 计算。估值利差百分位 ≥75 触发，叠加规则另要求同一指数 RSI(14) ≤35。"
             "所有规则在信号后的首个指数收盘点位模拟入场，至满一年后的首个指数收盘点位退出。各条规则从最早合格日开始，下一笔须在上一笔退出后入场。"
             "按指数点位变化模拟收益，每次入场、退出分别扣 0.10% 的假设跟踪成本。实际产品会有额外的费用、滑点和跟踪误差。", "",
             "**分期。** 前期仅纳入 2022 年末前已退出的观察，后期从 2023 年的首个信号日重新开始。两段各自统计互不重叠的一年期。"
             "全期按连续时间重新选择入场，笔数可与两段之和不同。表中的平均百分比衡量持有期净收益，空仓时间只在主策略实验的组合年化收益中计入。", ""]
    for code, name in INDICES.items():
        price_code, total_code, _, base, launch = SERIES[code]
        def bars(symbol):
            history = cached(cache, f"csi-{symbol}-{cutoff}", lambda: csi_bars(symbol,base,cutoff))
            return [r for r in history if launch <= r[0] <= cutoff.isoformat()]
        total = bars(total_code)
        sessions = [d for d, _ in total]
        proxy = daily_proxy(bars(price_code), total, bonds)
        title = f"{name}（价格 {price_code} / 全收益 {total_code}）"
        if code == "000922":
            lines.extend(table(f"{title} · 月报真实股息率", total, monthly_scores(entries,bonds,sessions)))
        lines.extend(table(f"{title} · 分红贡献代理", total, proxy_scores(proxy,sessions)))
    lines += ["不重叠笔数是各条规则实际走完的一年持有期数。不同规则的入场日期可能不同，两条指数的样本股也可能重叠，合并笔数会夸大有效样本。"
              "胜率和均值随行情区间而变。", "",
              f"来源包括 [中证指数研报目录](https://www.csindex.com.cn/#/research/all)、[第一期可核验月报]({entries[0]['url'].replace(' ', '%20')})、"
              f"[最近一期可核验月报]({entries[-1]['url'].replace(' ', '%20')})、[财政部国债曲线](https://yield.chinabond.com.cn/cbweb-czb-web/czb/showHistory?locale=cn_ZH)、"
              "[中证红利](https://www.csindex.com.cn/#/indices/family/detail?indexCode=000922)、"
              "[中证红利低波](https://www.csindex.com.cn/#/indices/family/detail?indexCode=H30269)。",
              "", f"复现时安装 PyMuPDF，再运行 `python research_value.py --as-of {cutoff}`。"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path(tempfile.gettempdir()) / "dividend-watch-value-research")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.as_of,args.cache_dir)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(report,encoding="utf8")
    else:
        print(report)
