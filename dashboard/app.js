"use strict";

const snapshot = JSON.parse(document.getElementById("snapshot").textContent);
const byId = id => document.getElementById(id);
const hits = new Set(["first", "second", "signal"]);
const guards = new Set(["blocked", "verify"]);
const types = ["全部", "ETF", "A 股", "港股", "场外基金"];
const view = {type: "全部", scope: "", expanded: new Set(), days: 20, cursor: null};
const fmt = (n, digits = 2) => n == null ? "—" : n.toFixed(digits);
const pct = n => n == null ? "—" : (n > 0 ? "+" : "") + n.toFixed(2) + "%";
const dateText = iso => iso ? iso.replace("T", " ").slice(0, 16) : "—";
const signedClass = value => value > 0 ? "up" : value < 0 ? "down" : "";
const digits = r => r.index ? 4 : r.type === "ETF" ? 3 : 2;

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function displayCode(r) {
  if (r.index) return r.code + " · 场外";
  return r.code.slice(2) + "." + r.code.slice(0, 2).toUpperCase();
}

function details(r) {
  const values = [
    ["数据来源", r.source || "行情暂缺"], ["价格时间 · 北京", dateText(r.at)],
    ["首档 / 二档", fmt(r.first, digits(r)) + (r.second ? " / " + fmt(r.second, digits(r)) : "") + " " + r.currency]
  ];
  const limits = (first, second, unit = "") => first == null ? "" :
    " · 首档 ≤ " + first + unit + (second == null ? "" : " / 二档 ≤ " + second + unit);
  if (r.rsi_max != null || r.drawdown_days) {
    values.push(["日线 RSI(14)", fmt(r.rsi, 2) + limits(r.rsi_max, r.rsi_second_max)]);
  }
  if (r.rsi7_max != null) values.push(["日线 RSI(7)", fmt(r.rsi7, 2) + limits(r.rsi7_max, r.rsi7_second_max)]);
  if (r.type === "ETF" && (r.rsi_max != null || r.drawdown_days)) values.push(["MACD 柱 (12,26,9)", fmt(r.macd, 4)]);
  if (r.drawdown_days) values.push([r.drawdown_days + " 日收盘回撤", pct(r.drawdown) + " · 阈值 < −" + r.drawdown_pct + "%"]);
  if (r.premium_max != null) values.push(["IOPV", fmt(r.iopv, 4)], ["盘中溢价", pct(r.premium) + " · 上限 " + r.premium_max + "%"]);
  if (r.yield_min != null) values.push(["近 365 天现金股息率", (r.yield_pct == null ? "—" : fmt(r.yield_pct) + "%") + " · 观察线 " + r.yield_min + "%"]);
  if (r.index) values.push(["最近公布净值", fmt(r.nav, 4) + " · " + (r.nav_day || "—")],
    ["跟踪指数 " + r.index + " 日涨跌", pct(r.index_pct) + limits(r.index_drop, r.index_drop_second, "%")],
    ["指数行情时间 · 北京", dateText(r.index_at)]);
  if (r.reasons.length) values.push(["命中条件", r.reasons.join("、")]);
  if (r.observe) values.push(["通知方式", "仅在看板观察"]);
  const dl = el("dl", null, "detail");
  for (const [name, value] of values) {
    const node = el("div");
    node.append(el("dt", name), el("dd", value));
    dl.append(node);
  }
  return dl;
}

function makeRow(r) {
  const tr = el("tr", null, "row");
  const first = el("td");
  const button = el("button", null, "name-button");
  const detailId = "detail-" + r.code;
  const typeIcon = {"A 股": ["A", "stock"], "港股": ["HK", "hk"], "场外基金": ["基", "fund"], ETF: ["ETF", "etf"]}[r.type];
  const icon = el("span", typeIcon[0], "type-icon " + typeIcon[1]);
  icon.setAttribute("aria-hidden", "true");
  const name = el("span");
  name.append(el("b", r.name), el("small", displayCode(r), "code"));
  button.append(icon, name);
  button.setAttribute("aria-controls", detailId);
  button.setAttribute("aria-label", r.name + "，行情与触发条件详情");
  first.append(button);
  tr.append(first);
  const columns = [
    ["最新价 / 净值", fmt(r.price, digits(r)), "price", true],
    ["日涨跌", pct(r.pct), signedClass(r.pct), false],
    ["首档目标", fmt(r.first, digits(r)), "", true],
    ["距首档", pct(r.gap), "", false]
  ];
  for (const [label, value, cls, currency] of columns) {
    const td = el("td", null, "num " + cls);
    td.dataset.label = label;
    if (currency && value !== "—") td.append(el("span", r.currency === "HKD" ? "HK$" : "¥", "currency"));
    td.append(document.createTextNode(value));
    if (r.index && cls === "price" && r.source) {
      const kind = r.source.includes("已公布") ? "公布净值" : r.source.includes("指数") ? "指数推算" : "盘中估值";
      td.append(el("small", kind, "code"));
    }
    tr.append(td);
  }
  const status = el("td");
  const cls = hits.has(r.status) ? "hits" : guards.has(r.status) ? "guard" : r.status.startsWith("observe") ? "quiet" : "";
  status.append(el("span", r.label, "badge " + cls));
  tr.append(status);
  const extra = el("tr", null, "row-details");
  extra.id = detailId;
  const cell = el("td");
  cell.colSpan = 6;
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
  if (sort === "change") rows.sort((a, b) => (b.pct ?? -Infinity) - (a.pct ?? -Infinity));
  else if (sort === "status") rows.sort((a, b) => rank(a) - rank(b));
  byId("rows").replaceChildren(...rows.flatMap(makeRow));
  byId("empty").hidden = rows.length > 0;
  byId("visible-count").textContent = rows.length + " 条";
  byId("clear-scope").hidden = !view.scope;
  byId("clear-scope").textContent = view.scope === "hits" ? "清除「触发提醒」筛选 ×" : "清除「溢价风控」筛选 ×";
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
for (const type of types) {
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
