// Memory console (owner only). All memory content is untrusted: it is only ever
// inserted with textContent / createTextNode, never as HTML.
"use strict";

const TOKEN_KEY = "cmh-console-token";
// Token arrives in the URL fragment (never sent to the server); keep it in this
// tab's sessionStorage and remove it from the address bar.
(function takeToken() {
  const m = location.hash.match(/[#&]t=([^&]+)/);
  if (m) { sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1])); history.replaceState(null, "", location.pathname); }
})();

const $ = (s) => document.querySelector(s);
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v; else if (k === "style") e.style.cssText = v; else e.setAttribute(k, v);
  }
  for (const kid of kids) e.append(kid instanceof Node ? kid : document.createTextNode(String(kid ?? "")));
  return e;
}
const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, ...kids) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  for (const kid of kids) e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return e;
}
const fmt = (n) => Number(n).toLocaleString("en");
const compact = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? Math.round(n / 1e3) + "K" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n);
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function api(path) {
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (!token) { showLogin(); throw new Error("no token"); }
  const r = await fetch(path, { headers: { Authorization: "Bearer " + token } });
  if (r.status === 401 || r.status === 403) { showLogin("Token refused (" + r.status + ")."); throw new Error("auth"); }
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}
function showLogin(msg) { $("#login").hidden = false; if (msg) $("#status").textContent = msg; }
$("#token-save").addEventListener("click", () => {
  sessionStorage.setItem(TOKEN_KEY, $("#token-input").value.trim());
  $("#token-input").value = "";
  $("#login").hidden = true;
  loadOverview();
});

// ---- theme ---------------------------------------------------------------------
const THEMES = ["system", "light", "dark"];
let theme = "system";
try { theme = localStorage.getItem("cmh-console-theme") || "system"; } catch {}
function applyTheme() {
  if (theme === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = theme;
  $("#theme").setAttribute("aria-label", "Colour theme: " + theme);
}
applyTheme();
$("#theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % 3];
  applyTheme();
  try { localStorage.setItem("cmh-console-theme", theme); } catch {}
  rerender();
});

// ---- tabs ------------------------------------------------------------------------
const loaded = {};
const LOADERS = { overview: () => loadOverview(), map: () => loadMap(), activity: () => loadActivity(),
                  search: () => $("#q").focus(), public: () => loadPublic() };
function show(tab) {
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + tab));
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  if (!loaded[tab]) { loaded[tab] = true; LOADERS[tab](); }
  if (tab === "map") renderMap();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  const tab = { 1: "overview", 2: "map", 3: "activity", 4: "search", 5: "public" }[e.key];
  if (tab) show(tab);
});

// ---- tooltip (enhances; every value is also in the table view) -------------------
const tip = $("#tip");
function showTip(evt, title, rows) {
  tip.replaceChildren(el("div", { class: "t" }, title), ...rows.map(([color, value, label]) =>
    el("div", { class: "r" }, el("i", { style: `background:${color}` }), el("b", {}, value), el("span", {}, label))));
  tip.hidden = false;
  const r = evt.target.getBoundingClientRect?.() || { left: evt.clientX, top: evt.clientY, width: 0 };
  const x = (evt.clientX ?? r.left + r.width / 2) + 14, y = (evt.clientY ?? r.top) + 14;
  tip.style.left = Math.min(x, innerWidth - tip.offsetWidth - 8) + "px";
  tip.style.top = Math.min(y, innerHeight - tip.offsetHeight - 8) + "px";
}
const hideTip = () => { tip.hidden = true; };

// ---- shared chart pieces ---------------------------------------------------------
const GROUP_VARS = { "This laptop": "--s1", "Other machines": "--s2", "claude.ai": "--s3", "Perplexity": "--s4",
                     "Files & notes": "--s5" };
