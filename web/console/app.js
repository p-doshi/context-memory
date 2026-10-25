// Memory console. All memory content is untrusted: it is only ever inserted
// with textContent, never as HTML.
"use strict";

const TOKEN_KEY = "cmh-console-token";

// Token arrives in the URL fragment (never sent to the server); keep it in
// this tab's sessionStorage and remove it from the address bar.
(function takeToken() {
  const m = location.hash.match(/[#&]t=([^&]+)/);
  if (m) {
    sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1]));
    history.replaceState(null, "", location.pathname);
  }
})();

const $ = (sel) => document.querySelector(sel);
function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else e.setAttribute(k, v);
  }
  for (const kid of kids) e.append(kid instanceof Node ? kid : document.createTextNode(String(kid ?? "")));
  return e;
}

async function api(path) {
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (!token) { showLogin(); throw new Error("no token"); }
  const r = await fetch(path, { headers: { Authorization: "Bearer " + token } });
  if (r.status === 401 || r.status === 403) { showLogin("Token refused (" + r.status + ")."); throw new Error("auth"); }
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}

function showLogin(msg) {
  $("#login").hidden = false;
  if (msg) $("#status").textContent = msg;
}
$("#token-save").addEventListener("click", () => {
  sessionStorage.setItem(TOKEN_KEY, $("#token-input").value.trim());
  $("#token-input").value = "";
  $("#login").hidden = true;
  loadOverview();
});

// ---- tabs ----------------------------------------------------------------
const loaded = {};
function show(tab) {
  document.querySelectorAll(".tab").forEach((s) => (s.hidden = s.id !== "tab-" + tab));
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  if (!loaded[tab]) {
    loaded[tab] = true;
    ({ overview: loadOverview, graph: loadProjectGraph, audit: loadAudit, search: () => $("#q").focus() })[tab]();
  }
}
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select")) return;
  const tab = { 1: "overview", 2: "graph", 3: "search", 4: "audit" }[e.key];
  if (tab) show(tab);
  if (e.key === "/") { e.preventDefault(); show("search"); $("#q").focus(); }
});

// ---- overview ------------------------------------------------------------
function table(target, cols, rows) {
  const t = $(target);
  t.replaceChildren(el("tr", {}, ...cols.map((c) => el("th", {}, c[0]))));
  for (const r of rows) t.append(el("tr", {}, ...cols.map((c) => el("td", {}, c[1](r)))));
}
const ago = (iso) => {
  if (!iso) return "never";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  return s < 120 ? Math.round(s) + " s ago" : s < 7200 ? Math.round(s / 60) + " min ago"
       : s < 172800 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago";
};

async function loadOverview() {
  const d = await api("/v1/console/overview");
  const s = d.stats;
  const tiles = [["documents", s.documents], ["current chunks", s.chunks_current], ["vectors", s.vectors],
                 ["graph entities", s.entities], ["graph edges", s.edges_current], ["projects", s.projects.length],
                 ["audit rows chained", d.audit_chain.rows]];
  $("#tiles").replaceChildren(...tiles.map(([k, v]) =>
    el("div", { class: "tile" }, el("div", { class: "v" }, (v ?? 0).toLocaleString()), el("div", { class: "k" }, k))));
  table("#projects", [["project", (r) => r.project], ["docs", (r) => r.documents], ["chats", (r) => r.chats],
                      ["code", (r) => r.code], ["updated", (r) => ago(r.updated)]], d.projects);
  const dm = d.daemon || {};
  const tunnels = Object.entries(dm.tunnels || {}).map(([h, t]) => h + ": " + t.state).join(", ") || "none";
  const gpu = dm.gpu ? dm.gpu.temp_c + " °C, " + dm.gpu.util_pct + " % util, " + dm.gpu.mem_mb + " MB" : "n/a";
  const b = d.backup;
  table("#daemon", [["", (r) => r[0]], ["", (r) => r[1]]], [
    ["last pass", dm.seconds_since_pass == null ? "none yet" : dm.seconds_since_pass + " s ago (" + dm.status + ")"],
    ["tunnels", tunnels], ["GPU", gpu],
    ["backup", b ? b.created + " (" + (b.ok ? "verified" : "FAILED") + ", " + Math.round(b.bytes / 1e6) + " MB)" : "none"],
    ["audit chain", d.audit_chain.ok ? "intact, " + d.audit_chain.rows + " rows" : "BROKEN at " + d.audit_chain.broken_at],
  ]);
  table("#devices", [["device", (r) => r.name], ["scopes", (r) => r.scopes], ["last seen", (r) => r.revoked_at ? "revoked" : ago(r.last_seen)]],
        d.devices);
  $("#status").textContent = "updated " + new Date().toLocaleTimeString();
}

