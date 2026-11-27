"use strict";

const model = JSON.parse(document.getElementById("thermo-data").textContent);
const byId = id => document.getElementById(id);
const number = Number.isFinite;
const thresholds = model.thresholds;
const bands = model.bands;
const palette = ["#4389c7", "#4ca8aa", "#e1b84f", "#e28148", "#ca544b"];
const bandIndex = v => number(v) ? thresholds.filter(t => v > t).length : -1;
const currentBand = bandIndex(model.temperature);
const isFund = model.type === "场外基金";
const priceName = isFund ? "基金收益指数" : "复权价";
const currency = model.currency === "HKD" ? "港元" : "元";
const priceDigits = isFund ? 4 : model.type === "ETF" ? 3 : 2;
const asPrice = v => number(v) ? v.toFixed(priceDigits) : "—";
const asPercent = (v, digits = 2) => number(v) ? v.toFixed(digits) + "%" : "—";
const series = model.series || [];

function asTemperature(v) {
  if (!number(v)) return "—";
  let text = v.toFixed(1);
  // A rounded threshold must still describe the same notification band.
  if (bandIndex(Number(text)) !== bandIndex(v)) text = String(v);
  return text + "°";
}

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

function stamp(at) {
  if (!at || at.length <= 10) return at || "";
  const d = new Date(at);
  if (isNaN(d)) return "";
  return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "short" }).format(d);
}

function describe() {
  document.title = model.name + " · 温度计 · 红利观察台";
  byId("name").textContent = model.name;
  byId("instrument").textContent = [model.code.toUpperCase(), model.type].filter(Boolean).join(" · ");
  if (isFund) byId("price-label").textContent = "净值 / 估值";
  byId("price").textContent = number(model.price) ? asPrice(model.price) + " " + currency : "—";
  const priceAt = Object.hasOwn(model, "price_at") ? model.price_at : model.at;
  if (priceAt) byId("quote-time").textContent = (priceAt.length > 10 ? "报价时间 " : "报价日期 ") + stamp(priceAt);
  byId("temperature").textContent = asTemperature(model.temperature);
  const state = currentBand < 0 ? "温度待更新" : bands[currentBand];
  byId("status").textContent = state;
  byId("stat-band").textContent = state;
  if (currentBand >= 0) byId("stat-band").style.setProperty("--band", palette[currentBand]);
  const obs = model.at ? "温度观测 " + stamp(model.at) + "（北京）" : "";
  byId("asof").textContent = [obs, model.asof_note].filter(Boolean).join(" · ");
  byId("asof").hidden = !byId("asof").textContent;
  byId("method").textContent = model.method || "";
  byId("source").textContent = model.source ? "数据来源 · " + model.source : "";
  const parts = model.components || {};
  for (const [key, label, weight] of [["position","250 日价格位置",50],["rsi14","RSI(14)",35],["rsi7","RSI(7)",15]]) {
    const value = parts[key], row = el("div", null, "component");
    row.append(el("span",label), el("small",(number(value) ? value.toFixed(1) : "—") + " × " + weight + "%"),
      el("strong",number(value) ? (value * weight / 100).toFixed(2) + "°" : "—"));
    byId("components").append(row);
  }
  byId("price-axis").textContent = priceName + "（右轴）";
  bands.forEach((name, i) => {
    const li = el("li");
    li.style.setProperty("--band", palette[i]);
    if (i === currentBand) li.className = "active";
    const bounds = i === 0 ? "≤ " + thresholds[0] : i === bands.length - 1 ? "> " + thresholds[i - 1] :
      "(" + thresholds[i - 1] + ", " + thresholds[i] + "]";
    li.append(el("b", name), el("span", bounds));
    byId("band-key").append(li);
  });
}

function context(id) {
  const canvas = byId(id);
  const { width, height } = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  canvas.width = Math.max(1, Math.round(width * dpr));
  canvas.height = Math.max(1, Math.round(height * dpr));
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  return { ctx, width, height };
}

