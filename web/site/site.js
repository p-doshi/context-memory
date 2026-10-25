// Public site. Content is untrusted and only ever inserted as text.
"use strict";

const $ = (s) => document.querySelector(s);
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
function el(tag, cls, ...kids) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(String(k ?? "")));
  return e;
}
const fmt = (n) => Number(n).toLocaleString("en");
const ms = (v) => (v == null ? "–" : v < 10 ? `${v.toFixed(1)} ms` : `${Math.round(v)} ms`);

// API origin: "" when this laptop serves the page; the API host when the
// static site is hosted elsewhere (config.json is written at deploy time).
let API = "";
const loadConfig = () => fetch("/config.json", { cache: "no-store" }).then((r) => r.json())
  .then((c) => { API = c.api || ""; }).catch(() => {});
let apiReady = loadConfig();
async function getJSON(path, opts) {
  await apiReady;
  const url = () => (path.startsWith("/api/") ? API : "") + path;
  let r;
  try { r = await fetch(url(), opts); } catch (e) {
    // The tunnel address changes when the owner's laptop restarts; a page opened
    // before that holds the old one. Re-read it once and retry.
    if (!path.startsWith("/api/")) throw e;
    const before = API;
    apiReady = loadConfig();
    await apiReady;
    if (API === before) throw e;
    r = await fetch(url(), opts);
  }
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}

// ---- theme ---------------------------------------------------------------------
const THEMES = ["system", "light", "dark"];
function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("#theme")?.setAttribute("aria-label", "Colour theme: " + t);
}
let theme = "system";
try { theme = localStorage.getItem("cmh-theme") || "system"; } catch {}
applyTheme(theme);
$("#theme")?.addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme);
  try { localStorage.setItem("cmh-theme", theme); } catch {}
});
document.documentElement.classList.add("js");

// ---- Figure 1: the transit map -----------------------------------------------------
// Grey lines are every source feeding the memory; the signal line is one question's
// path. Station tags show times measured on this page's own search.
const SVG = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, ...kids) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}
const SOURCES = ["Claude Code · laptop", "Claude Code · lab server", "claude.ai", "Perplexity", "Code + notes", "XPS laptop"];
const STOP_NAMES = ["SSH link", "Hub", "Gate", "Search", "Verify", "Answer"];
let TIMES = null;   // { link, hub, gate, search, verify, answer } as tag strings

