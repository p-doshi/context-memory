import json
import re

import pytest

from context_memory.public_export import current_store, export_public
from context_memory.server import public_node
from context_memory.sources import Sources

starlette_testclient = pytest.importorskip("starlette.testclient")


@pytest.fixture
def exported(svc, settings):
    root = settings.ingest_roots[0]
    from context_memory.ingestion.markdown import ingest_markdown_file
    f = root / "design.md"
    f.write_text("# Design\nThe disclosure gate withholds contact details like owner@example.org "
                 "before anything leaves the memory.", encoding="utf-8")
    ingest_markdown_file(svc, f, "open-proj")
    svc.ingest_text(text="# chat\nPrivate chat about the disclosure gate.", project="open-proj",
                    source="claude", kind="chat", path="x://chat", doc_id="d_chat",
                    tags=["account:someone"])
    svc.memory_write(text="Secret plan about the disclosure gate.", kind="note", project="private-proj")
    out_root = settings.data_dir.parent / "public"
    out = export_public(svc, Sources(public_projects=["open-proj"]), out_root)
    return out_root, out


def _client(settings, root, **kw):
    app = public_node.build_app(settings, root, "http://127.0.0.1:1", "", llm_url=None, **kw)
    return starlette_testclient.TestClient(app)


def test_export_holds_only_public_gated_material(exported):
    root, out = exported
    assert out["documents"] == 1 and out["vectors"] == out["chunks"] > 0
    db = (current_store(root) / "data" / "metadata.sqlite3").read_bytes()
    assert b"Secret plan" not in db and b"Private chat" not in db
    assert b"owner@example.org" not in db and b"account:someone" not in db


def test_humans_get_html_agents_get_markdown(settings, exported):
    root, _ = exported
    with _client(settings, root) as c:
        browser = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Firefox/140.0", "Accept": "text/html,*/*"}
        assert c.get("/", headers=browser).headers["content-type"].startswith("text/html")
        for agent in ({"User-Agent": "Claude-User/1.0"}, {"User-Agent": "curl/8.0", "Accept": "*/*"},
                      {"User-Agent": "Mozilla/5.0", "Accept": "text/markdown"}):
            r = c.get("/guide", headers=agent)
            assert r.headers["content-type"].startswith("text/markdown"), agent
        assert "text/markdown" in c.get("/", headers=browser).headers["link"]
        assert c.get("/?view=agent", headers=browser).text.startswith("# ")
        assert c.get("/llms.txt").text.startswith("# ")
        assert c.get("/vercel.json").status_code == 404 and c.get("/../pyproject.toml").status_code == 404


def test_search_falls_back_to_bm25_and_hides_private(settings, exported):
    root, _ = exported
    with _client(settings, root) as c:
        r = c.get("/api/search", params={"q": "disclosure gate"}).json()
        assert r["mode"] == "bm25" and r["hits"]
        blob = json.dumps(r)
        assert "Secret plan" not in blob and "example.org" not in blob
        stats = c.get("/api/stats").json()
        assert stats["documents"] == {"note": 1} and stats["answer_model"] == "excerpts"


def test_agent_run_streams_ag_ui_and_gate_demo(settings, exported):
    root, _ = exported
    with _client(settings, root) as c:
        r = c.post("/api/agent", json={"messages": [{"role": "user", "content": "What does the gate do?"}]})
        frames = [json.loads(l[6:]) for l in r.text.split("\n") if l.startswith("data: ")]
        types = [f["type"] for f in frames]
        assert types[0] == "RUN_STARTED" and types[-1] == "RUN_FINISHED" and "TEXT_MESSAGE_CONTENT" in types
        g = c.post("/api/gate", json={"text": "mail me at a.b@example.org"}).json()
        assert "example.org" in g["owner"]["text"] and "example.org" not in g["public"]["text"]
        assert g["public"]["withheld"]


def test_cors_only_for_configured_origins(settings, exported):
    root, _ = exported
    s = settings.model_copy(update={"public_allowed_origins": ["https://site.example"]})
    with _client(s, root) as c:
        ok = c.get("/api/stats", headers={"Origin": "https://site.example"})
        assert ok.headers.get("access-control-allow-origin") == "https://site.example"
        bad = c.get("/api/stats", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in bad.headers
        assert c.options("/api/agent", headers={"Origin": "https://evil.example"}).status_code == 403


def test_no_store_yet_returns_503(settings, tmp_path):
    with _client(settings, tmp_path / "empty") as c:
        assert c.get("/api/stats").status_code == 503
        assert c.get("/", headers={"Accept": "text/html"}).status_code == 200  # the site still works


def test_markdown_twin_names_every_pipeline_stage():
    site = public_node.SITE
    stages = json.loads((site / "pipeline.json").read_text())["stages"]
    md = (site / "index.md").read_text()
    assert [s["id"] for s in stages] == re.findall(r"^\d+\. \*\*(\w+)\*\*", md, re.M)


def test_vercel_deploy_folder_fills_api_host(tmp_path):
    from context_memory.site_deploy import write_deploy
    files = write_deploy(tmp_path / "deploy", "api.example.org")
    assert "index.html" in files
    assert "API_HOST" not in (tmp_path / "deploy" / "vercel.json").read_text()
    assert json.loads((tmp_path / "deploy" / "config.json").read_text())["api"] == "https://api.example.org"
    with pytest.raises(ValueError):
        write_deploy(tmp_path / "x", "not a host; rm -rf")
