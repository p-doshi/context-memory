"""Owner-report digests: quote verification, incremental runs, failures, report API."""

import pytest

from context_memory import digest
from context_memory.server import hub

starlette_testclient = pytest.importorskip("starlette.testclient")


@pytest.fixture
def client(settings, svc):
    with starlette_testclient.TestClient(hub.build_app(settings, svc.embedder, service=svc)) as c:
        yield c

CHAT = ("**User:**\nPlease combine the 2018 and 2019 zone tables and convert hours to days.\n\n"
        "**Assistant:**\nDone. All 22 zones are now combined in one CSV. The 2018 arrival data is "
        "completely missing for weeks 49 and 50, which looks like a gap in the source export.")


def _ingest(svc, text=CHAT, path="C:/x/.claude/projects/p/s1.jsonl", when="2026-10-20T10:00:00Z"):
    return svc.ingest_text(text=text, project="p", source="claude", kind="chat", path=path,
                           meta={"platform": "claude-code", "first_timestamp": when})


def fake(answer):
    calls = []

    def ask(text):
        calls.append(text)
        if isinstance(answer, Exception):
            raise answer
        return answer
    ask.calls = calls
    return ask


GOOD = {"context": [], "input": [],
        "architecture": [],
        "results": [{"text": "The combined CSV now holds all 22 zones.", "quote": "All 22 zones are now combined"}],
        "findings": [{"text": "2018 arrivals are missing for weeks 49 and 50.",
                      "quote": "The 2018 arrival data is completely missing"},
                     {"text": "An invented conclusion that the chat never states.", "quote": "the model is perfect"}],
        "errors": [{"text": "too short", "quote": "gap in the source export"}]}


def test_verify_keeps_only_quoted_notes_and_adds_the_request(svc):
    items, offered = digest.verify(GOOD, [CHAT])
    assert offered == 4
    fields = [(i["field"], i["text"]) for i in items]
    assert ("results", "The combined CSV now holds all 22 zones.") in fields
    assert ("findings", "2018 arrivals are missing for weeks 49 and 50.") in fields
    assert not any("invented" in t for _, t in fields)          # quote not in the conversation
    assert not any(t == "too short" for _, t in fields)          # under four words
    assert items[0]["field"] == "input" and items[0]["text"].startswith("Please combine the 2018")


def test_runs_once_per_version_and_gives_up_after_two_failures(svc):
    _ingest(svc)
    conn = svc.sqlite.conn
    [(doc_id, fp)] = digest.pending(conn)
    assert digest.digest_document(conn, doc_id, fp, fake(GOOD))["kept"] == 3
    assert digest.pending(conn) == []                             # nothing new: no second call
    _ingest(svc, text=CHAT + "\n\n**User:**\nthanks, now plot it")
    [(doc2, fp2)] = digest.pending(conn)
    assert doc2 == doc_id and fp2 != fp                           # changed conversation: digest again
    boom = fake(RuntimeError("model down"))
    assert digest.digest_document(conn, doc2, fp2, boom)["status"] == "failed"
    assert digest.pending(conn) == [(doc2, fp2)]                  # one failure: retry later
    digest.digest_document(conn, doc2, fp2, boom)
    assert digest.pending(conn) == []                             # two failures on the same version: skip


def test_report_and_passage_endpoints(client, svc):
    _ingest(svc)
    [(doc_id, fp)] = digest.pending(svc.sqlite.conn)
    digest.digest_document(svc.sqlite.conn, doc_id, fp, fake(GOOD))
    admin = hub.add_device(svc.sqlite, "owner", None, scopes="admin")
    h = {"Authorization": f"Bearer {admin}"}
    r = client.get("/v1/console/report", params={"days": 0}, headers=h).json()
    assert r["conversations"] == 1 and r["digested"] == 1 and r["pending_total"] == 0
    assert r["counts"]["results"] == 1 and r["sections"]["findings"][0]["project"] == "p"
    assert r["by_project"] == [["p", 3]]
    assert client.get("/v1/console/report", params={"days": 0, "project": "other"}, headers=h).json()["conversations"] == 0
    note = r["sections"]["findings"][0]
    ps = client.get("/v1/console/passage", params={"doc": note["doc"], "quote": note["quote"]}, headers=h).json()
    assert ps["found"] and "completely missing" in ps["text"]
    assert client.post("/v1/console/report/hide", json={"doc": doc_id}, headers=h).json()["hidden"] is True
    hid = client.get("/v1/console/report", params={"days": 0}, headers=h).json()
    assert hid["conversations"] == 0 and hid["hidden"] == 1
    client.post("/v1/console/report/hide", json={"doc": doc_id, "hidden": False}, headers=h)
    assert client.get("/v1/console/report", params={"days": 0}, headers=h).json()["conversations"] == 1
    assert client.post("/v1/console/report/hide", content=b"nope", headers=h).status_code == 400
    reader = hub.add_device(svc.sqlite, "laptop2", None)
    assert client.get("/v1/console/report", headers={"Authorization": f"Bearer {reader}"}).status_code == 403
    assert client.post("/v1/console/report/hide", json={"doc": doc_id},
                       headers={"Authorization": f"Bearer {reader}"}).status_code == 403


def test_console_page_versions_its_assets_and_serves_fonts(client):
    page = client.get("/console")
    assert page.headers["cache-control"] == "no-cache"
    assert "/console/app.js?v=" in page.text and "/console/style.css?v=" in page.text
    assert client.get("/console/fonts/IBMPlexSans-Regular.woff2").headers["content-type"] == "font/woff2"
    assert client.get("/console/fonts/OFL.txt").status_code == 404           # only the listed font files


def test_unreachable_model_records_nothing_and_stops_the_run(svc, monkeypatch):
    _ingest(svc)
    _ingest(svc, path="C:/x/.claude/projects/p/s2.jsonl")
    conn = svc.sqlite.conn

    def refused(*a, **k):
        raise ConnectionRefusedError(10061, "refused")
    monkeypatch.setattr(digest, "ask_model", refused)
    out = digest.run(conn, "http://127.0.0.1:1", None)
    assert out["done"] == out["failed"] == 0 and "model unavailable" in out["stopped"]
    assert len(digest.pending(conn)) == 2                         # both still queued, no failure recorded
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