function tagFor(i) {
  if (!TIMES) return i === 0 ? "encrypted" : "…";
  return [TIMES.link, TIMES.hub, TIMES.gate, TIMES.search, TIMES.verify, TIMES.answer][i];
}
function stationTag(g, x, y, text) {
  const w = text.length * 7.1 + 14;
  const t = svg("g", { class: "tag" });
  t.append(svg("rect", { x: x - w / 2, y, width: w, height: 19 }), svg("text", { x, y: y + 13.5, "text-anchor": "middle" }, text));
  g.append(t);
  return t;
}
function drawTransit() {
  const root = $("#transit");
  if (!root) return;
  const wide = root.clientWidth >= 600;
  root.replaceChildren();
  const delay = (i) => `${450 + i * 170}ms`;
  if (wide) {
    const W = 790, H = 480, ty = 240, xs = [250, 345, 445, 545, 640, 728];
    root.setAttribute("viewBox", `0 0 ${W} ${H}`);
    ["Sources", "Link", "Hub", "Retrieval", "Answer"].forEach((h, i) =>
      root.append(svg("text", { class: "colh", x: [40, 250, 345, 545, 728][i], y: 14, "text-anchor": i ? "middle" : "start" }, h.toUpperCase())));
    const feed = (y) => `M40,${y} L${xs[0] - 70},${y} Q${xs[0] - 24},${y} ${xs[0] - 12},${(y + ty) / 2} T${xs[0]},${ty}`;
    SOURCES.forEach((name, i) => {
      const y = 70 + i * 68;
      root.append(svg("path", { class: "rest", d: feed(y) }));
      root.append(svg("rect", { class: "src", x: 35, y: y - 5, width: 10, height: 10 }));
      root.append(svg("text", { class: "lab", x: 52, y: y - 10 }, name));
    });
    for (const [x, y2, label] of [[xs[1], 92, "audit log, sealed chain"], [xs[1], 392, "nightly verified backup"],
                                  [xs[3], 122, "knowledge graph"], [xs[3], 380, "public copy, opt-in projects"]]) {
      root.append(svg("line", { class: "rest", x1: x, y1: ty, x2: x, y2 }));
      root.append(svg("circle", { class: "src", cx: x, cy: y2, r: 5 }));
      root.append(svg("text", { class: "lab", x: x + 12, y: y2 + 4 }, label));
    }
    const path = svg("path", { class: "path", d: `${feed(70)} L${xs[5]},${ty}` });
    root.append(path);
    path.style.setProperty("--len", Math.ceil(path.getTotalLength()));
    root.append(svg("circle", { cx: 40, cy: 70, r: 7, fill: "var(--signal)" }));
    STOP_NAMES.forEach((name, i) => {
      const x = xs[i], below = i % 2 === 0;
      const g = svg("g");
      const ring = svg("circle", { class: "ring", cx: x, cy: ty, r: 10 });
      ring.style.animationDelay = delay(i);
      g.append(ring, svg("circle", { class: "hub", cx: x, cy: ty, r: 3.5 }));
      const ly = below ? ty + 38 : ty - 32;
      g.append(svg("text", { class: "stn", x, y: ly, "text-anchor": "middle", "paint-order": "stroke", stroke: "var(--paper)", "stroke-width": 5 }, name));
      stationTag(g, x, ly + 7, tagFor(i)).style.animationDelay = delay(i);
      root.append(g);
    });
  } else {
    // Phones: the line runs down the page with labels on the right.
    const W = 340, step = 66, top = 70, H = top + STOP_NAMES.length * step;
    root.setAttribute("viewBox", `0 0 ${W} ${H}`);
    root.append(svg("rect", { class: "src", x: 16, y: 10, width: 220, height: 34 }));
    root.append(svg("text", { class: "lab", x: 28, y: 32 }, "6 sources: sessions, chats, code"));
    const path = svg("path", { class: "path", d: `M40,44 L40,${top + (STOP_NAMES.length - 1) * step}` });
    root.append(path);
    path.style.setProperty("--len", Math.ceil(path.getTotalLength()));
    STOP_NAMES.forEach((name, i) => {
      const y = top + i * step, g = svg("g");
      const ring = svg("circle", { class: "ring", cx: 40, cy: y, r: 10 });
      ring.style.animationDelay = delay(i);
      g.append(ring, svg("circle", { class: "hub", cx: 40, cy: y, r: 3.5 }), svg("text", { class: "stn", x: 64, y: y + 5 }, name));
      const tag = tagFor(i), w = tag.length * 7.1 + 14;
      const t = svg("g", { class: "tag" });
      t.append(svg("rect", { x: 160, y: y - 10, width: w, height: 19 }), svg("text", { x: 167, y: y + 4 }, tag));
      t.style.animationDelay = delay(i);
      g.append(t);
      root.append(g);
    });
  }
}
let transitWide = null, rt;
function maybeRedraw() {
  const w = $("#transit")?.clientWidth >= 600;
  if (w !== transitWide) { transitWide = w; drawTransit(); }
  drawRail();
}
addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(maybeRedraw, 150); });

