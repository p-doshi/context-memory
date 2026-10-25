// Visitor page: an AG-UI client for the public memory agent. All memory content
// is untrusted and only ever inserted as text.
"use strict";

const TOKEN_KEY = "cmh-visitor-token";
(function takeToken() {
  const m = location.hash.match(/[#&]t=([^&]+)/);
  if (m) { sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1])); history.replaceState(null, "", location.pathname); }
})();
const token = () => sessionStorage.getItem(TOKEN_KEY) || "";
const $ = (s) => document.querySelector(s);
function el(tag, cls, ...kids) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(String(k ?? "")));
  return e;
}
async function getJSON(path) {
  const r = await fetch(path, { headers: { Authorization: "Bearer " + token() } });
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}

// ---- AG-UI run -------------------------------------------------------------
const threadId = crypto.randomUUID();
const history_ = [];

async function ask(question) {
  history_.push({ id: crypto.randomUUID(), role: "user", content: question });
  $("#log").append(el("div", "msg user", question));
  const step = el("div", "step", "Starting…");
  $("#log").append(step);
  const btn = $("#ask button"); btn.disabled = true;
  let answer = null, hitsBox = null;
  try {
    const r = await fetch("/v1/public/agent", {
      method: "POST",
      headers: { Authorization: "Bearer " + token(), "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ threadId, runId: crypto.randomUUID(), messages: history_, tools: [], context: [], state: {} }),
    });
    if (r.status === 429) { step.textContent = "Too many questions right now; please wait a minute."; return; }
    if (!r.ok) { step.textContent = "The project memory is not reachable (" + r.status + ")."; return; }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, i); buf = buf.slice(i + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const ev = JSON.parse(line.slice(6));
        switch (ev.type) {
          case "STEP_STARTED": step.textContent = "Searching the project's memory…"; break;
          case "TOOL_CALL_RESULT": {
            const res = JSON.parse(ev.content);
            hitsBox = el("div", "hits");
            for (const h of res.hits.slice(0, 4)) {
              hitsBox.append(el("div", "hit",
                el("div", "where", h.where || h.title || h.project),
                el("div", "meta", h.project + " · " + h.kind + " · ", el("span", "badge " + h.status, h.status)),
                el("div", "", h.snippet.slice(0, 220) + (h.snippet.length > 220 ? "…" : ""))));
            }
            step.textContent = res.hits.length ? "Found " + res.hits.length + " relevant excerpts." : "No matching excerpts.";
            break;
          }
          case "TEXT_MESSAGE_START": answer = el("div", "msg", ""); $("#log").append(answer); break;
          case "TEXT_MESSAGE_CONTENT": answer.textContent += ev.delta; break;
          case "TEXT_MESSAGE_END": history_.push({ id: ev.messageId, role: "assistant", content: answer.textContent }); break;
          case "RUN_ERROR": step.textContent = "Error: " + ev.message; break;
        }
      }
    }
    // Excerpt cards sit directly under the answer they support.
    if (hitsBox) (answer || step).after(hitsBox);
  } catch (e) {
    step.textContent = "Something went wrong: " + e.message;
  } finally {
    btn.disabled = false;
    $("#question").focus();
  }
}
$("#ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#question").value.trim();
  if (q) { $("#question").value = ""; ask(q); }
});

// ---- side panels -----------------------------------------------------------
async function loadProjects() {
  const ps = await getJSON("/v1/public/projects");
  if (!ps.length) { $("#projects").textContent = "Nothing has been made public yet."; return; }
  $("#projects").replaceChildren(...ps.map((p) => {
    const counts = Object.entries(p.documents).map(([k, v]) => v + " " + k).join(", ");
    const ul = el("ul", "", ...p.recent.slice(0, 5).map((t) => el("li", "", t)));
    return el("div", "project", el("strong", "", p.project), el("div", "muted small", counts), ul);
  }));
}
async function loadGraph() {
  const g = await getJSON("/v1/public/graph");
  if (!g.nodes.length) { $("#arch").replaceChildren(el("p", "muted small", "No architecture graph for the public projects.")); return; }
  const cy = cytoscape({
    container: $("#arch"),
    elements: [...g.nodes.map((n) => ({ data: n })), ...g.edges.map((e, i) => ({ data: { id: "e" + i, ...e } }))],
    style: [
      { selector: "node", style: { "background-color": "#0072B2", label: "data(label)", "font-size": 10,
          color: "#1d2125", "text-valign": "bottom", "text-margin-y": 2, width: 12, height: 12 } },
      { selector: "edge", style: { width: 1, "line-color": "#c9cfd6", "target-arrow-shape": "triangle",
          "target-arrow-color": "#c9cfd6", "curve-style": "bezier", "arrow-scale": 0.6 } },
    ],
    layout: { name: "cose", animate: false, nodeRepulsion: 200000, idealEdgeLength: 90 },
  });
  cy.on("tap", "node", (e) => ask("What does the " + e.target.data("label") + " module do?"));
}
loadProjects().catch(() => ($("#projects").textContent = "Project list unavailable."));
loadGraph().catch(() => {});
$("#question").focus();
