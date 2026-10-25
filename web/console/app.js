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
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many || one + "s"}`;

async function api(path, body) {
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (!token) { showLogin(); throw new Error("no token"); }
  const opts = { headers: { Authorization: "Bearer " + token } };
  if (body !== undefined) Object.assign(opts, { method: "POST", body: JSON.stringify(body) });
  const r = await fetch(path, opts);
  if (r.status === 401 || r.status === 403) {
    showLogin("Token refused (" + r.status + "): run `cmh console` for a fresh link.");
    throw new Error("auth");
  }
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}
function showLogin(msg) { $("#login").hidden = false; if (msg) $("#status").textContent = msg; }
$("#token-save").addEventListener("click", () => {
  sessionStorage.setItem(TOKEN_KEY, $("#token-input").value.trim());
  $("#token-input").value = "";
  $("#login").hidden = true;
  loadReport();
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
const LOADERS = { report: () => loadReport(), projects: () => loadProjects(), activity: () => loadActivity(),
                  search: () => $("#q").focus(), public: () => loadPublic() };
function show(tab) {
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + tab));
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  if (!loaded[tab]) { loaded[tab] = true; LOADERS[tab](); }
  rerender();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea") || $("#passage").open) return;
  const tab = { 1: "report", 2: "projects", 3: "activity", 4: "search", 5: "public" }[e.key];
  if (tab) show(tab);
});

// ---- tooltip (enhances; every value is also in a table view) ---------------------
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

// ---- shared pieces ---------------------------------------------------------------
const GROUP_VARS = { "This laptop": "--s1", "Other machines": "--s2", "claude.ai": "--s3", "Perplexity": "--s4",
                     "Files & notes": "--s5" };
const GROUPS = ["This laptop", "Other machines", "claude.ai", "Perplexity"];
const groupColor = (g) => cssVar(GROUP_VARS[g] || "--neutral");
const SOURCE_WORDS = { "This laptop": "session · this laptop", "Other machines": "session · other machine",
                       "claude.ai": "claude.ai chat", "Perplexity": "Perplexity thread" };
function niceMax(v) {
  if (v <= 4) return 4;
  const p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
}
function bucketLabel(t, bucket) {
  const d = new Date(t.slice(0, 10) + "T00:00:00");
  if (bucket === "month") return d.toLocaleDateString("en", { month: "short", year: "2-digit" });
  return d.toLocaleDateString("en", { month: "short", day: "numeric" });
}
const shortDate = (ts) => bucketLabel(ts, "day");
const longDate = (ts) => new Date(ts.slice(0, 10) + "T00:00:00").toLocaleDateString("en", { month: "short", day: "numeric", year: "numeric" });
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

// Single-series columns (total sessions and chats per period) in ink; the newest
// period in the signal colour. The tooltip gives the split by source.
function columns(target, rows, bucket, opts = {}) {
  const box = $(target), W = Math.max(260, box.clientWidth), H = opts.height || 120, PAD = { l: 26, r: 2, t: 6, b: 22 };
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.label || "chart" });
  const totals = rows.map((r) => GROUPS.reduce((a, g) => a + (r[g] || 0), 0));
  const max = niceMax(Math.max(1, ...totals)), iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const y = (v) => PAD.t + ih - (v / max) * ih;
  const grid = s("g", { class: "grid" });
  for (const v of [0, max / 2, max]) {
    grid.append(s("line", { x1: PAD.l, x2: W - PAD.r, y1: y(v), y2: y(v) }));
    svg.append(s("text", { x: PAD.l - 6, y: y(v) + 4, "text-anchor": "end" }, fmt(v)));
  }
  svg.prepend(grid);
  const band = iw / Math.max(1, rows.length), bw = Math.max(2, Math.min(18, band - 2));
  const every = Math.ceil(rows.length / Math.max(1, Math.floor(iw / 58)));
  rows.forEach((r, i) => {
    const x = PAD.l + i * band + (band - bw) / 2, h = Math.max(totals[i] ? 1 : 0, y(0) - y(totals[i]));
    svg.append(s("rect", { x, y: y(0) - h, width: bw, height: h, fill: i === rows.length - 1 ? cssVar("--signal") : cssVar("--ink") }));
    const hit = s("rect", { class: "hit", x: PAD.l + i * band, y: PAD.t, width: band, height: ih, tabindex: 0,
                            "aria-label": `${bucketLabel(r.t, bucket)}: ${totals[i]}` });
    const tipIt = (e) => showTip(e, `${bucketLabel(r.t, bucket)} · ${fmt(totals[i])} total`,
                                 GROUPS.filter((g) => r[g] > 0).map((g) => [groupColor(g), fmt(r[g]), g]));
    hit.addEventListener("pointermove", tipIt); hit.addEventListener("focus", tipIt);
    hit.addEventListener("pointerleave", hideTip); hit.addEventListener("blur", hideTip);
    svg.append(hit);
    const last = i === rows.length - 1, nearEnd = !last && (rows.length - 1 - i) * band < 48;
    if ((i % every === 0 && !nearEnd) || last)
      svg.append(s("text", { x: PAD.l + i * band + band / 2, y: H - 6, "text-anchor": i === rows.length - 1 ? "end" : "middle" }, bucketLabel(r.t, bucket)));
  });
  svg.append(s("g", { class: "axis" }, s("line", { x1: PAD.l, x2: W - PAD.r, y1: y(0), y2: y(0) })));
  if (!rows.length) svg.append(s("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, "Nothing in this period"));
  box.replaceChildren(svg);
  table(opts.table, ["Period", ...GROUPS, "Total"], rows.map((r, i) => [bucketLabel(r.t, bucket), ...GROUPS.map((g) => fmt(r[g] || 0)), fmt(totals[i])]));
}

// ---- report sections -------------------------------------------------------------
const SECTIONS = [["context", "Context"], ["input", "Input"], ["architecture", "Architecture"],
                  ["results", "Results"], ["findings", "Findings"], ["errors", "Errors"]];
const SHOW_FIRST = 8;
function renderSections(prefix, rep, showProject) {
  const toc = $(`#${prefix}-toc`), main = $(`#${prefix}-main`);
  toc.replaceChildren(...SECTIONS.map(([key, name], i) => {
    const a = el("a", { href: `#${prefix}-sec-${key}`, class: key === "errors" && rep.counts[key] ? "err" : "" },
                 el("span", {}, `${i + 1}.0 ${name}`), el("span", {}, fmt(rep.counts[key])));
    a.addEventListener("click", (e) => { e.preventDefault(); $(`#${prefix}-sec-${key}`).scrollIntoView({ behavior: "smooth", block: "start" }); });
    return a;
  }));
  main.replaceChildren(...SECTIONS.map(([key, name], i) => {
    const items = rep.sections[key];
    const sec = el("section", { class: "sec" + (key === "errors" ? " err" : ""), id: `${prefix}-sec-${key}` },
      el("h2", {}, el("span", { class: "n" }, `${i + 1}.0`), name, el("span", { class: "ct" }, plural(rep.counts[key], "note"))));
    if (!items.length) sec.append(el("p", { class: "empty" }, rep.digested ? "Nothing under this heading in this period."
                                                                          : "No conversations have been digested for this period yet."));
    const list = el("div");
    const draw = (n) => list.replaceChildren(...items.slice(0, n).map((it) => noteRow(it, showProject)));
    draw(SHOW_FIRST);
    sec.append(list);
    if (items.length > SHOW_FIRST) {
      const more = el("button", { class: "more", type: "button" }, `Show all ${items.length}`);
      more.addEventListener("click", () => { draw(items.length); more.remove(); });
      sec.append(more);
    }
    return sec;
  }));
}
function noteRow(it, showProject) {
  const proj = el("button", { class: "p", type: "button", title: "Open this project" }, showProject ? it.project : SOURCE_WORDS[it.group] || it.group);
  if (showProject) proj.addEventListener("click", () => { show("projects"); openProject(it.project); });
  else proj.disabled = true;
  const quote = el("button", { class: "qt", type: "button", title: "Read the passage this quote comes from" }, `“${it.quote}”`);
  quote.addEventListener("click", () => openPassage(it));
  return el("div", { class: "it" }, el("time", {}, shortDate(it.when)), proj,
    el("div", { class: "body-col" }, el("div", { class: "t" }, it.text), quote,
       el("div", { class: "src" }, el("span", { class: "ok" }, "✓ quote found"), ` · ${it.title} · `,
          showProject ? (SOURCE_WORDS[it.group] || it.group) : longDate(it.when))));
}
async function openPassage(it) {
  const d = $("#passage");
  $("#ps-kind").textContent = `${it.project} · ${SOURCE_WORDS[it.group] || it.group} · ${longDate(it.when)}`;
  $("#ps-title").textContent = it.title;
  $("#ps-text").replaceChildren("Loading…");
  d.querySelector(".ps-hide")?.remove();
  d.showModal();
  const p = await api(`/v1/console/passage?doc=${encodeURIComponent(it.doc)}&quote=${encodeURIComponent(it.quote)}`);
  if (!p.found) { $("#ps-text").replaceChildren("The passage could not be found any more (the conversation may have changed)."); return; }
  // Speaker markers in plain words; highlight the quote when it appears with the same
  // spacing, otherwise show the passage as is.
  const text = p.text.replace(/\*\*User:\*\*/g, "You:").replace(/\*\*Assistant:\*\*/g, "Claude:").replace(/\*\*Perplexity:\*\*/g, "Perplexity:");
  const at = text.toLowerCase().indexOf(it.quote.toLowerCase());
  $("#ps-text").replaceChildren(...(at < 0 ? [text]
    : [text.slice(0, at), el("mark", {}, text.slice(at, at + it.quote.length)), text.slice(at + it.quote.length)]));
  $("#ps-text").querySelector("mark")?.scrollIntoView({ block: "center" });
  const hide = el("button", { class: "more", type: "button" }, "Hide this conversation from the report");
  hide.addEventListener("click", async () => {
    await api("/v1/console/report/hide", { doc: it.doc, hidden: true });
    d.close();
    loadReport();
    if (PS) openProject(PS.story.project);
  });
  $("#ps-text").after(el("p", { class: "note ps-hide" }, hide, " Tests and noise can be hidden; nothing is deleted, and the Activity log records it."));
}