const EXTRA_VARS = { Retired: "--s1" };   // single-series charts take slot 1
const groupColor = (g) => cssVar(GROUP_VARS[g] || EXTRA_VARS[g] || "--neutral");
function legend(target, groups) {
  $(target).replaceChildren(...groups.map((g) => el("span", {}, el("i", { style: `background:${groupColor(g)}` }), g)));
}
function niceMax(v) {
  if (v <= 4) return 4;
  const p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
}
function bucketLabel(t, bucket) {
  const d = new Date(t + "T00:00:00");
  if (bucket === "month") return d.toLocaleDateString("en", { month: "short", year: "2-digit" });
  return d.toLocaleDateString("en", { month: "short", day: "numeric" });
}
function roundedTop(x, y, w, h, r) {
  r = Math.min(r, h, w / 2);
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
}
function table(target, head, rows) {
  const t = el("table", {}, el("tr", {}, ...head.map((h, i) => el("th", { class: i ? "n" : "" }, h))));
  for (const r of rows) t.append(el("tr", {}, ...r.map((c, i) => el("td", { class: i ? "n" : "" }, c))));
  $(target).replaceChildren(t);
}
document.querySelectorAll("[data-table]").forEach((b) => b.addEventListener("click", () => {
  const box = $("#" + b.dataset.table + "-table");
  box.hidden = !box.hidden;
  b.textContent = box.hidden ? "Show as table" : "Hide table";
}));

