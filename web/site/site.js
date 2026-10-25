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

// API origin: "" when this laptop serves the page; the API host when the
// static site is hosted elsewhere (config.json is written at deploy time).
let API = "";
const apiReady = fetch("/config.json").then((r) => r.json()).then((c) => { API = c.api || ""; }).catch(() => {});
async function getJSON(path, opts) {
  await apiReady;
  const r = await fetch((path.startsWith("/api/") ? API : "") + path, opts);
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

// ---- reveal on scroll (decorative; content is visible without JS) --------------
document.documentElement.classList.add("js");
const sections = document.querySelectorAll(".reveal");
if ("IntersectionObserver" in window) {
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
  }, { rootMargin: "0px 0px -60px 0px", threshold: 0.08 });
  sections.forEach((s) => io.observe(s));
} else sections.forEach((s) => s.classList.add("in"));

// ---- flow diagram ---------------------------------------------------------------
const SVG = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, ...kids) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}
const SOURCES = [["Claude Code", "every machine"], ["claude.ai", "web + desktop"], ["Perplexity", "threads"],
                 ["Code", "git repositories"], ["Docs", "Markdown"]];
const OUTS = [["Claude Code", "any machine, via SSH"], ["Owner console", "private"], ["This site", "public copy"]];

function node(x, y, w, h, title, sub, cls = "node") {
  const g = svg("g", { class: cls, transform: `translate(${x},${y})` });
  g.append(svg("rect", { width: w, height: h, rx: 10 }));
  g.append(svg("text", { x: w / 2, y: sub ? h / 2 - 3 : h / 2 + 4, "text-anchor": "middle" }, title));
  if (sub) g.append(svg("text", { class: "sub", x: w / 2, y: h / 2 + 13, "text-anchor": "middle" }, sub));
  return g;
}

function drawFlow() {
  const root = $("#flow");
  if (!root) return;
  const wide = root.clientWidth >= 640;
  root.replaceChildren(root.querySelector("title") || svg("title", {}, "Data flow"));
  const W = wide ? 1000 : 360, H = wide ? 330 : 560;
  root.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const paths = svg("g"), nodes = svg("g"), dots = svg("g");
  root.append(paths, dots, nodes);
  let core, ins = [], outs = [];
  if (wide) {
    core = { x: 410, y: 110, w: 180, h: 110 };
    SOURCES.forEach(([t, s], i) => { const y = 14 + i * 62; nodes.append(node(20, y, 170, 46, t, s)); ins.push([190, y + 23]); });
    OUTS.forEach(([t, s], i) => { const y = 64 + i * 80; nodes.append(node(810, y, 170, 46, t, s)); outs.push([810, y + 23]); });
  } else {
    core = { x: 90, y: 230, w: 180, h: 100 };
    SOURCES.forEach(([t], i) => {
      const row = i < 3 ? 0 : 1, col = i < 3 ? i : i - 3 + 0.5, x = 6 + col * 118, y = 10 + row * 60;
      nodes.append(node(x, y, 112, 44, t, "")); ins.push([x + 56, y + 44]);
    });
    OUTS.forEach(([t], i) => { const x = 6 + i * 118, y = 490; nodes.append(node(x, y, 112, 44, t, "")); outs.push([x + 56, y]); });
  }
  const cx = core.x + core.w / 2, cy = core.y + core.h / 2;
  const cin = wide ? [core.x, cy] : [cx, core.y], cout = wide ? [core.x + core.w, cy] : [cx, core.y + core.h];
  const curve = ([x1, y1], [x2, y2]) => wide
    ? `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}`
    : `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`;
  const all = [...ins.map((p) => curve(p, cin)), ...outs.map((p) => curve(cout, p))];
  all.forEach((d, i) => {
    const id = `fp${i}`;
    const p = svg("path", { id, d, class: "path draw" });
    paths.append(p);
    p.style.setProperty("--len", Math.ceil(p.getTotalLength()));
    p.style.animationDelay = `${i * 70}ms`;
    // Constant motion is linear; each path carries two dots, half a cycle apart.
    for (const off of [0, 1.6]) {
      const c = svg("circle", { r: 3.2, class: "dot", opacity: 0 });
      const begin = `${1.2 + i * 0.25 + off}s`;
      c.append(svg("animateMotion", { dur: "3.2s", repeatCount: "indefinite", begin }, svg("mpath", { href: `#${id}` })));
      c.append(svg("animate", { attributeName: "opacity", values: "0;1;1;0", keyTimes: "0;0.12;0.85;1",
                                dur: "3.2s", repeatCount: "indefinite", begin }));
      dots.append(c);
    }
  });
  const g = node(core.x, core.y, core.w, core.h, "one local store", "SQLite · LanceDB · graph", "node core");
  g.insertBefore(svg("rect", { class: "pulse", width: core.w, height: core.h, rx: 12 }), g.firstChild);
  nodes.append(g);
}
let flowWide = null;
function maybeRedraw() {
  const w = $("#flow")?.clientWidth >= 640;
  if (w !== flowWide) { flowWide = w; drawFlow(); }
}
maybeRedraw();
let rt;
addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(maybeRedraw, 150); });

