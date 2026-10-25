"""claude.ai custom-connector gateway: the private memory's MCP, read-only, for
claude.ai chats.

claude.ai connects from Anthropic's cloud, so this endpoint is reachable from
the internet (through cloudflared). Each request must pass every layer:

  1. path: only /mcp exists here;
  2. source address: Anthropic's published outbound range (160.79.104.0/21,
     platform.claude.com/docs/en/api/ip-addresses), read from cloudflared's
     CF-Connecting-IP and trusted only when the peer is loopback;
  3. bearer token: the dedicated "claude-ai" device token (configured in the
     claude.ai connector's request headers), checked by SHA-256 here and again
     by the hub;
  4. scope: that device has only "read", so the hub refuses memory_write; the hub
     never offers local ingest to remote clients;
  5. the Disclosure Gate: hub MCP results go out as recipient "cloud_model";
  6. a rate limit, a kill switch (<home>/config/connector-disabled), and the hub's
     audit log of every call.

The gateway holds no data: it forwards to the hub on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("cmh.connector")
ANTHROPIC_OUTBOUND = ["160.79.104.0/21"]
FORWARD_REQ = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
FORWARD_RESP = ("content-type", "mcp-session-id", "mcp-protocol-version")


def build_app(settings, token_sha256: str, hub_url: str, home: Path,
              allowed: list[str] | None = None, per_minute: int = 120):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    nets = [ipaddress.ip_network(c) for c in (allowed if allowed is not None else ANTHROPIC_OUTBOUND)]
    hits: list[float] = []
    lock = threading.Lock()

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else ""
        if peer in ("127.0.0.1", "::1"):
            return request.headers.get("cf-connecting-ip", peer)
        return peer

    def refuse(status: int, why: str, ip: str):
        log.warning("connector refused %s from %s: %s", status, ip, why)
        return JSONResponse({"error": why}, status_code=status)

    async def mcp(request: Request):
        ip = client_ip(request)
        if (home / "config" / "connector-disabled").exists():
            return refuse(503, "connector switched off by the owner", ip)
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return refuse(403, "unknown source address", ip)
        if nets and not any(addr in n for n in nets):
            return refuse(403, "source address not allowed", ip)
        auth = request.headers.get("authorization", "")
        presented = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not presented or hashlib.sha256(presented.encode()).hexdigest() != token_sha256:
            return refuse(401, "missing or wrong token", ip)
        now = time.time()
        with lock:
            hits[:] = [t for t in hits if now - t < 60]
            if len(hits) >= per_minute:
                return refuse(429, "rate limit", ip)
            hits.append(now)
        body = await request.body()
        if len(body) > 256_000:
            return refuse(413, "request too large", ip)
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQ}
        headers["Authorization"] = f"Bearer {presented}"

        def forward():
            req = urllib.request.Request(hub_url.rstrip("/") + "/mcp", data=body or None,
                                         method=request.method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()

        status, rh, data = await asyncio.to_thread(forward)
        out = {k: v for k, v in rh.items() if k.lower() in FORWARD_RESP}
        out["Cache-Control"] = "no-store"
        return Response(data, status_code=status, headers=out)

    async def not_found(request: Request):
        return Response("not found", status_code=404)

    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]),
                             Route("/{path:path}", not_found, methods=["GET", "POST", "PUT", "DELETE", "HEAD"])])


def serve(settings, host: str = "127.0.0.1", port: int = 8790) -> None:
    import uvicorn
    from context_memory.secrets import broker
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the connector gateway binds loopback; cloudflared carries outside traffic")
    token = broker.get_secret("claude-ai-connector") or ""
    if not token:
        raise SystemExit("no connector token: run `cmh connector setup` first")
    app = build_app(settings, hashlib.sha256(token.encode()).hexdigest(),
                    f"http://127.0.0.1:{settings.hub_port}", settings.data_dir.parent,
                    allowed=settings.connector_allowed_cidrs)
    uvicorn.run(app, host=host, port=port, log_level="warning")