function gauge() {
  const { ctx, width, height } = context("gauge");
  const x = width / 2, y = height - 13, r = Math.min(x - 31, y - 25);
  if (r < 10) return;
  const angle = t => Math.PI * (1 + t / 100);
  const at = (v, radius) => [x + Math.cos(angle(v)) * radius, y + Math.sin(angle(v)) * radius];
  const ends = [0, ...thresholds, 100], stroke = Math.max(11, r * .125);
  for (let i = 1; i < ends.length; i++) {
    ctx.beginPath(); ctx.arc(x, y, r, angle(ends[i - 1]) + .005, angle(ends[i]) - .005);
    ctx.lineWidth = stroke; ctx.strokeStyle = palette[i - 1]; ctx.stroke();
  }
  ctx.font = "12px system-ui"; ctx.textBaseline = "middle"; ctx.textAlign = "center";
  for (let tick = 0; tick <= 100; tick += 5) {
    const major = tick % 25 === 0;
    const inner = r - stroke / 2 - 3;
    ctx.beginPath(); ctx.moveTo(...at(tick, inner)); ctx.lineTo(...at(tick, inner - (major ? 7 : 3)));
    ctx.strokeStyle = "#9bafa2"; ctx.lineWidth = 1; ctx.stroke();
    if (major) {
      ctx.fillStyle = "#51675e"; ctx.fillText(String(tick), ...at(tick, r + stroke / 2 + 10));
    }
  }
  if (number(model.temperature)) {
    const [px, py] = at(Math.max(0, Math.min(100, model.temperature)), r);
    ctx.beginPath(); ctx.arc(px, py, stroke * .55, 0, Math.PI * 2);
    ctx.fillStyle = "#fff"; ctx.fill(); ctx.lineWidth = 2.5; ctx.strokeStyle = "#203e32"; ctx.stroke();
    ctx.beginPath(); ctx.arc(px, py, stroke * .26, 0, Math.PI * 2); ctx.fillStyle = palette[currentBand]; ctx.fill();
  }
}

let visible = [], cursor = 0, plotArea;
const dateValue = day => Date.parse(day + "T00:00:00Z");
function setPeriod(years) {
  let cutoff = "";
  if (years && series.length) {
    const d = new Date(dateValue(series.at(-1)[0]));
    d.setUTCFullYear(d.getUTCFullYear() - years);
    cutoff = d.toISOString().slice(0, 10);
  }
  visible = series.filter(row => row[0] >= cutoff);
  cursor = Math.max(0, visible.length - 1);
  for (const btn of byId("period").querySelectorAll("button")) btn.setAttribute("aria-pressed", Number(btn.dataset.years) === years);
  byId("no-history").hidden = visible.length > 0;
  byId("plot").setAttribute("aria-valuemax", cursor);
  byId("plot").setAttribute("aria-disabled", !visible.length);
  byId("plot").tabIndex = visible.length ? 0 : -1;
  drawHistory();
}

function drawHistory() {
  const { ctx, width, height } = context("plot");
  const left = 29, right = width - 47, top = 12, bottom = height - 26;
  if (!visible.length || right <= left) {
    byId("cursor").replaceChildren(); return;
  }
  const available = visible.map(r => r[1]).filter(number);
  let lo = available.length ? Math.min(...available) : 0;
  let hi = available.length ? Math.max(...available) : 1;
  const pad = (hi - lo) * .06 || Math.max(.0001, Math.abs(hi) * .01);
  lo -= pad; hi += pad;
  const start = dateValue(visible[0][0]), end = dateValue(visible.at(-1)[0]);
  const x = i => end === start ? (left + right) / 2 : left + (right - left) * (dateValue(visible[i][0]) - start) / (end - start);
  const tempY = t => bottom - (bottom - top) * t / 100;
  const priceY = p => bottom - (bottom - top) * (p - lo) / (hi - lo);
  plotArea = {left,right,start,end};
  const ends = [0,...thresholds,100];
  for (let i = 1; i < ends.length; i++) {
    ctx.fillStyle = palette[i - 1] + "19";
    ctx.fillRect(left, tempY(ends[i]), right-left, tempY(ends[i - 1])-tempY(ends[i]));
  }
  ctx.font = "12px system-ui"; ctx.textBaseline = "middle";
  for (let tick = 0; tick <= 100; tick += 25) {
    const y = tempY(tick);
    ctx.beginPath(); ctx.moveTo(left,y); ctx.lineTo(right,y); ctx.strokeStyle = "#d9e0da"; ctx.lineWidth = 1; ctx.stroke();
    ctx.fillStyle = "#53685e"; ctx.textAlign = "right"; ctx.fillText(String(tick), left - 4, y);
    if (available.length) {
      ctx.textAlign = "left";
      ctx.fillText((lo + tick / 100 * (hi - lo)).toFixed(hi > 100 ? 0 : hi > 10 ? 1 : 2), right + 4, y);
    }
  }
  function line(column, axis, color, dashed) {
    ctx.beginPath(); let started = false;
    visible.forEach((row, i) => {
      const val = row[column];
      if (!number(val)) { started = false; return; }
      if (started) ctx.lineTo(x(i), axis(val));
      else { ctx.moveTo(x(i), axis(val)); started = true; }
    });
    ctx.setLineDash(dashed ? [4,3] : []); ctx.strokeStyle = color; ctx.lineWidth = dashed ? 1.6 : 1.8; ctx.stroke();
    ctx.setLineDash([]);
  }
  line(1, priceY, "#727c8f", true);
  line(2, tempY, "#08746b", false);
  ctx.textBaseline = "bottom"; ctx.fillStyle = "#53685e"; ctx.textAlign = "left";
  ctx.fillText(visible[0][0], left, height - 3);
  if (visible.length > 1) { ctx.textAlign = "right"; ctx.fillText(visible.at(-1)[0], right, height - 3); }
  const row = visible[cursor], cx = x(cursor);
  ctx.beginPath(); ctx.moveTo(cx,top); ctx.lineTo(cx,bottom); ctx.strokeStyle = "#748d80"; ctx.lineWidth = 1; ctx.setLineDash([2,3]); ctx.stroke(); ctx.setLineDash([]);
  for (const [val, axis, color] of [[row[1],priceY,"#727c8f"],[row[2],tempY,"#08746b"]]) {
    if (number(val)) {
      ctx.beginPath(); ctx.arc(cx,axis(val),3.2,0,Math.PI*2); ctx.fillStyle = color; ctx.fill(); ctx.lineWidth = 1; ctx.strokeStyle = "#fff"; ctx.stroke();
    }
  }
  const tempText = "温度 " + asTemperature(row[2]);
  const priceText = priceName + " " + asPrice(row[1]);
  byId("cursor").replaceChildren(el("time",row[0]), el("span",tempText), el("span",priceText));
  byId("plot").setAttribute("aria-valuenow", cursor);
  byId("plot").setAttribute("aria-valuetext", [row[0],tempText,priceText].join("，"));
}

