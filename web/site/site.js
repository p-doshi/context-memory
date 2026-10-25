// Public site. Content is untrusted and only ever inserted as text.
"use strict";

const $ = (s) => document.querySelector(s);
function el(tag, cls, ...kids) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(String(k ?? "")));
  return e;
}
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
const fmt = (n) => Number(n).toLocaleString("en");

// ---- pipeline ----------------------------------------------------------------
let PIPE = null, STATS = null;

function statFor(stage) {
  if (!STATS || !stage.stat) return null;
  if (stage.stat === "documents") return fmt(Object.values(STATS.documents).reduce((a, b) => a + b, 0)) + " documents";
  if (stage.stat === "chunks") return fmt(STATS.chunks) + " chunks";
  if (stage.stat === "vectors") return fmt(STATS.vectors) + " vectors";
  if (stage.stat === "graph") return fmt(Object.values(STATS.graph).reduce((a, b) => a + b, 0)) + " edges";
  return null;
}

function renderPipeline() {
  const ol = $("#pipeline");
  ol.replaceChildren(...PIPE.stages.map((s, i) => {
    const b = el("button", "stage", el("span", "num", String(i + 1)), el("span", "title", s.title));
    const st = statFor(s);
    if (st) b.append(el("span", "stat", st));
    b.setAttribute("aria-controls", "detail");
    b.addEventListener("click", () => select(i));
    return el("li", "", b);
  }));
}

function select(i) {
  const s = PIPE.stages[i];
  document.querySelectorAll(".stage").forEach((b, j) => b.setAttribute("aria-pressed", String(i === j)));
  const where = el("ul", "where", ...s.where.map((w) => {
    const a = el("a", "", w);
    a.href = PIPE.repository + "/tree/main/" + w;
    a.rel = "noopener";
    return el("li", "", a);
  }));
  const d = $("#detail");
  d.replaceChildren(el("h3", "", (i + 1) + ". " + s.title), el("p", "", s.what), el("p", "label", "In the code"), where);
  if (s.citation) {
    const a = el("a", "", "doi:" + s.citation.doi);
    a.href = "https://doi.org/" + s.citation.doi;
    a.rel = "noopener";
    d.append(el("p", "cite small", s.citation.text + ". ", a));
  }
  if (s.demo) {
    const a = el("a", "", "Try it below ↓");
    a.href = "#gate-h";
    d.append(el("p", "small", a));
  }
}

async function loadPipeline() {
  PIPE = await getJSON("/pipeline.json");
  $("#summary").textContent = PIPE.summary;
  $("#repo").href = PIPE.repository;
  renderPipeline();
  select(0);
  try {
    STATS = await getJSON("/api/stats");
    renderPipeline();
    select(0);
    if (STATS.answer_model !== "local")
      $("#ask-note").textContent = "The answer model is offline, so answers list the matching excerpts and their sources.";
  } catch {
    $("#ask-note").textContent = "The live memory is offline right now; the pipeline and the guide still work.";
  }
}

// ---- disclosure gate demo ------------------------------------------------------
const RECIPIENTS = { owner: "Owner", cloud_model: "Cloud model", public: "Public" };
$("#gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = $("#gate-out");
  try {
    const res = await getJSON("/api/gate", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: $("#gate-text").value }) });
    out.replaceChildren(...Object.entries(RECIPIENTS).map(([k, label]) => el("div", "gate-card",
      el("p", "label", label), el("p", "mono", res[k].text),
      el("p", "small muted", res[k].withheld.length ? "withheld: " + res[k].withheld.join(", ") : "nothing withheld"))));
  } catch {
    out.replaceChildren(el("p", "muted", "The gate demo needs the live memory, which is offline right now."));
  }
});

// ---- ask (AG-UI client) ---------------------------------------------------------
const threadId = crypto.randomUUID();
const history = [];

async function ask(question) {
  history.push({ id: crypto.randomUUID(), role: "user", content: question });
  $("#log").append(el("div", "msg user", question));
  const step = el("div", "step", "Starting…");
  $("#log").append(step);
  const btn = $("#ask-form button");
  btn.disabled = true;
  let answer = null, hits = [];
  try {
    await apiReady;
    const r = await fetch(API + "/api/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ threadId, runId: crypto.randomUUID(), messages: history, tools: [], context: [], state: {} }),
    });
    if (r.status === 429) { step.textContent = "Too many questions right now; please wait a minute."; return; }
    if (!r.ok) { step.textContent = "The project memory is offline right now (" + r.status + ")."; return; }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, i).split("\n").find((l) => l.startsWith("data: "));
        buf = buf.slice(i + 2);
        if (!line) continue;
        const ev = JSON.parse(line.slice(6));
        if (ev.type === "STEP_STARTED") step.textContent = ev.stepName === "answer" ? "Writing the answer…" : "Searching the memory…";
        else if (ev.type === "TOOL_CALL_RESULT") hits = JSON.parse(ev.content).hits || [];
        else if (ev.type === "TEXT_MESSAGE_START") { answer = el("div", "msg", ""); step.after(answer); }
        else if (ev.type === "TEXT_MESSAGE_CONTENT") answer.textContent += ev.delta;
        else if (ev.type === "TEXT_MESSAGE_END") history.push({ id: ev.messageId, role: "assistant", content: answer.textContent });
        else if (ev.type === "RUN_ERROR") step.textContent = "Error: " + ev.message;
        else if (ev.type === "RUN_FINISHED") step.textContent = hits.length ? "Sources" : "No matching excerpts.";
      }
    }
    if (hits.length) {
      const box = el("ol", "hits");
      for (const h of hits) box.append(el("li", "hit",
        el("div", "where", h.where || h.title || h.project),
        el("div", "meta", h.kind + " · ", el("span", "badge " + h.status, h.status)),
        el("div", "", h.snippet.slice(0, 260) + (h.snippet.length > 260 ? "…" : ""))));
      (answer || step).after(box);
    }
  } catch (e) {
    step.textContent = "Something went wrong: " + e.message;
  } finally {
    btn.disabled = false;
  }
}
$("#ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#question").value.trim();
  if (q) { $("#question").value = ""; ask(q); }
});

loadPipeline().catch(() => { $("#pipeline").replaceChildren(el("li", "muted", "Could not load the pipeline description.")); });
