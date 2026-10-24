import asyncio
import gzip
import json

import pytest

from context_memory.memory.embedder import HashEmbedder
from context_memory.server import hub
from context_memory.server.mcp_server import build_mcp

starlette_testclient = pytest.importorskip("starlette.testclient")


def _transcript(text="Which hydrophone depth for the shelf mooring?"):
    recs = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-10-24T12:00:00Z", "cwd": "/home/p/ocean",
         "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-10-24T12:00:05Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "150 m."}]}},
    ]
    return "\n".join(json.dumps(r) for r in recs).encode()


@pytest.fixture
def client(settings, svc):
    app = hub.build_app(settings, svc.embedder, service=svc)
    with starlette_testclient.TestClient(app) as c:
        yield c


def test_mcp_server_builds_with_expected_tools(svc):
    local = asyncio.run(build_mcp(lambda: svc).list_tools())
    remote = asyncio.run(build_mcp(lambda: svc, allow_local_ingest=False).list_tools())
    names = {t.name for t in local}
    assert {"memory_search", "memory_write", "graph_neighbors", "memory_stats", "ingest_markdown"} <= names
    assert "ingest_markdown" not in {t.name for t in remote}  # hub must not read laptop paths


def test_requests_without_valid_token_are_rejected(client, svc):
    assert client.get("/v1/health").status_code == 401
    assert client.get("/v1/health", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/mcp", headers={"Authorization": "Bearer nope"}, json={}).status_code == 401
    assert svc.sqlite.audit_tail(1)[0]["outcome"] == "denied"


def test_session_upload_is_ingested_and_attributed(client, svc):
    token = hub.add_device(svc.sqlite, "Cluster login", "claude-work")
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/health", headers=auth).json()["device"] == "cluster-login"
    r = client.post("/v1/sessions", content=gzip.compress(_transcript()),
                    headers={**auth, "Content-Encoding": "gzip", "X-Session-Id": "s-123",
                             "X-Project-Dir": "-home-p-ocean"})
    assert r.status_code == 200 and r.json()["status"] == "created"
    hit = svc.memory_search("hydrophone mooring depth")["results"][0]
    assert "account:claude-work" in hit["tags"] and hit["project"] == "ocean"
    # Re-upload of the same transcript is a no-op.
    r2 = client.post("/v1/sessions", content=_transcript(),
                     headers={**auth, "X-Session-Id": "s-123", "X-Project-Dir": "-home-p-ocean"})
    assert r2.json()["status"] == "unchanged"


def test_revoked_device_and_bad_upload(client, svc):
    token = hub.add_device(svc.sqlite, "server5", None)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/sessions", content=b"x", headers=auth).status_code == 400  # no session id
    assert hub.revoke_device(svc.sqlite, "server5")
    assert client.get("/v1/health", headers=auth).status_code == 401
    assert [d["name"] for d in hub.list_devices(svc.sqlite)] == ["server5"]


def test_tokens_are_stored_hashed(svc):
    token = hub.add_device(svc.sqlite, "laptop2", None)
    stored = svc.sqlite.conn.execute("SELECT token_sha256 FROM devices").fetchone()[0]
    assert token not in stored and len(stored) == 64
    with pytest.raises(Exception):
        hub.add_device(svc.sqlite, "laptop2", None)  # names are unique


def test_push_client_hook_end_returns_fast(tmp_path, monkeypatch):
    import subprocess
    import sys
    import time
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / "client" / "cmh_push.py"
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"),
           "CMH_HUB_URL": "http://127.0.0.1:9", "PATH": ""}
    t = time.perf_counter()
    p = subprocess.run([sys.executable, str(script), "hook-end"],
                       input=json.dumps({"session_id": "s1", "transcript_path": str(tmp_path / "s1.jsonl")}),
                       text=True, env=env, capture_output=True, timeout=10)
    assert p.returncode == 0
    assert time.perf_counter() - t < 1.5  # inside the SessionEnd default budget
    queued = (tmp_path / "state" / "cmh" / "queue.jsonl").read_text().splitlines()
    assert len(queued) == 1 and json.loads(queued[0])["session_id"] == "s1"