// Stacked columns: one column per time bucket, segments by group (2px surface gaps,
// ≤ 24px wide, 4px rounded top), hairline grid, clean ticks, one tooltip per column.
function stackedColumns(target, rows, groups, bucket, opts = {}) {
  const box = $(target), W = Math.max(320, box.clientWidth), H = opts.height || 240, PAD = { l: 40, r: 8, t: 10, b: 30 };
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.label || "chart" });
  const totals = rows.map((r) => groups.reduce((a, g) => a + (r[g] || 0), 0));
  const max = niceMax(Math.max(1, ...totals)), iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const y = (v) => PAD.t + ih - (v / max) * ih;
  const grid = s("g", { class: "grid" });
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i;
    grid.append(s("line", { x1: PAD.l, x2: W - PAD.r, y1: y(v), y2: y(v) }));
    svg.append(s("text", { x: PAD.l - 8, y: y(v) + 4, "text-anchor": "end", class: "tick" }, compact(v)));
  }
  svg.prepend(grid);
  const band = iw / Math.max(1, rows.length), bw = Math.min(24, Math.max(3, band * 0.7));
  const every = Math.ceil(rows.length / Math.max(1, Math.floor(iw / 64)));
  rows.forEach((r, i) => {
    const x = PAD.l + i * band + (band - bw) / 2;
    let acc = 0;
    const g = s("g", { class: "bar" });
    const present = groups.filter((k) => r[k] > 0);
    present.forEach((k, j) => {
      const y0 = y(acc), y1 = y(acc + r[k]);
      const h = Math.max(1, y0 - y1 - (j < present.length - 1 ? 2 : 0));
      const top = j === present.length - 1;
      g.append(top ? s("path", { d: roundedTop(x, y0 - h, bw, h, 4), fill: groupColor(k) })
                   : s("rect", { x, y: y0 - h, width: bw, height: h, fill: groupColor(k) }));
      acc += r[k];
    });
    const hit = s("rect", { class: "hit", x: PAD.l + i * band, y: PAD.t, width: band, height: ih, tabindex: 0,
                            "aria-label": `${bucketLabel(r.t, bucket)}: ${totals[i]}` });
    const tipRows = () => present.slice().reverse().map((k) => [groupColor(k), fmt(r[k]), k]);
    const title = `${bucketLabel(r.t, bucket)} · ${fmt(totals[i])} total`;
    hit.addEventListener("pointermove", (e) => showTip(e, title, tipRows()));
    hit.addEventListener("focus", (e) => showTip(e, title, tipRows()));
    hit.addEventListener("pointerleave", hideTip); hit.addEventListener("blur", hideTip);
    svg.append(g, hit);
    if (i % every === 0) svg.append(s("text", { x: PAD.l + i * band + band / 2, y: H - 10, "text-anchor": "middle" }, bucketLabel(r.t, bucket)));
  });
  svg.append(s("g", { class: "axis" }, s("line", { x1: PAD.l, x2: W - PAD.r, y1: y(0), y2: y(0) })));
  if (!rows.length) svg.append(s("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, "Nothing in this period"));
  box.replaceChildren(svg);
}

// Horizontal stacked bars, one per project, total at the bar end.
function projectBars(target, projects, groups) {
  const box = $(target), W = Math.max(320, box.clientWidth), ROW = 30, LAB = Math.min(210, W * 0.36);
  const H = projects.length * ROW + 8;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Documents per project by source" });
  const max = Math.max(1, ...projects.map((p) => p.total)), iw = W - LAB - 52;
  projects.forEach((p, i) => {
    const y = i * ROW + 6, bh = 16;
    svg.append(s("text", { x: LAB - 10, y: y + 13, "text-anchor": "end", class: "plabel" },
                 p.project.length > 28 ? p.project.slice(0, 27) + "…" : p.project));
    let x = LAB;
    const present = groups.filter((g) => p[g] > 0);
    present.forEach((g, j) => {
      const w = Math.max(2, (p[g] / max) * iw - (j < present.length - 1 ? 2 : 0));
      const seg = j === present.length - 1
        ? s("path", { d: `M${x},${y} L${x + w - 4},${y} Q${x + w},${y} ${x + w},${y + 4} L${x + w},${y + bh - 4} Q${x + w},${y + bh} ${x + w - 4},${y + bh} L${x},${y + bh} Z`, fill: groupColor(g) })
        : s("rect", { x, y, width: w, height: bh, fill: groupColor(g) });
      seg.setAttribute("class", "bar");
      seg.addEventListener("pointermove", (e) => showTip(e, p.project, [[groupColor(g), fmt(p[g]), g]]));
      seg.addEventListener("pointerleave", hideTip);
      svg.append(seg);
      x += w + (j < present.length - 1 ? 2 : 0);
    });
    svg.append(s("text", { x: x + 8, y: y + 13, class: "total" }, fmt(p.total)));
  });
  box.replaceChildren(svg);
}

function sparkline(svg, series) {
  const W = 200, H = 36, n = series.length;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  if (n < 2) return;
  const max = Math.max(1, ...series);
  const pts = series.map((v, i) => [(i / (n - 1)) * (W - 6) + 3, H - 4 - (v / max) * (H - 8)]);
  svg.append(s("polyline", { points: pts.map((p) => p.join(",")).join(" "), fill: "none", stroke: cssVar("--neutral"),
                             "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke" }));
  const [lx, ly] = pts[n - 1];
  svg.append(s("circle", { cx: lx, cy: ly, r: 4, fill: cssVar("--accent"), stroke: cssVar("--panel"), "stroke-width": 2 }));
}

// ---- overview -------------------------------------------------------------------
let days = 30, OV = null;
const ago = (secs) => secs == null ? "never" : secs < 120 ? `${secs} seconds ago` : secs < 7200 ? `${Math.round(secs / 60)} minutes ago`
  : `${Math.round(secs / 3600)} hours ago`;
const periodWords = () => days === 0 ? "in total" : `in the last ${days} days`;
document.querySelectorAll("#tab-overview .filters button").forEach((b) => b.addEventListener("click", () => {
  days = Number(b.dataset.days);
  document.querySelectorAll("#tab-overview .filters button").forEach((x) => x.classList.toggle("on", x === b));
  loadOverview();
}));

function story(sm, ov) {
  const places = Object.values(sm.sources).filter((n) => n > 0).length;
  const oldest = sm.oldest ? new Date(sm.oldest).toLocaleDateString("en", { month: "long", year: "numeric" }) : "";
  const p = $("#story");
  p.replaceChildren("Your memory holds ", el("b", {}, fmt(sm.documents) + " documents"), ` from ${places} kinds of source across `,
    el("b", {}, sm.projects + " projects"), oldest ? `, reaching back to ${oldest}. ` : ". ",
    days === 0 ? "" : el("b", {}, fmt(sm.documents_in_period)), days === 0 ? "" : ` of them happened ${periodWords()}. `,
    `It last checked for changes ${ago(sm.last_pass_seconds)}`,
    ov && ov.audit_chain && !ov.audit_chain.ok ? ", and the audit log needs attention." : ", and its tamper-evident log is intact.");
}
function kpis(sm) {
  const delta = days > 0 && sm.documents_previous >= 0 ? sm.documents_in_period - sm.documents_previous : null;
  const tiles = [
    { hero: true, label: "Documents remembered", value: fmt(sm.documents),
      means: "Sessions, chats, files and notes it has read and can search.",
      delta: delta == null ? "" : `${delta >= 0 ? "+" : "−"}${fmt(Math.abs(delta))} vs the previous ${days} days`, spark: true },
    { label: "Searchable passages", value: compact(sm.passages),
      means: "Documents are cut into passages; an assistant receives the few that match its question." },
    { label: "Connections", value: compact(sm.connections),
      means: "Links in the knowledge graph: what each file defines, imports or mentions, and where it came from." },
    { label: days ? `Updates kept as history (${days} days)` : "Updates kept as history", value: compact(sm.retired_in_period),
      means: "Older passages retired because the source changed; kept so you can see what it used to say." },
  ];
  $("#kpis").replaceChildren(...tiles.map((t) => {
    const card = el("div", { class: "kpi" + (t.hero ? " hero" : "") }, el("div", { class: "label" }, t.label),
                    el("div", { class: "value" }, t.value));
    if (t.delta) card.append(el("div", { class: "delta" }, t.delta));
    card.append(el("p", { class: "means" }, t.means));
    if (t.spark) { const sv = s("svg", { "aria-hidden": "true" }); sparkline(sv, sm.spark.map((x) => x.n)); card.append(sv); }
    return card;
  }));
}
async function loadOverview() {
  const [sm, tl, pj, ov] = await Promise.all([api(`/v1/console/summary?days=${days}`), api(`/v1/console/timeline?days=${days || 0}`),
                                               api("/v1/console/projects"), api("/v1/console/overview")]);
  OV = { sm, tl, pj, ov };
  renderOverview();
  $("#status").textContent = "updated " + new Date().toLocaleTimeString();
}
function renderOverview() {
  if (!OV) return;
  const { sm, tl, pj, ov } = OV;
  story(sm, ov);
  kpis(sm);
  // Files carry no date of their own (only when they were first read), so the timeline
  // shows sessions and chats, which do.
  const dated = tl.groups.filter((g) => g !== "Files & notes");
  const drows = tl.rows.filter((r) => dated.some((g) => r[g] > 0));
  legend("#tl-legend", dated);
  stackedColumns("#tl-chart", drows, dated, tl.bucket, { label: "Sessions and chats over time by source" });
  table("#tl-table", ["Period", ...dated, "Total"],
        drows.map((r) => [bucketLabel(r.t, tl.bucket), ...dated.map((g) => fmt(r[g])), fmt(dated.reduce((a, g) => a + r[g], 0))]));
  projectBars("#pj-chart", [...pj].sort((a, b) => b.total - a.total), tl.groups);
  table("#pj-table", ["Project", ...tl.groups, "Total", "Last activity"],
        pj.map((p) => [p.project, ...tl.groups.map((g) => fmt(p[g])), fmt(p.total), p.last.slice(0, 10)]));
  const rt = tl.rows.filter((r) => r.retired > 0);
  stackedColumns("#rt-chart", rt.map((r) => ({ t: r.t, Retired: r.retired })), ["Retired"], tl.bucket,
                 { height: 180, label: "Passages retired over time" });
  table("#rt-table", ["Period", "Passages retired"], rt.map((r) => [bucketLabel(r.t, tl.bucket), fmt(r.retired)]));
  const m = $("#machines");
  m.replaceChildren(el("tr", {}, el("th", {}, "Machine"), el("th", { class: "n" }, "Sessions"), el("th", { class: "n" }, "Latest")));
  for (const r of ov.machines || []) m.append(el("tr", {}, el("td", {}, r.machine), el("td", { class: "n" }, fmt(r.sessions)),
                                                     el("td", { class: "n" }, (r.updated || "").slice(0, 10))));
  const dm = ov.daemon || {}, b = ov.backup;
  const tunnels = Object.entries(dm.tunnels || {}).map(([h, t]) => `${h}: ${t.state}`).join(", ") || "none";
  const h = $("#health");
  h.replaceChildren(...[
    ["Indexer", dm.seconds_since_pass == null ? "not run yet" : `last pass ${ago(dm.seconds_since_pass)} (${dm.status})`],
    ["Tunnels to servers", tunnels],
    ["Graphics card", dm.gpu ? `${dm.gpu.temp_c} °C, ${dm.gpu.mem_mb} MB in use` : "n/a"],
    ["Latest backup", b ? `${b.created.replace("T", " ").slice(0, 16)} (${b.ok ? "verified" : "FAILED"}, ${Math.round(b.bytes / 1e6)} MB)` : "none"],
    ["Audit log", ov.audit_chain.ok ? `intact (${fmt(ov.audit_chain.rows)} chained entries)` : `BROKEN at entry ${ov.audit_chain.broken_at}`],
    ["Devices with access", (ov.devices || []).filter((d) => !d.revoked_at).map((d) => d.name).join(", ")],
  ].map(([k, v]) => el("tr", {}, el("td", {}, k), el("td", {}, v))));
}
let rt;
addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(rerender, 200); });
function rerender() { renderOverview(); renderMap(); }

