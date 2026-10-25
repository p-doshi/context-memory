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
const LOADERS = { overview: () => loadOverview(), projects: () => loadProjects(), activity: () => loadActivity(),
                  search: () => $("#q").focus(), public: () => loadPublic() };
function show(tab) {
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + tab));
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  if (!loaded[tab]) { loaded[tab] = true; LOADERS[tab](); }
  if (tab === "projects") renderStory();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  const tab = { 1: "overview", 2: "projects", 3: "activity", 4: "search", 5: "public" }[e.key];
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
const groupColor = (g) => cssVar(GROUP_VARS[g] || "--neutral");
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
  if (box.hidden) b.dataset.label = b.textContent;
  box.hidden = !box.hidden;
  b.textContent = box.hidden ? b.dataset.label : "Hide table";
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
  const sel = rows.findIndex((r) => r.t === opts.selected);
  const sw = Math.min(band - 2, bw + 16);
  if (sel >= 0) grid.append(s("rect", { x: PAD.l + sel * band + (band - sw) / 2, y: PAD.t, width: Math.max(2, sw), height: ih, rx: 6, fill: cssVar("--soft") }));
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
    if (opts.onPick) {
      hit.classList.add("pick");
      hit.addEventListener("click", () => opts.onPick(r.t));
      hit.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && opts.onPick(r.t));
    }
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
const DAYMS = 864e5;
function rel(ts) {
  const d = Math.floor((Date.now() - Date.parse(ts.slice(0, 19) + "Z")) / DAYMS);
  return d <= 0 ? "today" : d === 1 ? "yesterday" : d < 14 ? `${d} days ago` : d < 60 ? `${Math.round(d / 7)} weeks ago`
    : new Date(ts.slice(0, 10) + "T00:00:00").toLocaleDateString("en", { month: "short", year: "numeric" });
}
const longDate = (ts) => new Date(ts.slice(0, 10) + "T00:00:00").toLocaleDateString("en", { month: "short", day: "numeric", year: "numeric" });
document.querySelectorAll("#tab-overview .filters button").forEach((b) => b.addEventListener("click", () => {
  days = Number(b.dataset.days);
  document.querySelectorAll("#tab-overview .filters button").forEach((x) => x.classList.toggle("on", x === b));
  loadOverview();
}));