// ---- live stats -------------------------------------------------------------------
function countUp(target, value) {
  if (REDUCED) { target.textContent = fmt(value); return; }
  const t0 = performance.now(), dur = 900;
  const tick = (t) => {
    const k = Math.min(1, (t - t0) / dur), eased = 1 - Math.pow(1 - k, 3);
    target.textContent = fmt(Math.round(value * eased));
    if (k < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// ---- pipeline ---------------------------------------------------------------------
let PIPE = null, STATS = null, current = 0;
function statFor(stage) {
  if (!STATS || !stage.stat) return null;
  if (stage.stat === "documents") return fmt(Object.values(STATS.documents).reduce((a, b) => a + b, 0)) + " documents";
  if (stage.stat === "chunks") return fmt(STATS.chunks) + " chunks";
  if (stage.stat === "vectors") return fmt(STATS.vectors) + " vectors";
  if (stage.stat === "graph") return fmt(Object.values(STATS.graph).reduce((a, b) => a + b, 0)) + " edges";
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
  if (s.demo) { const a = el("a", "", "Try it below ↓"); a.href = "#gate-h"; kids.push(el("p", "small", a)); }
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
  $("#repo").href = PIPE.repository;
  renderPipeline();
  select(0, true);
  try {
    STATS = await getJSON("/api/stats");
    renderPipeline();
    const edges = Object.values(STATS.graph).reduce((a, b) => a + b, 0);
    const docs = Object.values(STATS.documents).reduce((a, b) => a + b, 0);
    for (const [k, v] of Object.entries({ documents: docs, chunks: STATS.chunks, vectors: STATS.vectors, edges })) {
      const t = document.querySelector(`[data-count="${k}"]`);
      if (t) countUp(t, v);
    }
    if (STATS.answer_model !== "local")
      $("#ask-note").textContent = "The answer model is offline, so answers list the matching excerpts and their sources.";
  } catch {
    $("#ask-note").textContent = "The live memory is offline right now; the pipeline and the guide still work.";
    document.querySelectorAll("[data-count]").forEach((t) => { t.textContent = "offline"; });
  }
}

// ---- disclosure gate demo ----------------------------------------------------------
const RECIPIENTS = { owner: "Owner", cloud_model: "Cloud model", public: "Public" };
function withMarks(text) {
  const p = el("p", "mono");
  let last = 0;
  for (const m of text.matchAll(/\[WITHHELD:[^\]]+\]/g)) {
    p.append(text.slice(last, m.index), el("mark", "", m[0]));
    last = m.index + m[0].length;
  }
  p.append(text.slice(last));
  return p;
}
$("#gate-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = $("#gate-out");
  try {
    const res = await getJSON("/api/gate", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: $("#gate-text").value }) });
    out.replaceChildren(...Object.entries(RECIPIENTS).map(([k, label], i) => {
      const card = el("div", "gate-card", el("p", "label", label), withMarks(res[k].text),
        el("p", "small muted", res[k].withheld.length ? "withheld: " + res[k].withheld.join(", ") : "nothing withheld"));
      card.style.setProperty("--i", i);
      return card;
    }));
  } catch {
    out.replaceChildren(el("p", "muted", "The gate demo needs the live memory, which is offline right now."));
  }
});

// ---- ask (AG-UI client) --------------------------------------------------------------
const threadId = crypto.randomUUID();
const history = [];
function reveal(target, text) {
  // Quick progressive reveal (≤ 0.7 s); instant under reduced motion.
  if (REDUCED || text.length < 40) { target.textContent = text; return; }
  const step = Math.max(1, Math.ceil(text.length / 42));
  let n = 0;
  const tick = () => { n = Math.min(text.length, n + step); target.textContent = text.slice(0, n); if (n < text.length) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}
async function ask(question) {
  history.push({ id: crypto.randomUUID(), role: "user", content: question });
  $("#log").append(el("div", "msg user", question));
  const step = el("div", "step busy", "Searching the memory and writing an answer…");
  $("#log").append(step);
  const btn = $("#ask-form button");
  btn.disabled = true;
  let answer = null, text = "", hits = [];
  try {
    await apiReady;
    // AG-UI events as one JSON array (Cloudflare quick tunnels do not carry server-sent events).
    const r = await fetch(API + "/api/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ threadId, runId: crypto.randomUUID(), messages: history, tools: [], context: [], state: {} }),
    });
    step.classList.remove("busy");
    if (r.status === 429) { step.textContent = "Too many questions right now; please wait a minute."; return; }
    if (!r.ok) { step.textContent = "The project memory is offline right now (" + r.status + ")."; return; }
    const { events } = await r.json();
    for (const ev of events) {
      if (ev.type === "TOOL_CALL_RESULT") hits = JSON.parse(ev.content).hits || [];
      else if (ev.type === "TEXT_MESSAGE_START") { answer = el("div", "msg", ""); step.after(answer); }
      else if (ev.type === "TEXT_MESSAGE_CONTENT") text += ev.delta;
      else if (ev.type === "RUN_ERROR") step.textContent = "Error: " + ev.message;
    }
    if (answer) { reveal(answer, text); history.push({ id: crypto.randomUUID(), role: "assistant", content: text }); }
    step.textContent = hits.length ? "Answered from " + hits.length + " excerpts, listed below the answer." : "";
    if (hits.length) {
      const box = el("ol", "hits");
      hits.forEach((h, i) => {
        const li = el("li", "hit", el("div", "where", h.where || h.title || h.project),
          el("div", "meta", h.kind + " · ", el("span", "badge " + h.status, h.status)),
          el("div", "", h.snippet.slice(0, 260) + (h.snippet.length > 260 ? "…" : "")));
        li.style.setProperty("--i", i);
        box.append(li);
      });
      (answer || step).after(box);
    }
  } catch (e) {
    step.classList.remove("busy");
    step.textContent = "Something went wrong: " + e.message;
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

if ($("#pipeline")) loadPipeline().catch(() => { $("#pipeline").replaceChildren(el("li", "muted", "Could not load the pipeline description.")); });
