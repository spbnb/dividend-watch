"use strict";

const snapshot = JSON.parse(document.getElementById("snapshot").textContent);
const byId = id => document.getElementById(id);
const hits = new Set(["first", "second", "signal"]);
const guards = new Set(["blocked", "verify"]);
const types = ["全部", "指数", "A 股", "港股", "场外基金"];
const view = {type: "全部", scope: "", expanded: new Set(), days: 20, cursor: null};
const fmt = (n, digits = 2) => n == null ? "—" : n.toFixed(digits);
const pct = n => n == null ? "—" : (n > 0 ? "+" : "") + n.toFixed(2) + "%";
const dateText = iso => iso ? iso.replace("T", " ").slice(0, 16) : "—";
const signedClass = value => value > 0 ? "up" : value < 0 ? "down" : "";
const digits = r => r.type === "场外基金" ? 4 : 2;
const observedDay = r => Number.isFinite(r.temperature) ? (r.temperature_at || "").slice(0, 10) : "";
const rankingDay = snapshot.rows.map(observedDay).sort().at(-1) || "";
const comparable = r => Number.isFinite(r.temperature) && (!rankingDay || observedDay(r) === rankingDay);
const dayChange = r => Object.hasOwn(r, "rank_day_pct") ? r.rank_day_pct : r.pct;
const rateText = value => Number.isFinite(value) ? fmt(value) + "%" : "—";
const indexRate = row => row.reference_yield_approx && Number.isFinite(row.reference_yield)
  ? "约" + row.reference_yield + "%" : rateText(row.reference_yield);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function displayCode(r) {
  if (r.type === "指数") return r.code;
  if (r.index) return r.code + " · 场外";
  return r.code.slice(2) + "." + r.code.slice(0, 2).toUpperCase();
}

function details(r) {
  const index = r.type === "指数";
  const values = index ? [
    ["价格指数点位", Number.isFinite(r.price) ? fmt(r.price) + " 点" : "—"],
    ["点位计价币种", r.currency || "—"],
    ["点位时间 · 北京", Number.isFinite(r.price) ? dateText(r.at) : "—"],
    ["本次读数来源", r.reading_basis || "—"],
    ["历史统计序列", [r.reference_kind, r.reference_symbol, r.reference_currency, r.reference_provider].filter(Boolean).join(" · ") || "—"],
    ["指数股息率", [indexRate(r), r.reference_yield_at, r.reference_yield_method].filter(Boolean).join(" · ")]
  ] : [
    ["最新价 / 净值", fmt(r.price, digits(r)) + " " + r.currency], ["距价格首档", pct(r.gap)],
    ["价格时间 · 北京", dateText(r.at)],
    ["首档 / 二档", fmt(r.first, digits(r)) + (r.second ? " / " + fmt(r.second, digits(r)) : "") + " " + r.currency]
  ];
  values.push(["提醒状态",r.label], ["温度观测 · 北京", dateText(r.temperature_at)], ["行情来源",r.source || "行情暂缺"]);
  const limits = (first, second, unit = "") => first == null ? "" :
    " · 首档 ≤ " + first + unit + (second == null ? "" : " / 二档 ≤ " + second + unit);
  if (index || r.rsi_max != null || r.drawdown_days) {
    values.push(["日线 RSI(14)", fmt(r.rsi, 2) + limits(r.rsi_max, r.rsi_second_max)]);
  }
  if (index || r.rsi7_max != null) values.push(["日线 RSI(7)", fmt(r.rsi7, 2) + limits(r.rsi7_max, r.rsi7_second_max)]);
  if (r.macd != null) values.push(["MACD 柱 (12,26,9)", fmt(r.macd, 4)]);
  if (r.drawdown_days) values.push([r.drawdown_days + " 日收盘回撤", pct(r.drawdown) + " · 阈值 < −" + r.drawdown_pct + "%"]);
  if (r.yield_min != null) values.push(["近 365 天现金股息率", (r.yield_pct == null ? "—" : fmt(r.yield_pct) + "%") + " · 观察线 " + r.yield_min + "%"]);
  if (r.type === "场外基金" && r.index) values.push(["最近公布净值", fmt(r.nav, 4) + " · " + (r.nav_day || "—")],
    ["跟踪指数 " + r.index + " 日涨跌", pct(r.index_pct) + limits(r.index_drop, r.index_drop_second, "%")],
    ["指数行情时间 · 北京", dateText(r.index_at)]);
  if (r.reasons?.length) values.push(["命中条件", r.reasons.join("、")]);
  if (r.observe) values.push(["通知方式", r.temperature_alerts ? "温度分档提醒，其他条件仅观察" : "仅在看板观察"]);
  const dl = el("dl", null, "detail");
  for (const [name, value] of values) {
    const node = el("div");
    node.append(el("dt", name), el("dd", value));
    dl.append(node);
  }
  return dl;
}