// ---- knowledge map: a timeline of what the memory learned -------------------------
// Time runs left to right; each kind of thing has its own lane. Links are drawn only
// for the selected item, so the picture never turns into a hairball. The slider is a
// "now" line: everything after it fades out.
let MAP = null, playing = null, selected = null, nowFrac = 1;
const TYPE_WORDS = { project: "Project", session: "Coding session", chat: "AI chat", file: "File or note",
                     module: "Code module", "mentioned file": "File mentioned in a session" };
const LANES = [["session", "Coding sessions"], ["chat", "AI chats"], ["file", "Files and notes"],
               ["module", "Code modules"], ["mentioned file", "Files mentioned"]];
const DAY = 864e5;
const tms = (w) => Date.parse((w || "").slice(0, 19) + "Z");
function nodeColor(n) { return n.group ? groupColor(n.group) : cssVar("--neutral"); }

async function loadMap() {
  const pj = await api("/v1/console/projects");
  const span = (p) => tms(p.last) - tms(p.first);
  // Open on the project with the longest history (with at least a handful of documents).
  const ordered = [...pj].sort((a, b) => (b.total >= 5) - (a.total >= 5) || span(b) - span(a));
  const sel = $("#m-project");
  sel.replaceChildren(...ordered.map((p) => el("option", { value: p.project },
    `${p.project} · ${p.total} docs · ${p.first.slice(0, 7)} → ${p.last.slice(0, 7)}`)));
  sel.addEventListener("change", () => drawMap(sel.value));
  mapLegend();
  if (ordered.length) drawMap(ordered[0].project);
}
function mapLegend() {
  const items = [...Object.keys(GROUP_VARS).map((g) => [groupColor(g), "50%", g === "Files & notes" ? "Files and notes" :
                   g === "This laptop" || g === "Other machines" ? `Session on ${g.toLowerCase()}` : `${g} chat`]),
                 [cssVar("--neutral"), "2px", "Code module or mentioned file"]];
  $("#m-legend").replaceChildren(...items.map(([c, r, label]) => el("li", {}, el("i", { style: `background:${c};border-radius:${r}` }), label)));
}
async function drawMap(name) {
  stopPlay();
  MAP = await api("/v1/console/project-map?name=" + encodeURIComponent(name));
  MAP.byId = Object.fromEntries(MAP.nodes.map((n) => [n.id, n]));
  MAP.adj = {};
  for (const e of MAP.edges) {
    (MAP.adj[e.source] ||= []).push([e.target, e.relation]);
    (MAP.adj[e.target] ||= []).push([e.source, e.relation]);
  }
  selected = null;
  nowFrac = 1;
  $("#m-time").value = 1000;
  const shown = MAP.documents_shown < MAP.documents_total ? ` (an even sample of ${MAP.documents_shown} across time)` : "";
  $("#m-info").textContent = `${name}: ${fmt(MAP.documents_total)} documents${shown}. Hover a dot for its name, click it to see ` +
    "what it is linked to. Drag the slider or press play to watch the memory grow.";
  renderMap();
  clearDetail();
}
function renderMap() {
  if (!MAP) return;
  const box = $("#cy"), W = Math.max(480, box.clientWidth), PAD = { l: 170, r: 24, t: 18, b: 34 };
  const nodes = MAP.nodes.filter((n) => n.type !== "project");
  let t0 = Math.min(...nodes.map((n) => tms(n.when))), t1 = Math.max(...nodes.map((n) => tms(n.when)));
  if (!isFinite(t0)) { box.replaceChildren(el("p", { class: "muted", style: "padding:20px" }, "Nothing to show for this project.")); return; }
  if (t1 - t0 < 2 * DAY) { t0 -= DAY; t1 += DAY; }
  MAP.t0 = t0; MAP.t1 = t1;
  const iw = W - PAD.l - PAD.r, x = (t) => PAD.l + ((t - t0) / (t1 - t0)) * iw;
  // Lanes: height grows with how many items share a moment (beeswarm stacking).
  const lanes = LANES.filter(([type]) => nodes.some((n) => n.type === type));
  const placed = {}, laneTop = {}, R = 5, GAP = 2 * R + 2;
  let y = PAD.t;
  for (const [type] of lanes) {
    const items = nodes.filter((n) => n.type === type).sort((a, b) => tms(a.when) - tms(b.when));
    const cols = [];   // occupied y offsets near each x
    let maxOff = 0;
    for (const n of items) {
      const px = x(tms(n.when));
      let k = 0;
      const used = cols.filter((c) => Math.abs(c.x - px) < GAP).map((c) => c.k);
      while (used.includes(k)) k++;
      cols.push({ x: px, k });
      const off = (k % 2 ? 1 : -1) * Math.ceil(k / 2) * GAP;
      maxOff = Math.max(maxOff, Math.abs(off));
      placed[n.id] = { x: px, off };
    }
    const h = Math.min(220, Math.max(46, 2 * maxOff + 3 * R));
    laneTop[type] = { y, h };
    for (const n of items) placed[n.id].y = y + h / 2 + Math.max(-h / 2 + R, Math.min(h / 2 - R, placed[n.id].off));
    y += h + 10;
  }
  const H = y + PAD.b;
  MAP.placed = placed;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Timeline of what the memory learned" });
  // lane bands + labels
  lanes.forEach(([type, label], i) => {
    const { y: ly, h } = laneTop[type];
    svg.append(s("rect", { x: PAD.l, y: ly, width: iw, height: h, rx: 8, fill: i % 2 ? "transparent" : cssVar("--bg") }));
    svg.append(s("text", { x: PAD.l - 12, y: ly + h / 2 + 4, "text-anchor": "end", class: "plabel" }, label));
  });
  // time axis
  const span = t1 - t0, step = span > 400 * DAY ? "month" : span > 60 * DAY ? "month" : span > 14 * DAY ? "week" : "day";
  const ticks = [];
  const d = new Date(t0); d.setUTCHours(0, 0, 0, 0);
  if (step === "month") d.setUTCDate(1);
  while (d.getTime() <= t1) {
    if (d.getTime() >= t0) ticks.push(d.getTime());
    if (step === "month") d.setUTCMonth(d.getUTCMonth() + (span > 400 * DAY ? 2 : 1));
    else d.setUTCDate(d.getUTCDate() + (step === "week" ? 7 : 1));
  }
  const axis = s("g", { class: "grid" });
  for (const t of ticks) {
    axis.append(s("line", { x1: x(t), x2: x(t), y1: PAD.t, y2: H - PAD.b }));
    svg.append(s("text", { x: x(t), y: H - 12, "text-anchor": "middle" },
      new Date(t).toLocaleDateString("en", step === "month" ? { month: "short", year: "2-digit" } : { month: "short", day: "numeric" })));
  }
  svg.prepend(axis);
  const links = s("g", { class: "links" });
  svg.append(links);
  // dots
  for (const n of nodes) {
    const p = placed[n.id];
    const shape = n.type === "module" || n.type === "mentioned file"
      ? s("rect", { x: p.x - R + 0.5, y: p.y - R + 0.5, width: 2 * R - 1, height: 2 * R - 1, rx: 2, transform: `rotate(45 ${p.x} ${p.y})` })
      : s("circle", { cx: p.x, cy: p.y, r: R });
    shape.setAttribute("fill", nodeColor(n));
    shape.setAttribute("stroke", cssVar("--panel"));
    shape.setAttribute("stroke-width", 2);
    shape.setAttribute("class", "dot");
    shape.setAttribute("tabindex", 0);
    shape.dataset.id = n.id;
    const when = new Date(tms(n.when)).toLocaleDateString("en", { year: "numeric", month: "short", day: "numeric" });
    const tipIt = (e) => showTip(e, when, [[nodeColor(n), "", `${TYPE_WORDS[n.type]}: ${n.label}`]]);
    shape.addEventListener("pointermove", tipIt); shape.addEventListener("focus", tipIt);
    shape.addEventListener("pointerleave", hideTip); shape.addEventListener("blur", hideTip);
    shape.addEventListener("click", () => select(n.id));
    shape.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && select(n.id));
    svg.append(shape);
  }
  // the "now" line
  const nx = PAD.l + nowFrac * iw;
  svg.append(s("line", { id: "nowline", x1: nx, x2: nx, y1: PAD.t - 6, y2: H - PAD.b, stroke: cssVar("--accent"), "stroke-width": 2 }));
  box.replaceChildren(svg);
  MAP.svg = svg; MAP.links = links; MAP.x = x; MAP.iw = iw; MAP.PAD = PAD;
  applyTime();
  if (selected) select(selected, true);
}
function applyTime() {
  if (!MAP || !MAP.svg) return;
  const t = MAP.t0 + nowFrac * (MAP.t1 - MAP.t0);
  let visible = 0;
  MAP.svg.querySelectorAll(".dot").forEach((d) => {
    const later = tms(MAP.byId[d.dataset.id].when) > t + 60e3;
    d.style.opacity = later ? 0.08 : "";
    if (!later) visible++;
  });
  const nx = MAP.PAD.l + nowFrac * MAP.iw, line = MAP.svg.querySelector("#nowline");
  line.setAttribute("x1", nx); line.setAttribute("x2", nx);
  line.style.display = nowFrac >= 1 ? "none" : "";
  const day = new Date(t).toLocaleDateString("en", { year: "numeric", month: "short", day: "numeric" });
  $("#m-date").textContent = `${day} · ${fmt(visible)} known`;
  $("#m-time").setAttribute("aria-valuetext", `${day}: ${visible} items known`);
}
$("#m-time").addEventListener("input", (e) => { stopPlay(); nowFrac = Number(e.target.value) / 1000; applyTime(); });
function stopPlay() { if (playing) { clearInterval(playing); playing = null; $("#m-play").textContent = "▶ Play history"; } }
$("#m-play").addEventListener("click", () => {
  if (playing) return stopPlay();
  if (REDUCED) { nowFrac = 1; $("#m-time").value = 1000; applyTime(); return; }
  if (nowFrac >= 1) nowFrac = 0;
  $("#m-play").textContent = "❚❚ Pause";
  playing = setInterval(() => {   // explanatory animation: about 8 seconds start to end
    nowFrac = Math.min(1, nowFrac + 0.0125);
    $("#m-time").value = Math.round(nowFrac * 1000);
    applyTime();
    if (nowFrac >= 1) stopPlay();
  }, 100);
});
function select(id, keep = false) {
  selected = id;
  const n = MAP.byId[id], p = MAP.placed[id];
  const nb = (MAP.adj[id] || []).filter(([o]) => MAP.placed[o]);
  const near = new Set([id, ...nb.map(([o]) => o)]);
  MAP.svg.querySelectorAll(".dot").forEach((d) => d.classList.toggle("faded", !near.has(d.dataset.id)));
  MAP.links.replaceChildren(...nb.map(([o]) => {
    const q = MAP.placed[o], mx = (p.x + q.x) / 2, my = Math.min(p.y, q.y) - 24;
    return s("path", { d: `M${p.x},${p.y} Q${mx},${my} ${q.x},${q.y}`, fill: "none", stroke: cssVar("--accent"),
                       "stroke-width": 1.5, opacity: 0.55 });
  }));
  if (!keep) detail(n, nb);
}
function clearDetail() {
  selected = null;
  if (MAP && MAP.svg) { MAP.svg.querySelectorAll(".dot").forEach((d) => d.classList.remove("faded")); MAP.links.replaceChildren(); }
  $("#m-side").replaceChildren(el("h3", {}, "How to read this"), $("#m-legend"),
    el("p", { class: "small" }, "Left to right is time. Each row is one kind of thing the memory knows."),
    el("p", { class: "muted small" }, "Click a dot to see what it is, when the memory first saw it, and what it is linked to " +
      "(a session to the files it talked about, a file to the code modules it defines)."));
}
function detail(n, nb) {
  const when = new Date(tms(n.when)).toLocaleString("en", { dateStyle: "medium", timeStyle: "short" });
  const REL = { "part of": "belongs to", defines: "defines", mentions: "talks about", imports: "imports" };
  const list = el("ul");
  nb.slice(0, 20).forEach(([o, rel]) => {
    const m = MAP.byId[o];
    const b = el("button", {}, m.label);
    b.addEventListener("click", () => select(o));
    list.append(el("li", {}, el("span", { class: "muted small" }, `${REL[rel] || rel} · ${TYPE_WORDS[m.type] || m.type}: `), b));
  });
  const back = el("button", { class: "link" }, "Back to the legend");
  back.addEventListener("click", clearDetail);
  $("#m-side").replaceChildren(el("div", { class: "kind" }, TYPE_WORDS[n.type] || n.type), el("h3", {}, n.label),
    el("p", { class: "small" }, (n.type === "module" || n.type === "mentioned file" ? "First appeared " : "") + when),
    n.group ? el("p", { class: "small muted" }, `Source: ${n.group}`) : "",
    n.detail ? el("p", { class: "path" }, n.detail) : "",
    el("p", { class: "small" }, el("b", {}, fmt(nb.length)), nb.length === 1 ? " linked item" : " linked items",
       nb.length > 20 ? " (first 20 shown)" : ""), list, back);
}