// A real search against the public copy, timed by stage on the server and as a round
// trip here; its numbers fill the figure and the stops below.
const SAMPLE = "how does the disclosure gate keep private data out of answers";
async function measure() {
  try {
    const t0 = performance.now();
    const res = await getJSON("/api/search?q=" + encodeURIComponent(SAMPLE));
    const trip = performance.now() - t0, t = res.timings_ms || {};
    const search = (t.embed || 0) + (t.vector || 0) + (t.bm25 || 0);
    const hub = (t.hydrate || 0) + (t.audit || 0);
    const verified = res.hits.filter((h) => h.status === "verified").length;
    TIMES = { link: "encrypted", hub: ms(hub), gate: ms(t.gate), search: ms(search), verify: ms(t.verify), answer: `${res.hits.length} cited` };
    drawTransit();
    $("#fig-sum").textContent = `measured just now · ${ms(t.total)} on the server · ${ms(trip)} round trip`;
    const docs = new Set(res.hits.map((h) => h.source)).size;
    $("#came").replaceChildren(`${res.hits.length} passages from `, el("code", "", `${docs} documents`),
      ", each with the file it came from and whether that file still says so.");
    $("#metrics").replaceChildren(el("span", "", "question: ", el("b", "", `“${SAMPLE}”`)));
    for (const [id, v] of [["#t-gate", ms(t.gate)], ["#t-search", ms(search)], ["#t-total", ms(t.total)],
                           ["#t-verified", `${verified} of ${res.hits.length}`]]) { const n = $(id); if (n) n.textContent = v; }
  } catch {
    $("#fig-sum").textContent = "live memory offline: showing the layout only";
    $("#came").textContent = "The owner's laptop is offline or restarting, so no live search could run. The figure shows the route a question takes.";
  }
}

// ---- 1.0 the path, stop by stop: a rail that fills as you read --------------------------
const STOP_SHORT = ["Sources", "SSH", "Hub", "Gate", "Search", "Verify", "Answer"];
let railProgress = REDUCED ? 1 : 0;
function drawRail() {
  const root = $("#rail");
  if (!root) return;
  const W = root.clientWidth || 100, H = root.clientHeight || 500, n = STOP_SHORT.length, pad = 14;
  const y = (i) => pad + (i * (H - 2 * pad)) / (n - 1);
  root.setAttribute("viewBox", `0 0 ${W} ${H}`);
  root.replaceChildren(svg("line", { class: "bg", x1: 14, x2: 14, y1: y(0), y2: y(n - 1) }),
                       svg("line", { class: "fg", id: "rail-fg", x1: 14, x2: 14, y1: y(0), y2: y(0) }));
  STOP_SHORT.forEach((name, i) => {
    root.append(svg("circle", { cx: 14, cy: y(i), r: 7, "data-i": i }));
    if (W > 60) root.append(svg("text", { x: 30, y: y(i) + 4, "data-i": i }, name));
  });
  paintRail();
}
function paintRail() {
  const root = $("#rail");
  if (!root) return;
  const H = root.clientHeight || 500, n = STOP_SHORT.length, pad = 14;
  const fg = $("#rail-fg");
  if (fg) fg.setAttribute("y2", pad + railProgress * (H - 2 * pad));
  const reached = Math.round(railProgress * (n - 1) + 0.0001);
  root.querySelectorAll("[data-i]").forEach((e) => e.classList.toggle("on", Number(e.dataset.i) <= reached));
  document.querySelectorAll(".stop").forEach((s) => s.classList.toggle("on", REDUCED || Number(s.dataset.stop) <= reached));
}
function onScroll() {
  const stops = [...document.querySelectorAll(".stop")];
  if (!stops.length) return;
  const mid = innerHeight * 0.5;
  let p = 0;
  stops.forEach((s, i) => {
    const r = s.getBoundingClientRect();
    if (r.top <= mid) p = i + Math.min(1, (mid - r.top) / Math.max(1, r.height));
  });
  railProgress = Math.min(1, Math.max(0, (p - 0.5) / (stops.length - 1)));
  paintRail();
}
if (!REDUCED) {
  let ticking = false;
  addEventListener("scroll", () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; onScroll(); });
  }, { passive: true });
}

// ---- live status in the docket ---------------------------------------------------
async function liveStatus() {
  try {
    const st = await getJSON("/api/stats");
    STATS = st;
    $("#status").replaceChildren(el("i", "live"), st.answer_model === "local" ? "live, answer model on" : "live, excerpts only");
    if (st.exported) $("#exported").textContent = `${st.exported.slice(0, 4)}-${st.exported.slice(4, 6)}-${st.exported.slice(6, 8)} ${st.exported.slice(9, 11)}:${st.exported.slice(11, 13)} UTC`;
    const n = $("#t-chunks"); if (n) n.textContent = fmt(st.chunks);
    $("#docno").textContent = `Report · ${new Date().toISOString().slice(0, 10)}`;
  } catch {
    $("#status").replaceChildren(el("i", "live off"), "laptop offline or restarting");
  }
}