function thermoURL(r) {
  return "thermometers/" + encodeURIComponent(r.code) + ".html";
}

function showExtremes() {
  const priced = snapshot.rows.filter(comparable);
  byId("rank-date").textContent = rankingDay ? "温度观测 " + rankingDay + " · 同日 " + priced.length + "/" + snapshot.rows.length + " 只" : "温度观测日期待更新";
  for (const [id, pick] of [["hottest", Math.max], ["coldest", Math.min]]) {
    const link = byId(id);
    if (!priced.length) { link.textContent = "温度待更新"; continue; }
    const score = pick(...priced.map(r => r.temperature));
    const tied = priced.filter(r => r.temperature === score);
    const r = tied[0];
    link.href = thermoURL(r);
    link.replaceChildren(el("b", r.name), el("strong", fmt(score, 1) + "°"));
    if (tied.length > 1) link.append(el("small", "等 " + tied.length + " 只并列"));
  }
}

function makeRow(r) {
  const shades = {超低温:"ice",低温:"cool",中温:"neutral",高温:"warm",超高温:"hot"};
  const shade = shades[r.temperature_band] || "unknown";
  const tr = el("tr", null, "row " + shade);
  if (shade === "neutral" && r.temperature !== 50) tr.classList.add(r.temperature < 50 ? "cool-side" : "warm-side");
  const first = el("td"), link = el("a", r.name, "name-link");
  link.href = thermoURL(r);
  link.setAttribute("aria-label", r.name + "温度计");
  const index = r.type === "指数";
  first.append(link, el("small", displayCode(r), "code"));
  if (index) {
    const kind = r.reference_kind || "指数";
    const estimate = (r.reading_basis || "").includes("暂估");
    const label = r.temperature_live ? "盘中暂估" : estimate ? "收盘暂估" : kind;
    const basis = el("small", label, "basis-label");
    basis.title = r.reading_basis || kind;
    first.append(basis);
  }
  const move = el("td", null, "num move");
  const change = dayChange(r);
  move.append(el("strong", pct(change), signedClass(change)), el("small", pct(r.ytd_pct), signedClass(r.ytd_pct)));
  const changeAt = Object.hasOwn(r, "rank_day_pct") ? r.temperature_at : r.at;
  move.title = [index ? r.reading_basis : "", "当日涨跌日期 " + (changeAt || "待更新").slice(0,10)].filter(Boolean).join(" · ");
  const divYield = el("td", index ? indexRate(r) : rateText(r.yield_pct), "num");
  divYield.append(el("small", index ? r.reference_yield_at || "指数" : "近365日", "rate-basis"));
  divYield.title = index ? [r.name, "指数股息率", r.reference_yield_at].filter(Boolean).join(" · ") : "近 365 日现金分红率";
  const temp = el("td", r.temperature == null ? "—" : fmt(r.temperature, 1) + "°", "num temp");
  if (Number.isFinite(r.temperature) && rankingDay && !comparable(r)) {
    const date = observedDay(r);
    const stamp = el("small", date ? date.slice(5) : "日期待核", "older-date");
    stamp.title = date ? "历史观测 " + date : "温度观测日期待核";
    temp.append(stamp);
  }
  const band = el("td");
  band.append(el("b", r.temperature_band || "—", "band-name"));
  const button = el("button", "行情", "quote-toggle");
  const detailId = "detail-" + r.code;
  button.setAttribute("aria-controls", detailId);
  button.setAttribute("aria-label", r.name + "行情与触发条件");
  band.append(button);
  tr.append(first, move, divYield, temp, band);
  const extra = el("tr", null, "row-details");
  extra.id = detailId;
  const cell = el("td");
  cell.colSpan = 5;
  cell.append(details(r));
  extra.append(cell);
  function toggle(open) {
    extra.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    tr.classList.toggle("open", open);
    if (open) view.expanded.add(r.code); else view.expanded.delete(r.code);
  }
  toggle(view.expanded.has(r.code));
  button.onclick = () => toggle(extra.hidden);
  return [tr, extra];
}

