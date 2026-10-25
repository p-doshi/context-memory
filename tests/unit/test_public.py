import json

import pytest

from context_memory import public
from context_memory.server import hub
from context_memory.sources import Sources, load_sources

starlette_testclient = pytest.importorskip("starlette.testclient")


@pytest.fixture
def seeded(svc, settings):
    root = settings.ingest_roots[0]
    from context_memory.ingestion.markdown import ingest_markdown_file
    f = root / "design.md"
    f.write_text("# Design\nThe disclosure gate withholds contact details like owner@example.org "
                 "before anything leaves the memory.", encoding="utf-8")
    ingest_markdown_file(svc, f, "open-proj")
    svc.ingest_text(text="# chat\nPrivate chat about the disclosure gate.", project="open-proj",
                    source="claude", kind="chat", path="x://chat", doc_id="d_chat")
    svc.memory_write(text="Secret plan about the disclosure gate.", kind="note", project="private-proj")
    return Sources(public_projects=["open-proj"])


def test_sources_parse_public_section(tmp_path):
    f = tmp_path / "s.toml"
    f.write_text('[public]\nprojects = ["open-proj"]\n', encoding="utf-8")
    s = load_sources(f)
    assert s.public_projects == ["open-proj"] and "chat" not in s.public_kinds
    assert load_sources(tmp_path / "missing.toml").public_projects == []


def test_public_search_is_isolated_and_redacted(svc, seeded):
    hits = public.search(svc, seeded, "disclosure gate")["hits"]
    assert hits and {h["project"] for h in hits} == {"open-proj"}
    assert all(h["kind"] != "chat" for h in hits)                  # chats excluded by default
    blob = json.dumps(hits)
    assert "example.org" not in blob and "Secret plan" not in blob
    assert ":/" not in blob and "\\\\" not in blob                    # no local paths
    assert public.search(svc, Sources(), "disclosure gate")["hits"] == []


def test_projects_skip_retired_documents(svc, seeded, settings):
    root = settings.ingest_roots[0]
    assert public.projects(svc, seeded)[0]["documents"] == {"note": 1}
    (root / "design.md").unlink()
    svc.retire_missing(root, set())
    p = public.projects(svc, seeded)[0]
    assert p["documents"] == {} and p["recent"] == []


def test_agent_run_follows_ag_ui_event_schema(svc, seeded):
    events = list(public.agent_run(svc, seeded, {
        "threadId": "t1", "runId": "r1",
        "messages": [{"id": "m1", "role": "user", "content": "What does the disclosure gate do?"}]}))
    types = [e["type"] for e in events]
    assert types[0] == "RUN_STARTED" and types[-1] == "RUN_FINISHED"
    assert types.index("TOOL_CALL_START") < types.index("TOOL_CALL_ARGS") < types.index("TOOL_CALL_END") \
        < types.index("TOOL_CALL_RESULT") < types.index("TEXT_MESSAGE_START") < types.index("TEXT_MESSAGE_END")
    required = {"RUN_STARTED": {"threadId", "runId"}, "RUN_FINISHED": {"threadId", "runId"},
                "STEP_STARTED": {"stepName"}, "STEP_FINISHED": {"stepName"},
                "TEXT_MESSAGE_START": {"messageId", "role"}, "TEXT_MESSAGE_CONTENT": {"messageId", "delta"},
                "TEXT_MESSAGE_END": {"messageId"}, "TOOL_CALL_START": {"toolCallId", "toolCallName"},
                "TOOL_CALL_ARGS": {"toolCallId", "delta"}, "TOOL_CALL_END": {"toolCallId"},
                "TOOL_CALL_RESULT": {"messageId", "toolCallId", "content"}}
    for e in events:
        assert required.get(e["type"], set()) <= e.keys(), e
    assert all(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert next(e for e in events if e["type"] == "TEXT_MESSAGE_START")["role"] == "assistant"
    assert events[0]["threadId"] == "t1" and events[0]["runId"] == "r1"
    err = list(public.agent_run(svc, seeded, {"messages": []}))
    assert [e["type"] for e in err] == ["RUN_STARTED", "RUN_ERROR"]


def test_public_routes_scope_rate_limit_and_static(svc, settings, seeded):
    cfg = settings.data_dir.parent / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "sources.toml").write_text('[public]\nprojects = ["open-proj"]\n', encoding="utf-8")
    app = hub.build_app(settings, svc.embedder, service=svc)
    with starlette_testclient.TestClient(app) as c:
        pub = hub.add_device(svc.sqlite, "site", None, scopes="public")
        full = hub.add_device(svc.sqlite, "laptop", None)
        h = {"Authorization": f"Bearer {pub}"}
        assert c.get("/v1/public/search", params={"q": "gate"}, headers={"Authorization": f"Bearer {full}"}).status_code == 403
        assert c.get("/v1/public/search", params={"q": "disclosure gate"}, headers=h).json()["hits"]
        assert c.get("/v1/public/projects", headers=h).json()[0]["project"] == "open-proj"
        r = c.post("/v1/public/agent", headers=h, json={"messages": [{"role": "user", "content": "gate?"}]})
        frames = [json.loads(l[6:]) for l in r.text.split("\n") if l.startswith("data: ")]
        assert r.headers["content-type"].startswith("text/event-stream") and frames[-1]["type"] == "RUN_FINISHED"
        assert c.get("/visitor").status_code == 200 and c.get("/visitor/visitor.js").status_code == 200
        codes = [c.get("/v1/public/projects", headers=h).status_code for _ in range(60)]
        assert 429 in codes                                         # 60/minute per device