// ---- technical details: the ten stages --------------------------------------------
let PIPE = null, STATS = null, current = 0;
function statFor(stage) {
  if (!STATS || !stage.stat) return null;
  if (stage.stat === "documents") return fmt(Object.values(STATS.documents).reduce((a, b) => a + b, 0)) + " documents";
  if (stage.stat === "chunks") return fmt(STATS.chunks) + " passages";
  if (stage.stat === "vectors") return fmt(STATS.vectors) + " vectors";
  if (stage.stat === "graph") return fmt(Object.values(STATS.graph).reduce((a, b) => a + b, 0)) + " links";
  return null;
}
function renderPipeline() {
  $("#pipeline").replaceChildren(...PIPE.stages.map((s, i) => {
    const b = el("button", "stage", el("span", "num", String(i + 1).padStart(2, "0")), el("span", "title", s.title));
    const st = statFor(s);
    if (st) b.append(el("span", "stat", st));
    b.type = "button";
    b.setAttribute("aria-controls", "detail");
    b.setAttribute("aria-pressed", String(i === current));
    b.addEventListener("click", () => select(i));
    const li = el("li", "", b);
    li.style.setProperty("--i", i);
    return li;
  }));
}
function detailContent(i) {
  const s = PIPE.stages[i];
  const where = el("ul", "where", ...s.where.map((w) => {
    const a = el("a", "", w);
    a.href = PIPE.repository + "/tree/main/" + w;
    a.rel = "noopener";
    return el("li", "", a);
  }));
  const kids = [el("h3", "", String(i + 1).padStart(2, "0") + " · " + s.title), el("p", "", s.what),
                el("p", "label", "In the code"), where];
  if (s.citation) {
    const a = el("a", "", "doi:" + s.citation.doi);
    a.href = "https://doi.org/" + s.citation.doi;
    a.rel = "noopener";
    kids.push(el("p", "cite small", s.citation.text + ". ", a));
  }
  return kids;
}
function select(i, instant = false) {
  current = i;
  document.querySelectorAll(".stage").forEach((b, j) => b.setAttribute("aria-pressed", String(i === j)));
  const d = $("#detail");
  if (instant || REDUCED) { d.replaceChildren(...detailContent(i)); return; }
  d.classList.add("swap");  // a 2px blur hides the crossfade between two contents
  setTimeout(() => { d.replaceChildren(...detailContent(i)); d.classList.remove("swap"); }, 120);
}
async function loadPipeline() {
  PIPE = await getJSON("/pipeline.json");
  const repo = $("#repo");
  if (repo) repo.href = PIPE.repository;
  renderPipeline();
  select(0, true);
  try {
    STATS = await getJSON("/api/stats");
    renderPipeline();
    const docs = Object.values(STATS.documents).reduce((a, b) => a + b, 0);
    const lc = $("#livecount");
    if (lc) lc.textContent = `live: ${fmt(docs)} public documents · ${fmt(STATS.chunks)} passages`;
    if (STATS.answer_model !== "local")
      $("#ask-note").textContent = "The answer model is offline right now, so answers list the most relevant passages.";
  } catch {
    $("#ask-note").textContent = "The live memory is offline right now; everything else on this page still works.";
  }
}

// ---- privacy scenario ------------------------------------------------------------------
const READERS = [
  ["owner", "You", "You, on your laptop", "Sees everything. It's your memory."],
  ["cloud_model", "AI", "A cloud AI, e.g. Claude Code", "Gets the knowledge, not your personal details."],
  ["public", "WEB", "Visitors to this website", "Same filter, and only documents chosen for publication."],
];
const HIDDEN = { email: "email hidden", phone: "phone number hidden", street_address: "address hidden",
                 postal_code_ca: "postal code hidden", payment_card: "card number hidden", coordinates: "location hidden",
                 iban: "bank account hidden", government_id: "ID number hidden", date_of_birth: "birth date hidden" };