// ---- report ----------------------------------------------------------------------
let days = 30, REP = null;
const ago = (secs) => secs == null ? "never" : secs < 120 ? `${secs} seconds ago` : secs < 7200 ? `${Math.round(secs / 60)} minutes ago`
  : `${Math.round(secs / 3600)} hours ago`;
document.querySelectorAll("#r-period button").forEach((b) => b.addEventListener("click", () => {
  days = Number(b.dataset.days);
  document.querySelectorAll("#r-period button").forEach((x) => x.classList.toggle("on", x === b));
  loadReport();
}));
function docket(target, pairs) {
  $(target).replaceChildren(...pairs.map(([k, v, cls]) => el("div", {}, el("dt", {}, k), el("dd", { class: cls || "" }, v))));
}
async function loadReport() {
  const [rep, sm, tl, ov] = await Promise.all([api(`/v1/console/report?days=${days}`), api(`/v1/console/summary?days=${days}`),
                                               api(`/v1/console/timeline?days=${days}`), api("/v1/console/overview")]);
  REP = { rep, sm, tl, ov };
  renderReport();
  $("#status").textContent = "updated " + new Date().toLocaleTimeString("en", { hour: "2-digit", minute: "2-digit" });
}
function renderReport() {
  if (!REP || $("#tab-report").hidden) return;
  const { rep, sm, tl, ov } = REP;
  const intact = ov.audit_chain.ok;
  const late = sm.last_pass_seconds != null && sm.interval_seconds && sm.last_pass_seconds > 3 * sm.interval_seconds + 600;
  docket("#r-docket", [["Period", days ? `last ${days} days` : "all time"], ["Projects active", fmt(days ? sm.projects_in_period : sm.projects)],
    ["Sessions + chats", fmt(rep.conversations)], ["Notes", fmt(Object.values(rep.counts).reduce((a, b) => a + b, 0))],
    ["Status", `${late ? "indexer overdue" : "indexed " + ago(sm.last_pass_seconds)} · log ${intact ? "intact" : "BROKEN"}`, late || !intact ? "warn" : "ok"]]);
  $("#r-title").textContent = days ? `Report: the last ${days} days` : "Report: everything so far";
  const c = rep.counts, kids = [];
  if (rep.conversations) {
    kids.push(`You worked on ${plural(days ? sm.projects_in_period : sm.projects, "project")} in ${plural(rep.conversations, "session or chat", "sessions and chats")}. `);
    if (rep.by_project.length > 1) kids.push("Most of it went into ", el("b", {}, rep.by_project[0][0]), ". ");
    const weekAgo = new Date(Date.now() - 7 * 864e5).toISOString();
    const recentErr = rep.sections.errors.filter((e) => e.when >= weekAgo).length;
    kids.push(`The notes record ${plural(c.results, "result")}, ${plural(c.findings, "finding")} and ${plural(c.errors, "error or open problem", "errors or open problems")}`,
              days !== 7 && recentErr ? `, ${recentErr} of them from the last week.` : ".");
  } else kids.push(`Nothing happened ${days ? `in the last ${days} days` : "yet"}.`);
  $("#r-lede").replaceChildren(...kids);
  renderSections("r", rep, true);
  const dated = tl.rows.filter((r) => GROUPS.some((g) => r[g] > 0));
  $("#r-when-note").textContent = `Sessions and chats per ${tl.bucket}; the latest ${tl.bucket} in red. Hover for the split by source.`;
  columns("#r-when", dated, tl.bucket, { label: "Sessions and chats over time", table: "#rw-table" });
  $("#r-projects").replaceChildren(...rep.by_project.slice(0, 12).map(([p, n]) => {
    const b = el("button", { class: "stat", type: "button" }, el("span", {}, p), el("b", {}, fmt(n)));
    b.addEventListener("click", () => { show("projects"); openProject(p); });
    return b;
  }));
  const missing = rep.conversations - rep.digested;
  $("#r-coverage").textContent = !rep.conversations ? "" : missing
    ? `Digested so far: ${fmt(rep.digested)} of ${fmt(rep.conversations)} conversations in this period. The rest are queued (${fmt(rep.pending_total)} in all); the model works through them a few minutes at a time, so notes keep arriving.`
    : `All ${fmt(rep.conversations)} conversations in this period are digested.`;
}
let rt;
addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(rerender, 200); });
function rerender() { renderReport(); renderProject(); }