function compareWords(now, before) {
  if (!before) return "";
  const r = now / before;
  return r >= 1.8 ? `, about ${r >= 3 ? Math.round(r) : r.toFixed(1)} times as many as the ${days} days before`
    : r >= 1.15 ? `, more than the ${days} days before` : r > 0.87 ? `, about the same as the ${days} days before`
    : r > 0.5 ? `, fewer than the ${days} days before` : `, a quieter stretch than the ${days} days before`;
}
function story(sm, ov) {
  const oldest = sm.oldest ? new Date(sm.oldest).toLocaleDateString("en", { month: "long", year: "numeric" }) : "";
  const intact = ov && ov.audit_chain && !ov.audit_chain.ok ? "but the audit log needs attention." : "and its tamper-evident log is intact.";
  const p = $("#story"), kids = [];
  if (days > 0 && sm.worked_in_period) {
    kids.push(`In the last ${days} days you worked on `, el("b", {}, `${sm.projects_in_period} project${sm.projects_in_period === 1 ? "" : "s"}`),
      " in ", el("b", {}, `${fmt(sm.worked_in_period)} sessions and chats`), compareWords(sm.worked_in_period, sm.worked_previous), ". ");
    if (sm.top_project && sm.projects_in_period > 1)
      kids.push(sm.top_project_share >= 0.5 ? "Most of it was " : "The biggest share was ", el("b", {}, sm.top_project), ". ");
  } else if (days > 0) {
    kids.push(`Nothing new happened in the last ${days} days. `);
  }
  kids.push(`The memory holds ${fmt(sm.documents)} things from ${sm.projects} projects`, oldest ? `, going back to ${oldest}. ` : ". ",
    `It last checked for changes ${ago(sm.last_pass_seconds)}, ${intact}`);
  p.replaceChildren(...kids);
}
function kpis(sm) {
  const per = (n) => sm.documents ? Math.max(1, Math.round(n / sm.documents)) : 0;
  const every = sm.interval_seconds ? Math.round(sm.interval_seconds / 60) : null;
  const late = sm.last_pass_seconds != null && sm.interval_seconds && sm.last_pass_seconds > 3 * sm.interval_seconds + 600;
  const tiles = [
    { value: fmt(sm.documents), what: "things remembered: sessions, chats, files and notes",
      so: days > 0 ? `${fmt(sm.documents_in_period)} of them are from the last ${days} days. Any of them can come back as context in a new chat.`
                   : "Any of them can come back as context in a new chat.", spark: true },
    { value: "~" + per(sm.passages), what: `passages per thing, on average (${fmt(sm.passages)} in all)`,
      so: `A question pulls back the ${sm.search_k || 8} best passages, so an assistant reads a few relevant pieces instead of whole files.` },
    { value: "~" + per(sm.links_from_documents), what: "links per thing to the files and code it touched",
      so: "These links are how a question about one file finds the sessions that worked on it." },
    { value: sm.last_pass_seconds == null ? "–" : sm.last_pass_seconds < 3600 ? `${Math.max(1, Math.round(sm.last_pass_seconds / 60))} min`
                                                                              : `${Math.round(sm.last_pass_seconds / 3600)} h`,
      what: "since it last checked your files and chats for changes",
      so: late ? "Overdue: the indexer may have stopped. Check Health below." : every ? `Up to date. It checks every ${every} minutes.` : "Up to date.",
      cls: late ? "warn" : "ok" },
  ];
  $("#kpis").replaceChildren(...tiles.map((t) => {
    const card = el("div", { class: "kpi" }, el("div", { class: "value" }, t.value), el("div", { class: "what" }, t.what));
    const so = el("p", { class: "so" + (t.cls ? " " + t.cls : "") }, t.so);
    card.append(so);
    if (t.spark && sm.spark.length > 1) { const sv = s("svg", { "aria-hidden": "true" }); sparkline(sv, sm.spark.map((x) => x.n)); card.append(sv); }
    return card;
  }));
}
function latestList(pj) {
  const rows = pj.filter((p) => p.latest_when).slice(0, 7);
  $("#latest").replaceChildren(...rows.map((p) => {
    const b = el("button", { class: "proj", type: "button" }, el("i", { class: "dot", style: `background:${groupColor(p.latest_group)}` }),
      el("span", { class: "pbody" }, el("span", { class: "phead" }, el("b", {}, p.project), el("span", { class: "when-rel" }, rel(p.latest_when))),
         el("span", { class: "plast" }, p.latest_title)));
    b.addEventListener("click", () => { show("projects"); openProject(p.project); });
    return b;
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
  latestList(pj);
  projectBars("#pj-chart", [...pj].sort((a, b) => b.total - a.total), tl.groups);
  table("#pj-table", ["Project", ...tl.groups, "Total", "Last activity"],
        pj.map((p) => [p.project, ...tl.groups.map((g) => fmt(p[g])), fmt(p.total), p.last.slice(0, 10)]));
  const rt = tl.rows.filter((r) => r.retired > 0);
  $("#kept").replaceChildren(...(sm.retired_in_period
    ? [el("b", {}, `${fmt(sm.retired_in_period)} passages`), ` were replaced ${periodWords()}`,
       sm.retired_top_project ? ", mostly in " : "", sm.retired_top_project ? el("b", {}, sm.retired_top_project) : "",
       ". The old text is kept, so you can still ask what a file used to say."]
    : [`Nothing was replaced ${periodWords()}.`]));
  table("#rt-table", ["Period", "Passages replaced"], rt.map((r) => [bucketLabel(r.t, tl.bucket), fmt(r.retired)]));
  const m = $("#machines");
  m.replaceChildren(el("tr", {}, el("th", {}, "Machine"), el("th", { class: "n" }, "Sessions"), el("th", { class: "n" }, "Latest")));
  for (const r of ov.machines || []) m.append(el("tr", {}, el("td", {}, r.machine), el("td", { class: "n" }, fmt(r.sessions)),
                                                     el("td", { class: "n" }, (r.updated || "").slice(0, 10))));
  const dm = ov.daemon || {}, b = ov.backup;
  const tunnels = Object.entries(dm.tunnels || {});
  const down = tunnels.filter(([, t]) => t.state !== "up");
  const h = $("#health");
  h.replaceChildren(...[
    ["Indexer", dm.seconds_since_pass == null ? ["not run yet", "warn"] : [`running, last pass ${ago(dm.seconds_since_pass)}`, dm.status === "ok" ? "ok" : "warn"]],
    ["Tunnels to servers", !tunnels.length ? ["none set up", ""] : down.length
      ? [`${down.map(([n]) => n).join(", ")} down (${tunnels.length - down.length} of ${tunnels.length} up)`, "warn"] : ["all up", "ok"]],
    ["Graphics card", dm.gpu ? [`${dm.gpu.temp_c} °C, ${fmt(dm.gpu.mem_mb)} MB in use`, ""] : ["n/a", ""]],
    ["Latest backup", b ? [`${b.created.replace("T", " ").slice(0, 16)}, ${b.ok ? "verified" : "FAILED"} (${Math.round(b.bytes / 1e6)} MB)`, b.ok ? "" : "warn"] : ["none", "warn"]],
    ["Tamper-evident log", ov.audit_chain.ok ? [`intact (${fmt(ov.audit_chain.rows)} entries)`, "ok"] : [`BROKEN at entry ${ov.audit_chain.broken_at}`, "warn"]],
    ["Devices with access", [(ov.devices || []).filter((d) => !d.revoked_at).map((d) => d.name).join(", "), ""]],
  ].map(([k, [v, cls]]) => el("tr", {}, el("td", {}, k), el("td", { class: cls }, v))));
}
let rt;
addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(rerender, 200); });
function rerender() { renderOverview(); renderStory(); }

// ---- projects: one project's story over time --------------------------------------
// Activity per week, the files its sessions and chats worked on (a file x week grid:
// rows in order of first appearance, square size and shade = how many items touched
// the file that week), and a dated list for the chosen week or file.
let PJ = null, ST = null, pick = null;
const shortPath = (p) => p.split("/").slice(-2).join("/");
const items = (n) => n === 1 ? "1 session or chat" : `${fmt(n)} sessions and chats`;
const cap = (x) => x.charAt(0).toUpperCase() + x.slice(1);
const QLEVELS = [[1, "1"], [2, "2"], [3, "3"], [4, "4 or more"]];
function weekWords(t, bucket) {
  const d = new Date(t + "T00:00:00");
  return bucket === "month" ? d.toLocaleDateString("en", { month: "long", year: "numeric" })
    : "Week of " + d.toLocaleDateString("en", { month: "short", day: "numeric", year: "numeric" });
}
async function loadProjects() {
  PJ = await api("/v1/console/projects");
  const box = $("#p-chips");
  const top = PJ.slice(0, 7), rest = PJ.slice(7);
  box.replaceChildren(el("span", { class: "muted small" }, "Project"), ...top.map((p) => {
    const b = el("button", { type: "button", "data-project": p.project }, p.project);
    b.addEventListener("click", () => openProject(p.project));
    return b;
  }));
  if (rest.length) {
    const sel = el("select", { "aria-label": "More projects" }, el("option", { value: "" }, `${rest.length} more…`),
                   ...rest.map((p) => el("option", { value: p.project }, p.project)));
    sel.addEventListener("change", () => sel.value && openProject(sel.value));
    box.append(sel);
  }
  if (!ST && PJ.length) openProject(PJ[0].project);
}
async function openProject(name) {
  if (!PJ) await loadProjects();
  ST = await api("/v1/console/project-story?name=" + encodeURIComponent(name));
  document.querySelectorAll("#p-chips button").forEach((b) => b.classList.toggle("on", b.dataset.project === name));
  const sel = $("#p-chips select");
  if (sel) sel.value = PJ.slice(7).some((p) => p.project === name) ? name : "";
  const busy = [...ST.columns].reverse().find((c) => c.total > 0);
  pick = busy ? { bucket: busy.t } : null;
  renderStory();
}
function projectStory() {
  const c = ST.counts, kids = [el("b", {}, ST.project)];
  const parts = [[c.sessions, "coding session"], [c.chats, "AI chat"], [c.files, "file or note", "files and notes"]]
    .filter(([n]) => n > 0).map(([n, one, many]) => el("b", {}, `${fmt(n)} ${n === 1 ? one : many || one + "s"}`));
  kids.push(ST.first ? ` started on ${longDate(ST.first)}. Since then: ` : ": ");
  parts.forEach((p, i) => kids.push(p, i < parts.length - 2 ? ", " : i === parts.length - 2 ? " and " : ". "));
  const busiest = ST.columns.reduce((a, b) => (b.total > (a ? a.total : 0) ? b : a), null);
  if (busiest && busiest.total > 1)
    kids.push(`The busiest ${ST.bucket} was ${ST.bucket === "week" ? "the week of " : ""}`,
              new Date(busiest.t + "T00:00:00").toLocaleDateString("en", ST.bucket === "week" ? { month: "short", day: "numeric", year: "numeric" } : { month: "long", year: "numeric" }),
              ` (${items(busiest.total)}). `);
  const most = [...ST.files].sort((a, b) => b.items - a.items)[0];
  if (most) kids.push("The file worked on most is ", el("code", {}, shortPath(most.path)), ` (named in ${items(most.items)}).`);
  $("#p-story").replaceChildren(...kids);
}
function renderStory() {
  if (!ST || $("#tab-projects").hidden) return;
  projectStory();
  $("#p-when-sub").textContent = `Sessions and chats per ${ST.bucket}. Click a ${ST.bucket} to list what happened in it.`;
  document.querySelectorAll(".bucket-word").forEach((w) => (w.textContent = ST.bucket));
  legend("#p-legend", ST.groups);
  stackedColumns("#p-activity", ST.columns, ST.groups, ST.bucket, {
    height: 150, label: `Sessions and chats per ${ST.bucket}`, selected: pick && pick.bucket,
    onPick: (t) => { pick = { bucket: t }; renderStory(); } });
  $("#p-qlegend").replaceChildren(...QLEVELS.map(([lv, label]) => el("span", {},
    el("i", { style: `background:${cssVar("--q" + lv)};width:${7 + lv * 2}px;height:${7 + lv * 2}px` }), label)),
    el("span", {}, el("i", { style: `background:${cssVar("--text")};border-radius:50%;width:6px;height:6px` }), "first time it came up"));
  fileMatrix("#p-matrix");
  table("#pm-table", ["File", "First", "Last", "Sessions and chats"],
        ST.files.map((f) => [f.path, bucketLabel(f.first, ST.bucket), bucketLabel(f.last, ST.bucket), fmt(f.items)]));
  sideList();
}
function fileMatrix(target) {
  const box = $(target), files = ST.files, cols = ST.columns;
  if (!files.length) {
    box.replaceChildren(el("p", { class: "muted small" }, "No repository files are named in this project's sessions or chats, " +
      "so there is no file grid. The list carries the story."));
    return;
  }
  const W = Math.max(480, box.clientWidth), LAB = Math.min(230, Math.round(W * 0.3)), ROW = 28, TOP = 22;
  const cw = Math.min(48, (W - LAB - 6) / cols.length), H = TOP + files.length * ROW + 4, full = Math.min(cw - 2, ROW - 6);
  const right = LAB + cols.length * cw;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Which files were worked on in which weeks" });
  const ci = Object.fromEntries(cols.map((c, i) => [c.t, i]));
  const cx = (i) => LAB + i * cw + cw / 2;
  const every = Math.ceil(64 / cw);
  cols.forEach((c, i) => { if (i % every === 0) svg.append(s("text", { x: cx(i), y: 13, "text-anchor": "middle" }, bucketLabel(c.t, ST.bucket))); });
  if (pick && pick.bucket in ci)
    svg.append(s("rect", { x: LAB + ci[pick.bucket] * cw + 1, y: TOP - 4, width: Math.max(2, cw - 2), height: files.length * ROW + 6, rx: 6, fill: cssVar("--soft") }));
  files.forEach((f, r) => {
    const y = TOP + r * ROW + ROW / 2, on = pick && pick.file === f.path;
    if (on) svg.append(s("rect", { x: 0, y: y - ROW / 2 + 1, width: right, height: ROW - 2, rx: 6, fill: cssVar("--soft") }));
    const label = s("text", { x: LAB - 12, y: y + 4, "text-anchor": "end", class: "plabel rowlabel", tabindex: 0 },
                    s("title", {}, `${f.path}: click to list the sessions and chats that named it`), shortPath(f.path));
    const pickFile = () => { pick = { file: f.path }; renderStory(); };
    label.addEventListener("click", pickFile);
    label.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && pickFile());
    svg.append(s("line", { x1: LAB, x2: right, y1: y, y2: y, stroke: cssVar("--grid") }), label);
    for (const [t, n] of Object.entries(f.cells)) {
      const i = ci[t], lv = Math.min(4, n), sz = Math.max(4, full * (0.45 + 0.1375 * lv));
      const cell = s("rect", { x: cx(i) - sz / 2, y: y - sz / 2, width: sz, height: sz, rx: Math.min(4, sz / 3), fill: cssVar("--q" + lv), class: "cell" });
      const tipIt = (e) => showTip(e, weekWords(t, ST.bucket), [[cssVar("--q" + lv), fmt(n), `session${n === 1 ? "" : "s"} or chat${n === 1 ? "" : "s"} named ${shortPath(f.path)}`]]);
      cell.addEventListener("pointermove", tipIt);
      cell.addEventListener("pointerleave", hideTip);
      cell.addEventListener("click", () => { pick = { bucket: t }; renderStory(); });
      svg.append(cell);
    }
    svg.append(s("circle", { cx: cx(ci[f.first]), cy: y, r: 2.5, fill: cssVar("--text"), "pointer-events": "none" }));
  });
  box.replaceChildren(svg);
}
function sideList() {
  const side = $("#p-side");
  if (!pick) { side.replaceChildren(el("h2", {}, "Nothing dated yet"), el("p", { class: "sub" }, "This project has files and notes but no sessions or chats.")); return; }
  const list0 = pick.file ? ST.items.filter((it) => it.files.includes(pick.file)) : ST.items.filter((it) => it.bucket === pick.bucket);
  const title = pick.file ? shortPath(pick.file) : weekWords(pick.bucket, ST.bucket);
  const sub = pick.file ? `${cap(items(list0.length))} named ${pick.file}, newest first.`
    : `${cap(items(list0.length))}. The dot shows where each happened; tags are the files it touched (click one to follow it).`;
  const list = el("div", { class: "items" });
  for (const it of list0.slice(0, 200)) {
    const tags = el("div", { class: "tags" }, ...it.files.map((f) => {
      const t = el("button", { type: "button", class: "tag", title: f }, shortPath(f));
      t.addEventListener("click", () => { pick = { file: f }; renderStory(); });
      return t;
    }));
    list.append(el("div", { class: "item" }, el("time", {}, bucketLabel(it.when.slice(0, 10), "day")),
      el("i", { class: "dot", style: `background:${groupColor(it.group)}`, title: it.group }),
      el("div", {}, el("div", {}, it.title), it.files.length ? tags : "")));
  }
  if (!list0.length) list.append(el("p", { class: "muted small" }, pick.file ? "None." : `Nothing happened in this ${ST.bucket}. Pick another column.`));
  const kids = [el("h2", {}, title), el("p", { class: "sub" }, sub)];
  if (pick.file) {
    const back = el("button", { class: "link", type: "button" }, "Back to the latest week");
    back.addEventListener("click", () => { const busy = [...ST.columns].reverse().find((c) => c.total > 0); pick = busy ? { bucket: busy.t } : null; renderStory(); });
    kids.push(back);
  }
  side.replaceChildren(...kids, list);
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
