import json
import os
import subprocess

from context_memory.ingestion.claude_code import (
    ingest_config_dir, parse_session, project_slug, render,
)
from context_memory.ingestion.code import ingest_codebase, make_code_chunker
from context_memory.memory.embedder import HashEmbedder
from context_memory.sources import load_sources

count = HashEmbedder().count_tokens


def _session(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def _user(text, uuid, ts="2026-10-24T13:00:00.000Z", cwd="C:/work/Ocean-Model"):
    return {"type": "user", "uuid": uuid, "timestamp": ts, "cwd": cwd, "sessionId": "s1",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(blocks, uuid="a1"):
    return {"type": "assistant", "uuid": uuid, "timestamp": "2026-10-24T13:00:05.000Z",
            "message": {"role": "assistant", "model": "claude-opus-5-5", "content": blocks}}


SESSION = [
    {"type": "ai-title", "aiTitle": "Spectral loss refactor", "sessionId": "s1"},
    _user("# Make the loss spectral\nplease", "u1"),
    _assistant([{"type": "thinking", "thinking": "secret plan"},
                {"type": "text", "text": "Switching to a spectral loss."},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "loss.py"}}]),
    {"type": "user", "uuid": "u2", "timestamp": "2026-10-24T13:00:06.000Z",
     "message": {"role": "user", "content": [{"type": "tool_result", "content": "file dump"}]}},
    _assistant([{"type": "text", "text": "Done; tests pass."}], "a2"),
    _user("thanks", "u3"),
    "not json at all",
]


def test_parse_session_groups_turns_and_drops_tool_payloads(tmp_path):
    p = tmp_path / "s1.jsonl"
    _session(p, SESSION[:-1])
    p.write_text(p.read_text() + "\nnot json", encoding="utf-8")
    s = parse_session(p)
    assert s.title == "Spectral loss refactor"
    assert len(s.turns) == 2
    assert s.turns[0].assistant == ["Switching to a spectral loss.", "Done; tests pass."]
    assert s.turns[0].tools == ["Edit"]
    md = render(s)
    assert "file dump" not in md and "secret plan" not in md
    assert "\\# Make the loss spectral" in md  # chat text cannot create sections
    assert "## Turn 1 · 2026-10-24T13:00:00Z · msg u1" in md


def test_project_slug():
    assert project_slug("C:/Users/x/OneDrive/Documents/SAR", "fallback") == "sar"
    assert project_slug(None, "c--Users-X") == "c--users-x"
    assert project_slug("C:/a/Transit Time & METR", "f") == "transit-time-metr"


def test_ingest_config_dir_respects_idle_and_redacts(svc, tmp_path):
    cfg = tmp_path / "claude"
    old = cfg / "projects" / "c--work-Ocean-Model" / "s1.jsonl"
    token = "ghp_" + "c" * 36
    _session(old, [_user(f"my token is {token}", "u1"), _assistant([{"type": "text", "text": "ok"}])])
    os.utime(old, (1_700_000_000, 1_700_000_000))
    live = cfg / "projects" / "c--work-Ocean-Model" / "s2.jsonl"
    _session(live, [_user("in progress", "u9")])

    res = ingest_config_dir(svc, cfg, "dal", idle_seconds=600)
    by_status = {r["status"] for r in res}
    assert by_status == {"created", "active"}
    created = next(r for r in res if r["status"] == "created")
    assert created["redactions"] == 1

    hits = svc.memory_search("token", project="ocean-model")["results"]
    assert hits and token not in hits[0]["text"] and "[REDACTED:github_token]" in hits[0]["text"]
    assert "account:dal" in hits[0]["tags"]
    assert hits[0]["provenance"]["source_type"] == "chat"


def test_python_chunker_splits_on_definitions_with_line_ranges(tmp_path):
    src = "import os\n\nX = 1\n\n\ndef a():\n    return 1\n\n\nclass B:\n    def m(self):\n        pass\n"
    chunks = make_code_chunker(tmp_path / "m.py", count, max_tokens=8)(src)
    sections = [c.section_path for c in chunks]
    assert sections[0].startswith("L1-L") and "module" in sections[0]
    assert any("def a" in s for s in sections)
    assert any(s.startswith("L10-L12") and "class B" in s for s in sections)


def test_code_chunker_handles_syntax_errors_and_other_languages(tmp_path):
    bad = make_code_chunker(tmp_path / "x.py", count, 50)("def (:\n  broken\n\nok = 1\n")
    assert len(bad) >= 1
    js = make_code_chunker(tmp_path / "x.js", count, 50)("function a() {}\n\nfunction b() {}\n")
    assert js[0].section_path.startswith("L1-L")


def test_ingest_codebase_uses_git_ls_files(svc, settings):
    root = settings.ingest_roots[0] / "repo"
    root.mkdir()
    (root / "keep.py").write_text("def kept():\n    return 1\n", encoding="utf-8")
    (root / "ignored.py").write_text("def ignored():\n    pass\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / "notes.md").write_text("# Notes\nabout kept\n", encoding="utf-8")
    (root / "data.bin").write_bytes(b"\0\1\2")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    res = ingest_codebase(svc, root, "repo")
    paths = {os.path.basename(r["path"]) for r in res}
    assert "keep.py" in paths and "notes.md" in paths
    assert "ignored.py" not in paths and "data.bin" not in paths
    hit = svc.memory_search("kept", project="repo", kinds=["code"])["results"][0]
    assert hit["section_path"].startswith("L1-L2")


def test_load_sources(tmp_path):
    f = tmp_path / "sources.toml"
    f.write_text("""
[[accounts]]
label = "dal"
provider = "claude"
claude_config_dir = "~/.claude"

[[accounts]]
label = "pplx"
provider = "perplexity"

[[codebase]]
path = "C:/JARVIS/context-memory"
project = "context-memory"

[claude_code.project_map]
"c--Users-X-SAR" = "sar"
""", encoding="utf-8")
    s = load_sources(f)
    assert [a.label for a in s.accounts] == ["dal", "pplx"]
    assert s.accounts[0].claude_config_dir.name == ".claude"
    assert s.codebases[0].project == "context-memory"
    assert s.claude_project_map == {"c--Users-X-SAR": "sar"}
    assert load_sources(tmp_path / "missing.toml").accounts == []