// ---- activity --------------------------------------------------------------------
let ACT = [], kind = "";
const KINDS = { indexed: ["indexed"], retired: ["retired"], device: ["added device", "paired device", "revoked device", "new token for device"],
                publish: ["published the public copy", "visitor question"] };
document.querySelectorAll("#a-filter button").forEach((b) => b.addEventListener("click", () => {
  kind = b.dataset.kind;
  document.querySelectorAll("#a-filter button").forEach((x) => x.classList.toggle("on", x === b));
  renderActivity();
}));
async function loadActivity() {
  const [rows, ov] = await Promise.all([api("/v1/console/activity"), api("/v1/console/overview")]);
  ACT = rows;
  const c = ov.audit_chain;
  $("#chain").textContent = c.ok
    ? `Every change is recorded in a tamper-evident log (${fmt(c.rows)} entries, each sealed to the one before). It is intact.`
    : `The tamper-evident log is broken at entry ${c.broken_at}: something changed it outside the memory software.`;
  renderActivity();
}
function renderActivity() {
  const rows = ACT.filter((r) => !kind || KINDS[kind].includes(r.what));
  const feed = $("#feed");
  feed.replaceChildren();
  let day = "";
  const today = new Date().toDateString(), yest = new Date(Date.now() - 864e5).toDateString();
  for (const r of rows) {
    const d = new Date(r.ts);
    const label = d.toDateString() === today ? "Today" : d.toDateString() === yest ? "Yesterday" : d.toLocaleDateString("en", { weekday: "long", month: "short", day: "numeric" });
    if (label !== day) { feed.append(el("div", { class: "day" }, label)); day = label; }
    const short = r.target.split(/[\\/]/).pop() || r.target;
    feed.append(el("div", { class: "ev" }, el("time", {}, d.toLocaleTimeString("en", { hour: "2-digit", minute: "2-digit" })),
      el("span", { class: "verb" }, r.what), el("span", { class: "tgt", title: r.target }, short), el("span", { class: "out" }, r.outcome)));
  }
  if (!rows.length) feed.append(el("p", { class: "muted" }, "Nothing of this kind recently."));
}