function noteWithPills(text) {
  const p = el("p", "note");
  let last = 0;
  for (const m of text.matchAll(/\[WITHHELD:([^\]:]+):([^\]]+)\]/g)) {
    p.append(text.slice(last, m.index), el("span", "pill", HIDDEN[m[2]] || m[2].replace(/_/g, " ") + " hidden"));
    last = m.index + m[0].length;
  }
  p.append(text.slice(last));
  return p;
}
let gateExample = "contact";
async function runGate() {
  const out = $("#gate-out");
  document.querySelectorAll("#gate-presets .chip").forEach((c) => c.setAttribute("aria-pressed", String(c.dataset.example === gateExample)));
  try {
    // Fixed examples only: the server refuses free text, so the gate cannot be probed for gaps.
    const res = await getJSON("/api/gate", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ example: gateExample }) });
    out.replaceChildren(...READERS.map(([key, av, who, sub], i) => {
      const card = el("div", "gate-card",
        el("div", "who", el("span", "av", av), el("div", "", el("b", "", who), el("span", "", sub))),
        noteWithPills(res[key].text));
      card.style.setProperty("--i", i);
      return card;
    }));
  } catch {
    out.replaceChildren(el("p", "muted", "This demo needs the live memory, which is offline right now."));
  }
}
document.querySelectorAll("#gate-presets .chip").forEach((c) => c.addEventListener("click", () => {
  gateExample = c.dataset.example;
  runGate();
}));
if ($("#gate-presets")) {
  // Show the first example once the section comes into view, without a click.
  const g = $("#gate-presets").closest("section");
  if ("IntersectionObserver" in window) {
    const once = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) { once.disconnect(); runGate(); } });
    once.observe(g);
  } else runGate();
}

// ---- ask (AG-UI client) ---------------------------------------------------------------
const threadId = crypto.randomUUID();
const history = [];
const IDK = "I don't know that from the project's public documents.";