function moveCursor(next) {
  if (!visible.length) return;
  const index = Math.max(0, Math.min(visible.length - 1, next));
  if (cursor !== index) { cursor = index; drawHistory(); }
}
function pointCursor(e) {
  if (!plotArea || !visible.length) return;
  const pixel = e.clientX - e.currentTarget.getBoundingClientRect().left;
  const { left, right, start, end } = plotArea;
  const target = start + (end - start) * (pixel - left) / (right - left);
  let low = 0, high = visible.length - 1;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (dateValue(visible[mid][0]) < target) low = mid + 1;
    else high = mid;
  }
  if (low && target - dateValue(visible[low-1][0]) < dateValue(visible[low][0]) - target) low--;
  moveCursor(low);
}

function bar(value, min, max, isWin) {
  const cell = el("div", null, "bar-cell" + (isWin ? " win" : ""));
  const track = el("span",null,"track");
  track.setAttribute("aria-hidden","true");
  const baseline = -min / (max-min) * 100;
  if (number(value)) {
    const pos = Math.max(0,Math.min(100,(value-min)/(max-min)*100));
    const fill = el("i",null,"fill");
    fill.style.left = Math.min(pos,baseline) + "%"; fill.style.width = Math.abs(pos-baseline) + "%";
    track.append(fill);
  }
  if (!isWin && min < 0) {
    const zero = el("i",null,"zero"); zero.style.left = baseline + "%"; track.append(zero);
  }
  cell.append(track,el("span",asPercent(value,isWin ? 1 : 2)));
  return cell;
}

function statistics() {
  const stats = model.stats || [];
  for (let year = 1; year <= 3; year++) {
    const s = stats[currentBand]?.[year - 1];
    const tr = el("tr"), hdr = el("th", year + " 年");
    hdr.scope = "row";
    tr.append(hdr,el("td",asPercent(s?.mean)),el("td",asPercent(s?.win,1)),el("td",s?.n ?? "—"));
    byId("current-stats").append(tr);
  }
  const note = model.stats_note || "仅统计已走完相应持有期限的历史样本。胜率为收益大于零的样本占比，历史结果仅供参考。";
  const noteNode = byId("stats-note");
  if (currentBand < 0) noteNode.append(el("strong","当前温区待定，暂无对应区间统计。"));
  else if (!stats[currentBand]?.some(s => s.n > 0)) noteNode.append(el("strong","本温区暂无已到期的持有样本。"));
  noteNode.append(document.createTextNode(note));
  const returns = stats.map(row=>row?.[0]?.mean).filter(number);
  const min = Math.min(0,...returns), max = Math.max(0,...returns);
  const extent = min === max ? 1 : max;
  bands.forEach((name,i) => {
    const s = stats[i]?.[0], row = el("div",null,"comparison-row");
    row.style.setProperty("--band",palette[i]);
    const label = el("div",null,"band-label");
    label.append(el("span",name,"band-title"),el("small","样本 " + (s?.n ?? "—")));
    row.append(label,bar(s?.mean,min,extent,false),bar(s?.win,0,100,true));
    byId("comparisons").append(row);
  });
}

describe();
statistics();
gauge();
setPeriod(1);
byId("period").addEventListener("click", e => {
  const btn = e.target.closest("button[data-years]");
  if (btn) setPeriod(Number(btn.dataset.years));
});
byId("plot").addEventListener("pointermove",pointCursor);
byId("plot").addEventListener("pointerdown",pointCursor);
byId("plot").addEventListener("keydown", e => {
  const target = { ArrowLeft:cursor-1, ArrowRight:cursor+1, Home:0, End:visible.length-1 }[e.key];
  if (target !== undefined) { e.preventDefault(); moveCursor(target); }
});
const resize = new ResizeObserver(() => { gauge(); drawHistory(); });
resize.observe(byId("gauge").parentElement);
resize.observe(byId("plot").parentElement);
