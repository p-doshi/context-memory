"""Owner filing: suggestions, moving chats into a project, undo, and syncs keeping the filing."""

import pytest

from context_memory.server import hub

starlette_testclient = pytest.importorskip("starlette.testclient")


@pytest.fixture
def client(settings, svc):
    with starlette_testclient.TestClient(hub.build_app(settings, svc.embedder, service=svc)) as c:
        yield c


def _chat(svc, uid, text, project="claude-web", when="2026-10-10T10:00:00Z"):
    return svc.ingest_text(text=text, project=project, source="claude", kind="chat",
                           path=f"claude.ai://acct/chat/{uid}", doc_id=f"d_chat_{uid}",
                           meta={"platform": "claude.ai", "created_at": when})


def test_suggest_file_undo_and_sync_keeps_filing(client, svc):
    svc.ingest_text(text="# Window Length Pilot\n\nWindow length pilot for vessel detection on hydrophone data.",
                    project="studies", source="claude", kind="note", path="claude-project://studies/WLP",
                    title="Window Length Pilot")
    _chat(svc, "a", "# c\n**User:**\nhelp me read the window length pilot results for vessel detection")
    _chat(svc, "b", "# c\n**User:**\nwrite a birthday poem for my sister")
    admin = hub.add_device(svc.sqlite, "owner", None, scopes="admin")
    h = {"Authorization": f"Bearer {admin}"}
    sug = client.get("/v1/console/filing", params={"project": "studies"}, headers=h).json()
    assert sug["queries"] == ["Window Length Pilot"]
    assert sug["suggestions"][0]["doc"] == "d_chat_a" and sug["suggestions"][0]["project"] == "claude-web"

    r = client.post("/v1/console/filing", json={"action": "file", "project": "studies", "docs": ["d_chat_a"]},
                    headers=h).json()
    assert r["moved"] == [{"doc": "d_chat_a", "from": "claude-web"}]
    assert svc.sqlite.get_document("d_chat_a")["project"] == "studies"
    hits = svc.memory_search("window length pilot", project="studies", mode="vector", recipient="owner")["results"]
    assert any(x["provenance"]["doc_id"] == "d_chat_a" for x in hits)          # vector index moved too
    # The sync re-imports the chat (it changed) under its own project: the filing wins.
    _chat(svc, "a", "# c\n**User:**\nhelp me read the window length pilot results for vessel detection\nthanks!")
    assert svc.sqlite.get_document("d_chat_a")["project"] == "studies"
    again = client.get("/v1/console/filing", params={"project": "studies"}, headers=h).json()
    assert [f["doc"] for f in again["filed"]] == ["d_chat_a"]
    assert all(s["doc"] != "d_chat_a" for s in again["suggestions"])

    assert client.post("/v1/console/filing", json={"action": "unfile", "project": "studies", "docs": ["d_chat_a"]},
                       headers=h).json()["restored"] == 1
    assert svc.sqlite.get_document("d_chat_a")["project"] == "claude-web"
    assert client.get("/v1/console/filing", params={"project": "studies"}, headers=h).json()["filed"] == []

    client.post("/v1/console/filing", json={"action": "dismiss", "project": "studies", "docs": ["d_chat_a"]}, headers=h)
    assert all(s["doc"] != "d_chat_a" for s in
               client.get("/v1/console/filing", params={"project": "studies"}, headers=h).json()["suggestions"])
    assert client.post("/v1/console/filing", json={"action": "move"}, headers=h).status_code == 400
    reader = hub.add_device(svc.sqlite, "laptop2", None)
    assert client.post("/v1/console/filing", json={"action": "file", "project": "studies", "docs": ["d_chat_b"]},
                       headers={"Authorization": f"Bearer {reader}"}).status_code == 403