// ---- graph ---------------------------------------------------------------
// Okabe-Ito palette (readable with colour-vision deficiency); shape encodes kind too.
const KIND = {
  project: ["#E69F00", "round-rectangle"], platform: ["#56B4E9", "diamond"], account: ["#009E73", "hexagon"],
  document: ["#F0E442", "ellipse"], module: ["#0072B2", "rectangle"], class: ["#CC79A7", "triangle"],
  function: ["#D55E00", "ellipse"], method: ["#D55E00", "vee"], file_ref: ["#999999", "tag"],
};
let cy = null;
function drawGraph(data, center) {
  const nodes = data.nodes.map((n) => ({ data: { ...n, weight: n.weight || 1 } }));
  const edges = data.edges.map((e, i) => ({ data: { id: "e" + i, ...e } }));
  if (cy) cy.destroy();
  cy = cytoscape({
    container: $("#cy"), elements: [...nodes, ...edges], wheelSensitivity: 0.3,
    style: [
      { selector: "node", style: {
          "background-color": (n) => (KIND[n.data("kind")] || ["#bbbbbb"])[0],
          shape: (n) => (KIND[n.data("kind")] || [null, "ellipse"])[1],
          label: "data(label)", color: "#d8dde4", "font-size": 9, "text-valign": "bottom", "text-margin-y": 3,
          width: (n) => 10 + 4 * Math.sqrt(n.data("weight")), height: (n) => 10 + 4 * Math.sqrt(n.data("weight")) } },
      { selector: "edge", style: { width: (e) => Math.min(1 + Math.log2(1 + (e.data("weight") || 1)), 6),
          "line-color": "#3b4452", "curve-style": "bezier", "target-arrow-shape": "triangle",
          "target-arrow-color": "#3b4452", "arrow-scale": 0.6, label: "data(relation)", "font-size": 7,
          color: "#8b95a3" } },
      { selector: "node[id = '" + (center || "") + "']", style: { "border-width": 3, "border-color": "#ffffff" } },
    ],
    layout: { name: "cose", animate: false, nodeRepulsion: 9000, idealEdgeLength: 70 },
  });
  cy.on("tap", "node", (evt) => {
    const n = evt.target.data();
    $("#g-entity").value = n.kind === "project" ? n.label : (n.full || n.label);
    loadEntityGraph();
  });
  $("#g-info").textContent = data.nodes.length + " nodes, " + data.edges.length + " edges";
  $("#legend").replaceChildren(...[...new Set(data.nodes.map((n) => n.kind))].map((k) =>
    el("span", { style: "--c:" + (KIND[k] || ["#bbbbbb"])[0] }, k)));
}
async function loadProjectGraph() { drawGraph(await api("/v1/console/graph/projects")); }
async function loadEntityGraph() {
  const name = $("#g-entity").value.trim();
  if (!name) return;
  const d = await api("/v1/console/graph/entity?name=" + encodeURIComponent(name) + "&depth=" + $("#g-depth").value);
  if (!d.nodes.length) { $("#g-info").textContent = "no entity matching that name"; return; }
  drawGraph(d, d.center);
}
$("#g-projects").addEventListener("click", loadProjectGraph);
$("#g-go").addEventListener("click", loadEntityGraph);
$("#g-entity").addEventListener("keydown", (e) => e.key === "Enter" && loadEntityGraph());

// ---- search --------------------------------------------------------------
async function search() {
  const q = $("#q").value.trim();
  if (!q) return;
  const mode = $("#mode").value;
  const d = await api("/v1/console/search?q=" + encodeURIComponent(q) + (mode ? "&mode=" + mode : ""));
  $("#q-info").textContent = d.mode + " · " + d.results.length + " results · " + Math.round(d.timings_ms.total) + " ms";
  $("#results").replaceChildren(...d.results.map((r) => {
    const p = r.provenance;
    const body = r.text.replace(/^BEGIN_UNTRUSTED_MEMORY\n/, "").replace(/\nEND_UNTRUSTED_MEMORY$/, "");
    return el("div", { class: "result" },
      el("div", { class: "meta" }, el("span", { class: "badge " + p.verification_status }, p.verification_status), " ",
         r.score.toFixed(3) + " · " + r.project + " · " + (r.title || "") + (r.section_path ? " › " + r.section_path : "")),
      el("div", { class: "meta" }, p.source_path),
      el("pre", {}, body));
  }));
}
$("#q-go").addEventListener("click", search);
$("#q").addEventListener("keydown", (e) => e.key === "Enter" && search());

// ---- audit ---------------------------------------------------------------
async function loadAudit() {
  const [rows, ov] = await Promise.all([api("/v1/console/audit?n=200"), api("/v1/console/overview")]);
  const c = ov.audit_chain;
  $("#chain").textContent = c.ok ? "Audit chain intact: " + c.rows + " chained rows, head " + c.head.slice(0, 16) + "…"
                                 : "AUDIT CHAIN BROKEN at row " + c.broken_at + " (" + c.reason + ")";
  $("#chain").className = c.ok ? "ok" : "bad";
  table("#audit", [["time", (r) => r.ts.replace("T", " ").slice(0, 19)], ["actor", (r) => r.actor],
                   ["action", (r) => r.action], ["target", (r) => (r.target || "").slice(0, 70)],
                   ["outcome", (r) => r.outcome]], rows);
}

if (sessionStorage.getItem(TOKEN_KEY)) loadOverview().catch(() => {}); else showLogin();
