"""The hub: this machine's memory served to Claude Code sessions elsewhere.

Routes (all require `Authorization: Bearer <device token>`):

  POST /mcp            MCP over streamable HTTP (search, write, graph, stats)
  POST /v1/sessions    upload a Claude Code transcript (JSONL, optionally gzip)
  GET  /v1/health      liveness and the calling device's name

Each remote machine gets its own token (`cmh device add <name>`), stored only
as a SHA-256, revocable at any time. The hub binds 127.0.0.1; other machines
reach it only through SSH tunnels (server/tunnels.py, docs/remote-setup.md),
never through a public interface (design §24).

The hub uses its own SQLite and LanceDB connections and shares only the
embedding model with the daemon (the model serialises GPU use internally).
"""

from __future__ import annotations

import gzip
import json
import os
import hashlib
import re
import secrets
import threading
from contextvars import ContextVar
from pathlib import Path

from context_memory.config import Settings
from context_memory.memory.embedder import Embedder
from context_memory.memory.service import MemoryService
from context_memory.memory.sqlite_store import SQLiteStore
from context_memory.util import NO_WINDOW, new_id, slugify, utcnow

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
current_device: ContextVar[dict | None] = ContextVar("current_device", default=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# -- device registry -------------------------------------------------------

def add_device(db: SQLiteStore, name: str, account: str | None, rotate: bool = False) -> str:
    """Register a device and return its token. The token is not stored."""
    name = slugify(name, "")
    if not name:
        raise ValueError("device name must contain letters or digits")
    token = "cmh_" + secrets.token_urlsafe(32)
    if rotate and db.conn.execute("SELECT 1 FROM devices WHERE name = ?", (name,)).fetchone():
        # New token for an existing device; the old token stops working at once.
        db.conn.execute("UPDATE devices SET token_sha256 = ?, revoked_at = NULL, "
                        "account = coalesce(?, account) WHERE name = ?", (_hash(token), account, name))
        db.audit("cli", "device_rotate", name, "ok", {"account": account})
        return token
    db.conn.execute(
        "INSERT INTO devices (device_id, name, account, token_sha256, created_at) VALUES (?, ?, ?, ?, ?)",
        (new_id("dev"), name, account, _hash(token), utcnow()))
    db.audit("cli", "device_add", name, "ok", {"account": account})
    return token


def make_bundle(db: SQLiteStore, name: str, account: str | None, devices_dir: Path) -> Path:
    """Register a device and write a setup bundle: the token (never printed) plus
    the client files. On Windows the folder is restricted to the current user."""
    import shutil
    import subprocess

    client = Path(__file__).resolve().parents[2] / "client"
    bundle = devices_dir / f"{slugify(name, 'device')}-bundle"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    if os.name == "nt":
        subprocess.run(["icacls", str(bundle), "/inheritance:r", "/grant:r",
                        f"{os.environ.get('USERNAME', '')}:(OI)(CI)F"],
                       capture_output=True, creationflags=NO_WINDOW)
    for f in ("cmh_push.py", "setup_remote.sh", "claude_memory.md"):
        shutil.copy2(client / f, bundle / f)
    token = add_device(db, name, account, rotate=True)
    (bundle / "cmh.token").write_text(token)
    if os.name != "nt":
        (bundle / "cmh.token").chmod(0o600)
    return bundle


def revoke_device(db: SQLiteStore, name: str) -> bool:
    n = db.conn.execute("UPDATE devices SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                        (utcnow(), name)).rowcount
    db.audit("cli", "device_revoke", name, "ok" if n else "not_found", {})
    return bool(n)


def list_devices(db: SQLiteStore) -> list[dict]:
    rows = db.conn.execute("SELECT name, account, created_at, revoked_at, last_seen FROM devices "
                           "ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def lookup_device(db: SQLiteStore, token: str) -> dict | None:
    row = db.conn.execute("SELECT * FROM devices WHERE token_sha256 = ? AND revoked_at IS NULL",
                          (_hash(token),)).fetchone()
    if row is None:
        return None
    db.conn.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (utcnow(), row["device_id"]))
    return dict(row)


# -- app ---------------------------------------------------------------------

def build_app(settings: Settings, embedder: Embedder, service: MemoryService | None = None):
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from context_memory.server.mcp_server import build_mcp
    from context_memory.sources import load_sources

    svc = service or MemoryService(settings, embedder)
    # Requests may run on worker threads; one at a time touches the hub's
    # SQLite connection.
    lock = threading.RLock()
    inbox = settings.data_dir.parent / "inbox" / "remote"

    mcp = build_mcp(lambda: svc, allow_local_ingest=False, actor="hub", lock=lock)

    @mcp.custom_route("/v1/health", methods=["GET"])
    async def health(request: Request):
        dev = current_device.get()
        return JSONResponse({"status": "ok", "device": dev["name"] if dev else None})

    @mcp.custom_route("/v1/context", methods=["GET"])
    async def context(request: Request):
        """Session-start context pack for a working directory (used by the
        SessionStart hook on every machine). Passes the Disclosure Gate."""
        cwd = request.query_params.get("cwd", "")[:1000]
        with lock:
            project_map = load_sources(settings.resolved_sources_file).claude_project_map
            text = svc.context_pack(cwd, recipient=settings.mcp_recipient,
                                    project_map=project_map, actor="hub")
        return JSONResponse({"context": text})

    @mcp.custom_route("/v1/sessions", methods=["POST"])
    async def upload_session(request: Request):
        dev = current_device.get()
        session_id = _SAFE.sub("-", request.headers.get("x-session-id", ""))[:80]
        project_dir = _SAFE.sub("-", request.headers.get("x-project-dir", "unknown"))[:200]
        if not session_id:
            return JSONResponse({"error": "missing X-Session-Id"}, status_code=400)
        body = await request.body()
        if len(body) > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "transcript too large"}, status_code=413)
        if request.headers.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
            if len(body) > MAX_UPLOAD_BYTES:
                return JSONResponse({"error": "transcript too large"}, status_code=413)
        # Store and return at once; the daemon indexes it on its next pass.
        # Ingesting here (minutes for a long session, under the hub lock) froze
        # auth, health and search for every other client (observed).
        parent = _SAFE.sub("-", request.headers.get("x-parent-session", ""))[:80]
        # Subagent transcripts keep the local layout so the daemon recognises them.
        dest = (inbox / dev["name"] / project_dir / parent / "subagents" / f"{session_id}.jsonl"
                if parent else inbox / dev["name"] / project_dir / f"{session_id}.jsonl")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(body)
        account = request.headers.get("x-account") or dev["account"] or dev["name"]
        dest.with_suffix(".meta.json").write_text(json.dumps(
            {"device": dev["name"], "account": slugify(account, dev["name"]), "received_at": utcnow()}))
        tmp.replace(dest)
        with lock:
            svc.sqlite.audit("hub", "session_upload", f"{dev['name']}:{session_id}", "queued",
                             {"bytes": len(body)})
        return JSONResponse({"device": dev["name"], "status": "queued"}, status_code=202)

    class BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            header = request.headers.get("authorization", "")
            token = header[7:].strip() if header.lower().startswith("bearer ") else ""
            with lock:
                dev = lookup_device(svc.sqlite, token) if token else None
                if dev is None:
                    svc.sqlite.audit("hub", "auth", request.url.path, "denied",
                                     {"client": request.client.host if request.client else None})
            if dev is None:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            reset = current_device.set(dev)
            try:
                return await call_next(request)
            finally:
                current_device.reset(reset)

    # Plain JSON responses instead of an SSE stream: fewer round trips per call
    # over the SSH tunnel (tools return one result; nothing is streamed).
    app = mcp.streamable_http_app(host=settings.hub_host, json_response=True)
    app.add_middleware(BearerAuth)
    return app


def serve(settings: Settings, embedder: Embedder) -> None:
    import uvicorn
    app = build_app(settings, embedder)
    uvicorn.run(app, host=settings.hub_host, port=settings.hub_port, log_level="warning")


def start_in_thread(settings: Settings, embedder: Embedder) -> threading.Thread:
    t = threading.Thread(target=serve, args=(settings, embedder), name="cmh-hub", daemon=True)
    t.start()
    return t
