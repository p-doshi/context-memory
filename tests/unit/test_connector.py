"""The claude.ai connector gateway: every protection layer, against a real hub."""

import hashlib
import json
import socket
import threading
import time

import pytest

from context_memory.server import connector, hub

starlette_testclient = pytest.importorskip("starlette.testclient")
uvicorn = pytest.importorskip("uvicorn")

MCP_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}}
HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture
def hub_url(settings, svc):
    app = hub.build_app(settings, svc.embedder, service=svc)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


def _gateway(settings, token, hub_url, allowed=None, per_minute=120, client=("203.0.113.5", 40000)):
    app = connector.build_app(settings, hashlib.sha256(token.encode()).hexdigest(), hub_url,
                              settings.data_dir.parent, allowed=allowed, per_minute=per_minute)
    return starlette_testclient.TestClient(app, client=client)


def test_only_anthropic_addresses_with_the_token_get_through(settings, svc, hub_url):
    token = hub.add_device(svc.sqlite, "claude-ai", None, scopes="read")
    # A direct client outside Anthropic's range: refused before the token is even looked at.
    with _gateway(settings, token, hub_url) as c:
        assert c.post("/mcp", json=MCP_INIT, headers={**HDR, "Authorization": f"Bearer {token}"}).status_code == 403
    # The production path: cloudflared on loopback passes the visitor's address in CF-Connecting-IP.
    with _gateway(settings, token, hub_url, client=("127.0.0.1", 40000)) as c:
        auth = {**HDR, "Authorization": f"Bearer {token}"}
        assert c.post("/mcp", json=MCP_INIT, headers={**auth, "CF-Connecting-IP": "198.51.100.7"}).status_code == 403
        assert c.post("/mcp", json=MCP_INIT, headers={**auth, "CF-Connecting-IP": "160.79.106.20"}).status_code == 200
    # A non-loopback peer cannot claim an Anthropic address with the header.
    with _gateway(settings, token, hub_url) as c:
        assert c.post("/mcp", json=MCP_INIT, headers={**HDR, "Authorization": f"Bearer {token}",
                                                       "CF-Connecting-IP": "160.79.106.20"}).status_code == 403
    # Allow the test client's address: now the token decides.
    with _gateway(settings, token, hub_url, allowed=["0.0.0.0/0"]) as c:
        assert c.post("/mcp", json=MCP_INIT, headers=HDR).status_code == 401
        assert c.post("/mcp", json=MCP_INIT, headers={**HDR, "Authorization": "Bearer wrong"}).status_code == 401
        other = hub.add_device(svc.sqlite, "fir", None)          # a valid hub token, but not the connector's
        assert c.post("/mcp", json=MCP_INIT, headers={**HDR, "Authorization": f"Bearer {other}"}).status_code == 401
        r = c.post("/mcp", json=MCP_INIT, headers={**HDR, "Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and "serverInfo" in r.text
        assert c.get("/v1/context").status_code == 404 and c.get("/console").status_code == 404


def test_writes_are_refused_and_kill_switch_works(settings, svc, hub_url):
    token = hub.add_device(svc.sqlite, "claude-ai", None, scopes="read")
    auth = {**HDR, "Authorization": f"Bearer {token}"}
    with _gateway(settings, token, hub_url, allowed=["0.0.0.0/0"]) as c:
        init = c.post("/mcp", json=MCP_INIT, headers=auth)
        sid = init.headers.get("mcp-session-id")
        h = {**auth, **({"mcp-session-id": sid} if sid else {})}
        c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "memory_write", "arguments": {"text": "x", "kind": "note", "project": "p"}}}
        r = c.post("/mcp", json=call, headers=h)
        assert "read-only" in r.text
        search = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "memory_search", "arguments": {"query": "anything"}}}
        assert c.post("/mcp", json=search, headers=h).status_code == 200
        actors = [r["actor"] for r in svc.sqlite.audit_tail(20) if r["action"] == "search"]
        assert actors and actors[0] == "hub:claude-ai"           # the audit log names the device
        (settings.data_dir.parent / "config").mkdir(exist_ok=True)
        (settings.data_dir.parent / "config" / "connector-disabled").write_text("off")
        assert c.post("/mcp", json=MCP_INIT, headers=auth).status_code == 503


def test_rate_limit(settings, svc, hub_url):
    token = hub.add_device(svc.sqlite, "claude-ai", None, scopes="read")
    auth = {**HDR, "Authorization": f"Bearer {token}"}
    with _gateway(settings, token, hub_url, allowed=["0.0.0.0/0"], per_minute=3) as c:
        codes = [c.post("/mcp", json=MCP_INIT, headers=auth).status_code for _ in range(4)]
        assert codes[-1] == 429


def test_forwarded_for_takes_the_proxys_entry(settings, svc, hub_url):
    token = hub.add_device(svc.sqlite, "claude-ai", None, scopes="read")
    app = connector.build_app(settings, hashlib.sha256(token.encode()).hexdigest(), hub_url,
                              settings.data_dir.parent, ip_header="x-forwarded-for")
    auth = {**HDR, "Authorization": f"Bearer {token}"}
    with starlette_testclient.TestClient(app, client=("127.0.0.1", 40000)) as c:
        ok = c.post("/mcp", json=MCP_INIT, headers={**auth, "X-Forwarded-For": "160.79.106.20"})
        assert ok.status_code == 200
        # A caller who sends its own header cannot claim Anthropic's address: the proxy's entry is last.
        spoof = c.post("/mcp", json=MCP_INIT, headers={**auth, "X-Forwarded-For": "160.79.106.20, 198.51.100.7"})
        assert spoof.status_code == 403
        assert c.post("/mcp", json=MCP_INIT, headers=auth).status_code == 403      # no header: refused