function cleanText(s) {
  // Markdown tables, emphasis, code ticks and headings read badly as excerpts.
  return s.replace(/\|?\s*-{3,}\s*(\|\s*-{3,}\s*)+\|?/g, " ")
          .replace(/\s*\|\s*/g, " · ").replace(/[*`#>]+/g, "").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
          .replace(/\s+/g, " ").replace(/^ ?· /, "").trim();
}
function describe(h) {
  // "path/to/file.py › L12-L80 · def run" -> file.py / "def run · lines 12–80"
  const parts = (h.where || h.title || "").split(" › ");
  if (h.kind === "code") {
    const file = (parts[0] || h.title || "").split("/").pop();
    const m = /L(\d+)-L(\d+)(?: · (.*))?/.exec(parts.slice(1).join(" › "));
    return [file, m ? `${(m[3] || "code").split(",")[0]} · lines ${m[1]}–${m[2]}` : "code"];
  }
  // Section headings are often numbered ("5. Disclosure policy engine"); the card adds its own number.
  const title = (parts[parts.length - 1] || h.title || "").replace(/^\d+(\.\d+)*\.?\s+/, "");
  return [title, parts.length > 1 ? parts[0] : "documentation"];
}
function sourceCard(h, n) {
  const [t, s] = describe(h);
  const raw = h.snippet || "";
  const x = h.kind === "code" ? el("code", "x", raw.slice(0, 400)) : el("div", "x", cleanText(raw).slice(0, 420));
  const b = el("button", "src", el("div", "t", `${n}. ${t}`),
               el("div", "s", s + " · ", el("span", "badge " + h.status, h.status)), x);
  b.type = "button";
  b.dataset.n = n;
  b.addEventListener("click", () => b.classList.toggle("open"));
  return b;
}
function answerWithCitations(text, onCite) {
  const d = el("div", "msg bot");
  let last = 0;
  for (const m of text.matchAll(/\[(\d+)\]/g)) {
    d.append(text.slice(last, m.index));
    const c = el("button", "cn", m[1]);
    c.type = "button";
    c.setAttribute("aria-label", "Show source " + m[1]);
    c.addEventListener("click", () => onCite(Number(m[1])));
    d.append(c);
    last = m.index + m[0].length;
  }
  d.append(text.slice(last));
  return d;
}
function scrollLog() { const l = $("#log"); l.scrollTop = l.scrollHeight; }

async function ask(question) {
  history.push({ id: crypto.randomUUID(), role: "user", content: question });
  const log = $("#log");
  log.append(el("div", "msg user", question));
  const typing = el("div", "typing", el("i"), el("i"), el("i"));
  typing.setAttribute("aria-label", "Writing an answer");
  log.append(typing);
  scrollLog();
  const btn = $("#ask-form button");
  btn.disabled = true;
  let text = "", hits = [];
  try {
    await apiReady;
    // AG-UI events as one JSON array (Cloudflare quick tunnels do not carry server-sent events).
    const r = await fetch(API + "/api/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ threadId, runId: crypto.randomUUID(), messages: history, tools: [], context: [], state: {} }),
    });
    typing.remove();
    if (r.status === 429) { log.append(el("div", "msg bot", "Lots of questions right now. Please try again in a minute.")); return; }
    if (!r.ok) { log.append(el("div", "msg bot", "The project memory is offline right now. The rest of the page still works.")); return; }
    const { events } = await r.json();
    for (const ev of events) {
      if (ev.type === "TOOL_CALL_RESULT") hits = JSON.parse(ev.content).hits || [];
      else if (ev.type === "TEXT_MESSAGE_CONTENT") text += ev.delta;
    }
    text = text.trim() || IDK;
    history.push({ id: crypto.randomUUID(), role: "assistant", content: text });
    // Show only the sources the answer cites; with the model offline (extractive
    // answers, no citations) show the top three.
    const cited = [...new Set([...text.matchAll(/\[(\d+)\]/g)].map((m) => Number(m[1])))]
      .filter((n) => n >= 1 && n <= hits.length);
    const extractive = STATS && STATS.answer_model !== "local";
    const shown = text.startsWith(IDK) ? [] : (cited.length ? cited : (extractive ? [1, 2, 3].filter((n) => n <= hits.length) : []));
    let srcs = null;
    const onCite = (n) => {
      const card = srcs?.querySelector(`.src[data-n="${n}"]`);
      if (!card) return;
      srcs.querySelectorAll(".src").forEach((c) => c.classList.toggle("hl", c === card));
      card.classList.add("open");
      card.scrollIntoView({ block: "nearest", behavior: REDUCED ? "auto" : "smooth" });
    };
    log.append(answerWithCitations(text, onCite));
    if (shown.length) {
      srcs = el("div", "srcs", el("div", "srcs-h", shown.length === 1 ? "Source" : "Sources"),
                ...shown.map((n) => sourceCard(hits[n - 1], n)));
      log.append(srcs);
    }
    scrollLog();
  } catch (e) {
    typing.remove();
    // A burst can be refused by the tunnel before it reaches the server, without the
    // headers a browser needs to read the reply; it surfaces here as a network error.
    log.append(el("div", "msg bot", e instanceof TypeError
      ? "Too many questions at once, or the memory is offline. Please wait a moment and try again."
      : "Something went wrong: " + e.message));
  } finally {
    btn.disabled = false;
  }
}
$("#ask-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#question").value.trim();
  if (q) { $("#question").value = ""; ask(q); }
});
document.querySelectorAll("#suggest .chip").forEach((c) => c.addEventListener("click", () => ask(c.textContent)));

// ---- copy buttons on code blocks -----------------------------------------------------
document.querySelectorAll("pre").forEach((pre) => {
  const b = el("button", "copy", "Copy");
  b.type = "button";
  b.setAttribute("aria-label", "Copy commands");
  b.addEventListener("click", async () => {
    const lines = pre.innerText.split("\n").filter((l) => !l.trim().startsWith("#") && l.trim() !== "Copy" && l.trim() !== "Copied");
    try { await navigator.clipboard.writeText(lines.join("\n").trim()); } catch { return; }
    b.classList.add("swap");
    setTimeout(() => { b.textContent = "Copied"; b.classList.remove("swap"); }, 100);
    setTimeout(() => { b.textContent = "Copy"; }, 1600);
  });
  pre.append(b);
});

if ($("#pipeline")) loadPipeline().catch(() => { $("#pipeline").replaceChildren(el("li", "muted", "Could not load the technical details.")); });

// ---- start --------------------------------------------------------------------------
maybeRedraw();
if ($("#transit")) { measure(); liveStatus(); onScroll(); }
