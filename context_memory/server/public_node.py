"""The public node: what the internet sees, served from the exported public store.

A separate process from the hub. It never opens the private store; it reads
<home>/public/current (written by `cmh public export`) and reloads when that
changes. Query vectors come from the hub's /v1/embed (scope "embed"; the
vector of the visitor's own question, no memory content); if the hub is
unreachable, search falls back to BM25. Answers come from a local model via
llama-server on loopback, falling back to quoted excerpts.

One site, two audiences: browsers get the visual pages; agents (by User-Agent,
by an Accept header without text/html, or ?view=agent) get the Markdown twin
of each page, /llms.txt, /pipeline.json and the AG-UI endpoint.

Binds 127.0.0.1 only; the internet reaches it through cloudflared.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path

from context_memory import llm, public as public_mod
from context_memory.config import Settings
from context_memory.public_export import current_store
from context_memory.sources import Sources

log = logging.getLogger("cmh.public")
SITE = Path(__file__).resolve().parents[2] / "web" / "site"
PAGES = {"/": "index", "/guide": "guide"}
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript",
                ".json": "application/json", ".md": "text/markdown; charset=utf-8",
                ".txt": "text/plain; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".woff2": "font/woff2"}
FONTS = SITE.parent / "fonts"  # self-hosted IBM Plex (SIL OFL), shared with the console
AGENT_UA = re.compile(r"bot|crawl|spider|slurp|curl|wget|httpie|python|httpx|aiohttp|node-fetch|undici|"
                      r"axios|go-http|java/|okhttp|claude|anthropic|openai|gpt|perplexity|gemini|"
                      r"cohere|mistral|llama|agent|mcp", re.I)
# The only texts the public gate demo runs on (synthetic).
GATE_EXAMPLES = {
    "contact": "Email the draft to jane.doe@example.org and call her at +1 902 555 0147 before Friday.",
    "address": "The workshop moved to 123 Example Street, Halifax B3H 4R2, room 214.",
    "card": "Lab supplies were paid with card 4242 4242 4242 4242; reimbursement pending.",
    "location": "The test mooring sits at 44.6366 N, 63.5917 W, near the dock.",
}
HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def is_agent(request) -> bool:
    view = request.query_params.get("view")
    if view in ("agent", "human"):
        return view == "agent"
    accept = request.headers.get("accept", "")
    if accept and "text/html" not in accept and "*/*" not in accept:
        return True
    return bool(AGENT_UA.search(request.headers.get("user-agent", "") or "agent"))


class HubEmbedder:
    """Embedder that asks the hub for query vectors. dim and model_name come
    from the exported store so the Lance table opens without the hub."""

    def __init__(self, hub_url: str, token: str, dim: int, model_name: str):
        self.hub_url, self.token, self.dim, self.model_name = hub_url.rstrip("/"), token, dim, model_name
        self._ok_until = 0.0

    def available(self) -> bool:
        if time.time() < self._ok_until:
            return True
        try:
            self.embed_query("ping")
            return True
        except Exception:
            return False

    def embed_query(self, text: str) -> list[float]:
        req = urllib.request.Request(self.hub_url + "/v1/embed", data=json.dumps({"text": text}).encode(),
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            out = json.load(r)
        if out["model"] != self.model_name or out["dim"] != self.dim:
            raise ValueError("hub embedding model differs from the exported store")
        self._ok_until = time.time() + 30
        return out["vector"]

    def embed_documents(self, texts):
        raise RuntimeError("the public node does not ingest")

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


class RateLimiter:
    def __init__(self):
        self.hits: dict[tuple[str, str], list[float]] = {}
        self.lock = threading.Lock()

    def over(self, ip: str, bucket: str, limit: int, window: float) -> bool:
        now = time.time()
        with self.lock:
            times = [t for t in self.hits.get((ip, bucket), []) if now - t < window]
            times.append(now)
            self.hits[(ip, bucket)] = times
            if len(self.hits) > 50_000:  # forget idle visitors
                self.hits = {k: v for k, v in self.hits.items() if now - v[-1] < 3600}
            return len(times) > limit


class PublicStore:
    """The current exported store, reopened when a new export lands."""

    def __init__(self, settings: Settings, root: Path, hub_url: str, token: str):
        self.settings, self.root, self.hub_url, self.token = settings, root, hub_url, token
        self.lock = threading.RLock()
        self.path: Path | None = None
        self.svc = None
        self.embedder: HubEmbedder | None = None

    def get(self):
        store = current_store(self.root)
        with self.lock:
            if store != self.path:
                self._open(store)
            return self.svc

    def _open(self, store: Path | None) -> None:
        from context_memory.memory.lance_store import LanceStore
        from context_memory.memory.service import MemoryService
        from context_memory.memory.sqlite_store import SQLiteStore
        if self.svc is not None:
            self.svc.close()
        self.svc, self.path = None, store
        if store is None:
            return
        import lancedb
        db = lancedb.connect(str(store / "data" / "lance"))
        tbl = db.open_table("chunks")
        dim = tbl.schema.field("vector").type.list_size
        first = tbl.search().limit(1).select(["embedding_model"]).to_list()
        model = first[0]["embedding_model"] if first else "none"
        self.embedder = HubEmbedder(self.hub_url, self.token, dim, model)
        settings = self.settings.model_copy(update={"data_dir": store / "data"})
        self.svc = MemoryService(settings, self.embedder, SQLiteStore(store / "data" / "metadata.sqlite3"),
                                 LanceStore(store / "data" / "lance", dim, model))
        log.info("public store opened: %s", store.name)

    def sources(self) -> Sources:
        svc = self.get()
        q = svc.sqlite.conn.execute
        return Sources(public_projects=[r[0] for r in q("SELECT DISTINCT project FROM documents")],
                       public_kinds=[r[0] for r in q("SELECT DISTINCT kind FROM documents")])

    def mode(self) -> str:
        return "hybrid" if self.embedder and self.embedder.available() else "bm25"


def build_app(settings: Settings, root: Path, hub_url: str, token: str,
              llm_url: str | None = "http://127.0.0.1:8090", llm_key: str | None = None):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
    from starlette.routing import Route

    from context_memory.public_guard import Guard
    store = PublicStore(settings, root, hub_url, token)
    guard = Guard(settings.data_dir.parent, salt=token[-16:])
    limiter = RateLimiter()
    llm_slot = threading.Semaphore(1)  # one generation at a time on the laptop GPU
    waiting = [0]
    static = {p.relative_to(SITE).as_posix(): p for p in SITE.rglob("*")
              if p.is_file() and p.suffix in STATIC_TYPES and p.name != "vercel.json"}
    static.update({f"fonts/{p.name}": p for p in FONTS.glob("*.woff2")})

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else ""
        if peer in ("127.0.0.1", "::1"):  # cloudflared runs here; trust its header only then
            return request.headers.get("cf-connecting-ip", peer)[:64]
        return peer

    def limited(request: Request, bucket: str, limit: int, window: float):
        if limiter.over(client_ip(request), bucket, limit, window):
            return JSONResponse({"error": "rate limit, try again shortly"}, status_code=429,
                                headers={"Retry-After": str(int(window)), **HEADERS})
        return None

    def no_store():
        return JSONResponse({"error": "the public memory has not been exported yet"}, status_code=503,
                            headers=HEADERS)

    def _json(data, status=200):
        return JSONResponse(data, status_code=status, headers={**HEADERS, "Cache-Control": "no-store"})

    async def page(request: Request):
        if (r := limited(request, "page", 240, 60)):
            return r
        name = PAGES[request.url.path.rstrip("/") or "/"]
        agent = is_agent(request)
        # Human pages live under human/ so that static hosts (Vercel) find no file at
        # / or /guide and apply the agent/human rewrites in vercel.json.
        md = "overview.md" if name == "index" else f"{name}.md"  # no index.* at the root (static hosts)
        f = SITE / (md if agent else f"human/{name}.html")
        link = f'</{md}>; rel="alternate"; type="text/markdown", </llms.txt>; rel="describedby"'
        return FileResponse(f, media_type=STATIC_TYPES[f.suffix],
                            headers={**HEADERS, "Link": link, "Vary": "Accept, User-Agent"})

    async def static_file(request: Request):
        if (r := limited(request, "page", 240, 60)):
            return r
        rel = request.path_params["path"]
        if rel not in static:
            return Response("not found", status_code=404, headers=HEADERS)
        p = static[rel]
        return FileResponse(p, media_type=STATIC_TYPES[p.suffix], headers=HEADERS)

    async def api_projects(request: Request):
        if (r := limited(request, "api", 120, 60)):
            return r
        if store.get() is None:
            return no_store()
        with store.lock:
            return _json(public_mod.projects(store.get(), store.sources()))

    async def api_graph(request: Request):
        if (r := limited(request, "api", 120, 60)):
            return r
        if store.get() is None:
            return no_store()
        with store.lock:
            return _json(public_mod.architecture_graph(store.get(), store.sources()))

    async def api_search(request: Request):
        if (r := limited(request, "api", 60, 60)):
            return r
        if store.get() is None:
            return no_store()
        q = request.query_params.get("q", "")[:300]
        mode = store.mode()
        with store.lock:
            return _json({**public_mod.search(store.get(), store.sources(), q, mode=mode), "mode": mode})

    async def api_stats(request: Request):
        if (r := limited(request, "api", 120, 60)):
            return r
        svc = store.get()
        if svc is None:
            return no_store()
        with store.lock:
            q = svc.sqlite.conn.execute
            kinds = dict(q("SELECT kind, COUNT(*) FROM documents GROUP BY kind").fetchall())
            out = {"documents": kinds, "chunks": q("SELECT COUNT(*) FROM chunks").fetchone()[0],
                   "vectors": svc.lance.count(),
                   "graph": dict(q("SELECT relation, COUNT(*) FROM edges WHERE valid_to IS NULL "
                                   "GROUP BY relation").fetchall()),
                   "exported": store.path.name.removeprefix("store-") if store.path else None,
                   "search_mode": store.mode(),
                   "answer_model": "local" if llm_url and guard.llm_enabled() and llm.health(llm_url)
                   else "excerpts"}
        return _json(out)

    async def read_json(request: Request, limit: int):
        """(body, error response). Oversized bodies get 413, not a misleading 400."""
        raw = await request.body()
        if len(raw) > limit:
            return None, _json({"error": f"request too large: at most {limit // 1000} KB"}, 413)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return None, _json({"error": "body must be JSON"}, 400)
        return (body, None) if isinstance(body, dict) else (None, _json({"error": "body must be a JSON object"}, 400))

    async def api_gate(request: Request):
        """The Disclosure Gate on fixed example notes only. A free-text gate would be an
        oracle: a visitor could iterate formats until one slips through the patterns."""
        if (r := limited(request, "api", 30, 60)):
            return r
        from context_memory.policy.disclosure import gate
        body, err = await read_json(request, 1000)
        if err:
            return err
        text = GATE_EXAMPLES.get(str(body.get("example", "")))
        if text is None:
            return _json({"error": f"example must be one of {sorted(GATE_EXAMPLES)}"}, 400)
        res = {rec: gate(text, rec, chunk_tier="public") for rec in ("owner", "cloud_model", "public")}
        return _json({rec: {"text": g.text, "withheld": [f"{v['tier']}:{v['kind']}" for v in g.violations]}
                      for rec, g in res.items()})

    async def api_agent(request: Request):
        """AG-UI run over server-sent events, streamed as the model writes."""
        if (r := limited(request, "agent", 8, 60)) or (r := limited(request, "agent-hour", 60, 3600)):
            return r
        svc = store.get()
        if svc is None:
            return no_store()
        body, err = await read_json(request, 20000)
        if err:
            return err
        ip = client_ip(request)
        verdict = guard.check_question(public_mod.last_user_message(body), ip)
        if not verdict.ok:
            log.info("agent run refused: %s", ",".join(verdict.reasons))
            events = public_mod.canned_run(body, verdict.reply)
            if "application/json" in request.headers.get("accept", ""):
                return _json({"events": list(events)})
            return StreamingResponse((f"data: {json.dumps(ev)}\n\n" for ev in events),
                                     media_type="text/event-stream",
                                     headers={**HEADERS, "Cache-Control": "no-store"})
        if waiting[0] >= 4:
            return _json({"error": "busy, try again in a minute"}, 503)

        def answerer(question, hits, history):
            if not llm_url or not hits or not guard.llm_enabled():
                raise RuntimeError("no model, model switched off, or nothing to answer from")
            waiting[0] += 1
            try:
                if not llm_slot.acquire(timeout=60):
                    raise RuntimeError("model busy")
            finally:
                waiting[0] -= 1
            try:
                # Buffered, then checked: nothing reaches the visitor before the output check.
                text = "".join(llm.stream_chat(llm.build_messages(question, hits, history), llm_url,
                                               api_key=llm_key))
            finally:
                llm_slot.release()
            text = llm.ground(text, hits)  # drop unsupported sentences, cite the rest
            out = guard.check_answer(text, question, ip, excerpts=[h.get("snippet", "") for h in hits])
            text = text if out.ok else out.reply
            for i in range(0, len(text), 80):
                yield text[i:i + 80]

        sources, mode = store.sources(), store.mode()
        accept = request.headers.get("accept", "")
        if "application/json" in accept and "text/event-stream" not in accept:
            # Same AG-UI events as one JSON array: Cloudflare quick tunnels do not
            # carry server-sent events, and answers are buffered for the output check anyway.
            events = list(public_mod.agent_run(svc, sources, body, answerer=answerer, mode=mode,
                                               lock=store.lock))
            log.info("agent run (json)")
            return _json({"events": events})

        def stream():
            for ev in public_mod.agent_run(svc, sources, body, answerer=answerer, mode=mode,
                                           lock=store.lock):
                yield f"data: {json.dumps(ev)}\n\n"
            log.info("agent run")  # no question text: only flagged attempts are kept, for review

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={**HEADERS, "Cache-Control": "no-store"})

    allowed_origins = set(settings.public_allowed_origins)
    origin_re = re.compile(settings.public_allowed_origin_regex) if settings.public_allowed_origin_regex else None

    class Cors:
        """Browser access to /api/* from the configured site origins only."""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or not scope["path"].startswith("/api/"):
                return await self.app(scope, receive, send)
            origin = dict(scope["headers"]).get(b"origin", b"").decode()
            allow = origin in allowed_origins or bool(origin_re and origin_re.fullmatch(origin))
            if scope["method"] == "OPTIONS":
                headers = {"Vary": "Origin"}
                if allow:
                    headers.update({"Access-Control-Allow-Origin": origin,
                                    "Access-Control-Allow-Methods": "GET, POST",
                                    "Access-Control-Allow-Headers": "Content-Type, Accept",
                                    "Access-Control-Max-Age": "600"})
                return await Response(status_code=204 if allow else 403, headers=headers)(scope, receive, send)

            async def send_cors(message):
                if allow and message["type"] == "http.response.start":
                    message.setdefault("headers", [])
                    message["headers"] += [(b"access-control-allow-origin", origin.encode()),
                                           (b"vary", b"Origin")]
                await send(message)

            await self.app(scope, receive, send_cors)

    routes = [Route(p, page) for p in PAGES] + [
        Route("/api/projects", api_projects), Route("/api/graph", api_graph),
        Route("/api/search", api_search), Route("/api/stats", api_stats),
        Route("/api/gate", api_gate, methods=["POST"]), Route("/api/agent", api_agent, methods=["POST"]),
        Route("/{path:path}", static_file),
    ]
    return Cors(Starlette(routes=routes))


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn
    from context_memory.secrets import broker
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the public node binds loopback; cloudflared carries outside traffic")
    token = broker.get_secret("public-node-embed") or ""
    root = settings.data_dir.parent / "public"
    hub_url = f"http://127.0.0.1:{settings.hub_port}"
    app = build_app(settings, root, hub_url, token, llm_url=settings.public_llm_url or None,
                    llm_key=broker.get_secret("llm-server-key"))

    def warm():
        # One search after start-up opens the store and warms the embedder path, so the
        # first visitor's figure shows normal timings (cold: ~2.5 s; warm: ~0.25 s, measured).
        import urllib.request
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/search?q=disclosure+gate", timeout=60).read()
        except OSError as e:
            log.info("warm-up search failed: %r", e)
    threading.Timer(8, warm).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
