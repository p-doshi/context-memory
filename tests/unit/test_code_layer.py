"""Codebase layer: the push client uploads a repository's changed source files after a
session; the hub queues them; the daemon indexes them under the session's project."""

import importlib.util
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from context_memory.server import daemon, hub

uvicorn = pytest.importorskip("uvicorn")


def _client(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "cmh_push_code", Path(__file__).resolve().parents[2] / "client" / "cmh_push.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    state = tmp_path / "state"
    monkeypatch.setattr(mod, "STATE_DIR", state)
    monkeypatch.setattr(mod, "QUEUE", state / "queue.jsonl")
    monkeypatch.setattr(mod, "LOG", state / "push.log")
    monkeypatch.setattr(mod, "CODE_STATE", state / "code.json")
    state.mkdir()
    return mod


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "audio_comp"
    (r / "src").mkdir(parents=True)
    (r / "data").mkdir()
    (r / "src" / "model.py").write_text("def masked_spectrogram_loss(x):\n    return x.mean()\n")
    (r / "notes.md").write_text("# Notes\n\nWindow length pilot: 5 s beats 2 s.\n")
    (r / "data" / "labels.csv").write_text("id,label\n1,vessel\n")
    (r / ".env").write_text("API_KEY=abc\n")
    (r / "big.py").write_text("x = 1\n" * 120_000)
    (r / "nb.ipynb").write_text(json.dumps({"cells": [
        {"cell_type": "code", "source": ["print('pilot')"], "outputs": [{"text": "SECRET-OUTPUT-ROW 42"}]}]}))
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return r


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


def test_selection_skips_data_keys_big_files_and_notebook_outputs(monkeypatch, tmp_path, repo):
    mod = _client(monkeypatch, tmp_path)
    files = mod.code_files(repo)
    assert sorted(files) == ["nb.ipynb", "notes.md", "src/model.py"]
    assert "print('pilot')" in files["nb.ipynb"] and "SECRET-OUTPUT-ROW" not in files["nb.ipynb"]


def test_upload_index_change_and_delete(monkeypatch, tmp_path, repo, settings, svc, hub_url):
    mod = _client(monkeypatch, tmp_path)
    tok = hub.add_device(svc.sqlite, "fir", None)
    cfg = {"hub_url": hub_url}
    item = {"code": str(repo / "src"), "project_dir": "-scratch-pdoshi-audio-comp"}
    mod.upload_code(cfg, tok, item)
    home = settings.data_dir.parent
    bundles = list((home / "inbox" / "remote" / "fir" / "_code").glob("*.json"))
    assert len(bundles) == 1
    out = daemon.process_remote_code(svc, home, {"-scratch-pdoshi-audio-comp": "audio_comp"})
    assert out.get("created") == 3 and not list((home / "inbox" / "remote" / "fir" / "_code").glob("*.json"))
    hits = svc.memory_search("masked spectrogram loss", project="audio_comp", mode="bm25", recipient="owner")["results"]
    assert hits and hits[0]["provenance"]["source_path"].startswith("remote://fir")
    assert hits[0]["provenance"]["verification_status"] == "unverified"   # not on this disk: not "missing"
    actors = [r["actor"] for r in svc.sqlite.audit_tail(30) if r["action"] == "code_upload"]
    assert actors == ["hub:fir"]

    mod.upload_code(cfg, tok, item)                                       # nothing changed: nothing sent
    assert not list((home / "inbox" / "remote" / "fir" / "_code").glob("*.json"))

    (repo / "src" / "model.py").write_text("def masked_spectrogram_loss(x):\n    return x.sum()\n")
    _git(repo, "rm", "-q", "notes.md")
    mod.upload_code(cfg, tok, item)
    [b] = list((home / "inbox" / "remote" / "fir" / "_code").glob("*.json"))
    sent = json.loads(b.read_text())
    assert [f["path"] for f in sent["files"]] == ["src/model.py"] and sent["removed"] == ["notes.md"]
    out = daemon.process_remote_code(svc, home, {"-scratch-pdoshi-audio-comp": "audio_comp"})
    assert out.get("updated") == 1 and out.get("retired-deleted") == 1
    left = svc.memory_search("window length pilot", project="audio_comp", mode="bm25", recipient="owner")["results"]
    assert all(r["title"] != "notes.md" for r in left)                  # the deleted file is retired


def test_hook_end_queues_the_repository(monkeypatch, tmp_path, repo):
    mod = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "detach", lambda *a: None)
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"transcript_path": str(tmp_path / "proj-dir" / "s1.jsonl"), "session_id": "s1", "cwd": str(repo)})))
    assert mod.main(["cmh_push.py", "hook-end"]) == 0
    queued = [json.loads(line) for line in (tmp_path / "state" / "queue.jsonl").read_text().splitlines()]
    assert {"code": str(repo), "project_dir": "proj-dir"} in queued