// ---- search ----------------------------------------------------------------------
async function search() {
  const q = $("#q").value.trim();
  if (!q) return;
  $("#q-info").textContent = "searching…";
  const mode = $("#mode").value;
  const res = await api(`/v1/console/search?q=${encodeURIComponent(q)}${mode ? "&mode=" + mode : ""}`);
  $("#q-info").textContent = `${res.results.length} results in ${Math.round(res.timings_ms?.total ?? 0)} ms`;
  $("#results").replaceChildren(...res.results.map((r) => el("div", { class: "res" },
    el("div", { class: "t" }, r.title || r.provenance?.source_path || ""),
    el("div", { class: "m" }, `${r.project} · ${r.kind}${r.section_path ? " · " + r.section_path : ""} · `,
       el("span", { class: "badge " + r.provenance.verification_status }, r.provenance.verification_status)),
    el("div", { class: "x" }, (r.text || "").slice(0, 500)))));
}
$("#q-go").addEventListener("click", search);
$("#q").addEventListener("keydown", (e) => e.key === "Enter" && search());

// ---- public site -----------------------------------------------------------------
async function loadPublic() {
  const p = await api("/v1/console/public");
  const kids = Object.entries(p.children).map(([k, c]) => `${k}: ${c.state}`).join(", ") || "not running";
  $("#pub-status").replaceChildren(...[
    ["Helper processes", kids], ["Current address", p.address || "none yet"], ["Public copy made", p.export || "none"],
    ["Visitor answer model", p.model_enabled ? "on" : "OFF (excerpts only)"],
    ["Guard rules", p.guard_config ? "your config/public_guard.toml" : "built-in defaults"],
  ].map(([k, v]) => el("tr", {}, el("td", {}, k), el("td", {}, v))));
  $("#mod-info").textContent = p.moderation.length
    ? `${p.moderation.length} flagged questions, newest first. Visitors are identified only by a salted hash.`
    : "No flagged questions.";
  $("#moderation").replaceChildren(...p.moderation.map((r) => el("div", { class: "ev" },
    el("time", {}, r.t.slice(5, 16).replace("T", " ")), el("span", { class: "verb" }, r.reasons.join(", ")),
    el("span", { class: "tgt" }, r.question.slice(0, 200)), el("span", { class: "out" }, r.client.slice(0, 8)))));
}

if (sessionStorage.getItem(TOKEN_KEY)) { loaded.overview = true; loadOverview().catch(() => {}); } else showLogin();