// ---- projects --------------------------------------------------------------------
let PJ = null, PS = null;
async function loadProjects() {
  PJ = await api("/v1/console/projects");
  const box = $("#p-chips"), top = PJ.slice(0, 7), rest = PJ.slice(7);
  box.replaceChildren(...top.map((p) => {
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
  if (!PS && PJ.length) openProject(PJ[0].project);
}
async function openProject(name) {
  if (!PJ) await loadProjects();
  const [story, rep] = await Promise.all([api("/v1/console/project-story?name=" + encodeURIComponent(name)),
                                          api(`/v1/console/report?days=0&project=${encodeURIComponent(name)}`)]);
  PS = { story, rep };
  document.querySelectorAll("#p-chips button").forEach((b) => b.classList.toggle("on", b.dataset.project === name));
  const sel = $("#p-chips select");
  if (sel) sel.value = PJ.slice(7).some((p) => p.project === name) ? name : "";
  renderProject();
}
function renderProject() {
  if (!PS || $("#tab-projects").hidden) return;
  const { story: st, rep } = PS, c = st.counts;
  docket("#p-docket", [["Started", st.first ? longDate(st.first) : "–"], ["Coding sessions", fmt(c.sessions)], ["AI chats", fmt(c.chats)],
    ["Files and notes", fmt(c.files)], ["Notes", fmt(Object.values(rep.counts).reduce((a, b) => a + b, 0))]]);
  $("#p-title").textContent = st.project;
  const busiest = st.columns.reduce((a, b) => (b.total > (a ? a.total : 0) ? b : a), null);
  const kids = [];
  if (st.items.length) {
    kids.push(`Last worked on ${longDate(st.items[0].when)}: “${st.items[0].title}”. `);
    if (busiest && busiest.total > 1)
      kids.push(`The busiest ${st.bucket} was ${st.bucket === "week" ? "the week of " + longDate(busiest.t) : bucketLabel(busiest.t, "month")} with ${plural(busiest.total, "session or chat", "sessions and chats")}. `);
    kids.push(`Below: what it is about, what you asked, how it is built, what came out, and what went wrong, from ${plural(rep.digested, "digested conversation")}.`);
  } else kids.push("Only files and notes so far: no dated sessions or chats.");
  $("#p-lede").replaceChildren(...kids);
  renderSections("p", rep, false);
  $("#p-when-title").textContent = "When";
  $("#p-when-note").textContent = `Sessions and chats per ${st.bucket}; the latest in red.`;
  columns("#p-when", st.columns, st.bucket, { label: `Sessions and chats per ${st.bucket}`, table: "#pw-table" });
  $("#p-latest").replaceChildren(...st.items.slice(0, 6).map((it) => el("div", { class: "latest" },
    el("time", {}, `${longDate(it.when)} · ${SOURCE_WORDS[it.group] || it.group}`), el("span", {}, it.title))));
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
  health(ov);
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

function health(ov) {
  const m = $("#machines");
  m.replaceChildren(el("tr", {}, el("th", {}, "Machine"), el("th", { class: "n" }, "Sessions"), el("th", { class: "n" }, "Latest")));
  for (const r of ov.machines || []) m.append(el("tr", {}, el("td", {}, r.machine), el("td", { class: "n" }, fmt(r.sessions)),
                                                     el("td", { class: "n" }, (r.updated || "").slice(0, 10))));
  const dm = ov.daemon || {}, b = ov.backup;
  const tunnels = Object.entries(dm.tunnels || {});
  const down = tunnels.filter(([, t]) => t.state !== "up");
  $("#health").replaceChildren(...[
    ["Indexer", dm.seconds_since_pass == null ? ["not run yet", "warn"] : [`running, last pass ${ago(dm.seconds_since_pass)}`, dm.status === "ok" ? "ok" : "warn"]],
    ["Tunnels to servers", !tunnels.length ? ["none set up", ""] : down.length
      ? [`${down.map(([n]) => n).join(", ")} down (${tunnels.length - down.length} of ${tunnels.length} up)`, "warn"] : ["all up", "ok"]],
    ["Graphics card", dm.gpu ? [`${dm.gpu.temp_c} °C, ${fmt(dm.gpu.mem_mb)} MB in use`, ""] : ["n/a", ""]],
    ["Latest backup", b ? [`${b.created.replace("T", " ").slice(0, 16)}, ${b.ok ? "verified" : "FAILED"} (${Math.round(b.bytes / 1e6)} MB)`, b.ok ? "" : "warn"] : ["none", "warn"]],
    ["Tamper-evident log", ov.audit_chain.ok ? [`intact (${fmt(ov.audit_chain.rows)} entries)`, "ok"] : [`BROKEN at entry ${ov.audit_chain.broken_at}`, "warn"]],
    ["Devices with access", [(ov.devices || []).filter((d) => !d.revoked_at).map((d) => d.name).join(", "), ""]],
  ].map(([k, [v, cls]]) => el("tr", {}, el("td", {}, k), el("td", { class: cls }, v))));
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

if (sessionStorage.getItem(TOKEN_KEY)) { loaded.report = true; loadReport().catch(() => {}); } else showLogin();