function redraw() {
  const query = byId("search").value.trim().toLowerCase();
  let rows = snapshot.rows.filter(r => (view.type === "全部" || view.type === r.type) &&
    (!view.scope || (view.scope === "hits" ? hits : guards).has(r.status)) &&
    (r.name + r.code + displayCode(r)).toLowerCase().includes(query));
  const sort = byId("sort").value;
  const rank = r => hits.has(r.status) ? 0 : guards.has(r.status) ? 1 : 2;
  if (sort === "hot" || sort === "cold") {
    const sign = sort === "hot" ? -1 : 1;
    rows.sort((a, b) => {
      if (a.temperature == null || b.temperature == null) return Number(a.temperature == null) - Number(b.temperature == null);
      if (rankingDay && observedDay(a) !== observedDay(b)) return observedDay(b).localeCompare(observedDay(a));
      return sign * (a.temperature - b.temperature) || a.code.localeCompare(b.code);
    });
  } else if (sort === "gap") rows.sort((a, b) => (a.gap ?? Infinity) - (b.gap ?? Infinity));
  else if (sort === "change") rows.sort((a, b) => (dayChange(b) ?? -Infinity) - (dayChange(a) ?? -Infinity));
  else if (sort === "status") rows.sort((a, b) => rank(a) - rank(b));
  byId("rows").replaceChildren(...rows.flatMap(makeRow));
  byId("empty").hidden = rows.length > 0;
  byId("visible-count").textContent = rows.length + " 条";
  byId("clear-scope").hidden = !view.scope;
  byId("clear-scope").textContent = view.scope === "hits" ? "清除「触发提醒」筛选 ×" : "清除「待核验」筛选 ×";
  document.querySelectorAll("[data-scope]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.scope === view.scope)));
  document.querySelectorAll("[data-type]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.type === view.type)));
}

function drawChart() {
  const canvas = byId("chart"), ctx = canvas.getContext("2d");
  const data = snapshot.series.slice(-view.days);
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!ctx || !width || !height) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  byId("chart-start").textContent = data.length ? data[0][0] : "";
  byId("chart-end").textContent = data.length ? data[data.length - 1][0] : "";
  byId("chart-caption").textContent = data.length < 2 ? "暂缺历史走势" : data.length + " 个交易日";
  const tip = byId("chart-tip");
  tip.hidden = true;
  if (data.length < 2) return;
  const prices = data.map(d => d[1]);
  const lo = Math.min(...prices), hi = Math.max(...prices);
  const x = i => 4 + (width - 8) * i / (prices.length - 1);
  const y = i => hi === lo ? height / 2 : 6 + (height - 12) * (hi - prices[i]) / (hi - lo);
  ctx.strokeStyle = "#517266";
  ctx.setLineDash([2, 5]);
  for (const h of [0.25, 0.75]) {
    ctx.beginPath(); ctx.moveTo(0, height * h); ctx.lineTo(width, height * h); ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.beginPath();
  prices.forEach((_, i) => i ? ctx.lineTo(x(i), y(i)) : ctx.moveTo(x(i), y(i)));
  ctx.strokeStyle = "#d2ed87";
  ctx.lineWidth = 2.1; ctx.lineJoin = "round"; ctx.stroke();
  ctx.lineTo(x(prices.length - 1), height); ctx.lineTo(x(0), height); ctx.closePath();
  const fill = ctx.createLinearGradient(0, 0, 0, height);
  fill.addColorStop(0, "#b6dd6c47"); fill.addColorStop(1, "#b6dd6c02");
  ctx.fillStyle = fill; ctx.fill();
  const i = view.cursor == null ? prices.length - 1 : Math.min(view.cursor, prices.length - 1);
  ctx.beginPath(); ctx.arc(x(i), y(i), 3.2, 0, 2 * Math.PI); ctx.fillStyle = "#e7f7af"; ctx.fill();
  if (view.cursor != null) {
    tip.hidden = false;
    tip.value = data[i][0] + " · " + fmt(prices[i]);
    tip.style.left = Math.max(0, Math.min(width - tip.offsetWidth, x(i) - tip.offsetWidth / 2)) + "px";
  }
  canvas.setAttribute("aria-label", "上证指数 " + data[0][0] + " 至 " + data.at(-1)[0] +
    "，区间最低 " + fmt(lo) + "，最高 " + fmt(hi) + "。可用左右方向键查看点位。");
}

byId("updated").textContent = "最近巡检  " + dateText(snapshot.generated) + "（北京时间）";
byId("session").textContent = Object.values(snapshot.session).some(Boolean) ? "交易时段快照" : "最近行情快照";
byId("group-count").textContent = snapshot.groups;
byId("row-count").textContent = snapshot.rows.length + " 条报价";
byId("hit-count").textContent = snapshot.rows.filter(r => hits.has(r.status)).length;
byId("guard-count").textContent = snapshot.rows.filter(r => guards.has(r.status)).length;
const indexQuote = snapshot.benchmarks.CN;
byId("index-price").textContent = indexQuote ? indexQuote.price.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}) : "—";
byId("index-change").textContent = pct(indexQuote?.pct);
showExtremes();
for (const type of types.filter(t => t === "全部" || snapshot.rows.some(r => r.type === t))) {
  const b = el("button", type);
  b.dataset.type = type;
  b.onclick = () => { view.type = type; redraw(); };
  byId("filters").append(b);
}
document.querySelectorAll("[data-scope]").forEach(b => {
  b.onclick = () => {
    view.scope = view.scope === b.dataset.scope ? "" : b.dataset.scope;
    view.type = "全部"; byId("search").value = ""; redraw(); byId("list").scrollIntoView();
  };
});
byId("clear-scope").onclick = () => { view.scope = ""; redraw(); };
byId("reset").onclick = () => { view.type = "全部"; view.scope = ""; byId("search").value = ""; redraw(); };
byId("search").oninput = redraw;
byId("sort").onchange = redraw;
byId("rules-open").onclick = () => byId("rules").showModal();
byId("rules-close").onclick = () => byId("rules").close();
byId("rules").onclick = e => { if (e.target === byId("rules")) { const r = e.target.getBoundingClientRect();
  if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) e.target.close(); } };
document.querySelectorAll("[data-days]").forEach(b => {
  b.onclick = () => {
    view.days = Number(b.dataset.days); view.cursor = null;
    document.querySelectorAll("[data-days]").forEach(tab => tab.setAttribute("aria-pressed", String(tab === b)));
    drawChart();
  };
});
const chart = byId("chart");
chart.onpointermove = e => {
  const n = Math.min(view.days, snapshot.series.length);
  if (!n) return;
  view.cursor = Math.max(0, Math.min(n - 1, Math.round((e.clientX - chart.getBoundingClientRect().left) / chart.clientWidth * (n - 1))));
  drawChart();
};
chart.onpointerleave = chart.onblur = () => { view.cursor = null; drawChart(); };
chart.onkeydown = e => {
  if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
  e.preventDefault();
  const end = Math.min(view.days, snapshot.series.length) - 1;
  view.cursor = Math.min(end, Math.max(0, (view.cursor ?? end) + (e.key === "ArrowRight" ? 1 : -1)));
  drawChart();
};
redraw();
drawChart();
new ResizeObserver(drawChart).observe(chart.parentElement);
