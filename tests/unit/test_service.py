import pytest

from context_memory.ingestion.markdown import (
    PathNotAllowed, ingest_markdown_dir, ingest_markdown_file,
)
from context_memory.memory.service import (
    PROVENANCE_FIELDS, UNTRUSTED_CLOSE, UNTRUSTED_OPEN, IngressRejected,
)

DOC_V1 = """# Downscaling architecture

## Loss
The model uses a physics-informed spectral loss on sea surface temperature.

## Baseline
SR3 was the baseline model.
"""

DOC_V2 = DOC_V1.replace("SR3 was the baseline model.", "Latent diffusion replaced the SR3 baseline.")


def test_write_then_search_returns_full_provenance(svc):
    svc.memory_write(text="Spectral loss replaced MSE for SST downscaling.",
                     kind="decision", project="ocean", title="Loss decision")
    res = svc.memory_search("spectral loss SST", project="ocean")
    assert len(res["results"]) == 1
    r = res["results"][0]
    assert set(PROVENANCE_FIELDS) <= r["provenance"].keys()
    assert r["provenance"]["verification_status"] == "unverified"
    assert r["provenance"]["source_type"] == "decision_record"
    assert r["text"].startswith(UNTRUSTED_OPEN) and r["text"].endswith(UNTRUSTED_CLOSE)
    assert any(w["level"] == "info" for w in res["warnings"])


def test_project_and_kind_filters(svc):
    svc.memory_write(text="alpha spectral note", kind="note", project="ocean")
    svc.memory_write(text="alpha spectral decision", kind="decision", project="ocean")
    svc.memory_write(text="alpha spectral other", kind="note", project="acoustics")
    assert {r["project"] for r in svc.memory_search("alpha", project="ocean")["results"]} == {"ocean"}
    kinds = {r["kind"] for r in svc.memory_search("alpha", kinds=["decision"])["results"]}
    assert kinds == {"decision"}


def test_secret_rejected_before_any_write(svc):
    secret = "ghp_" + "b" * 36
    with pytest.raises(IngressRejected):
        svc.memory_write(text=f"token is {secret}", kind="note", project="ocean")
    assert svc.sqlite.stats()["documents"] == 0
    assert svc.lance.count() == 0
    rejected = svc.sqlite.audit_tail(1)[0]
    assert rejected["outcome"] == "rejected"
    assert secret not in str(rejected["details"])


def test_invalid_inputs(svc):
    with pytest.raises(ValueError):
        svc.memory_write(text="x", kind="gossip", project="ocean")
    with pytest.raises(ValueError):
        svc.memory_write(text="x", kind="note", project="Bad Project'")
    with pytest.raises(ValueError):
        svc.memory_search("x", project="'; DROP")


def test_reingest_unchanged_is_skipped(svc, settings):
    f = settings.ingest_roots[0] / "arch.md"
    f.write_text(DOC_V1, encoding="utf-8")
    first = ingest_markdown_file(svc, f, "ocean")
    second = ingest_markdown_file(svc, f, "ocean")
    assert first["status"] == "created" and second["status"] == "unchanged"
    assert svc.lance.count() == first["chunks"]


def test_changed_file_retires_old_chunks(svc, settings):
    f = settings.ingest_roots[0] / "arch.md"
    f.write_text(DOC_V1, encoding="utf-8")
    v1 = ingest_markdown_file(svc, f, "ocean")
    f.write_text(DOC_V2, encoding="utf-8")
    v2 = ingest_markdown_file(svc, f, "ocean")
    assert v2["status"] == "updated" and v2["retired"] == v1["chunks"]

    current = svc.memory_search("baseline model", project="ocean")["results"]
    texts = " ".join(r["text"] for r in current)
    assert "Latent diffusion replaced" in texts
    assert "SR3 was the baseline model." not in texts

    history = svc.memory_search("baseline model", project="ocean", include_retired=True)["results"]
    assert any(r["provenance"]["verification_status"] == "stale" for r in history)


def test_revert_to_earlier_version_does_not_collide(svc, settings):
    f = settings.ingest_roots[0] / "arch.md"
    statuses = []
    for text in (DOC_V1, DOC_V2, DOC_V1):
        f.write_text(text, encoding="utf-8")
        statuses.append(ingest_markdown_file(svc, f, "ocean"))
    assert [s["status"] for s in statuses] == ["created", "updated", "updated"]
    stats = svc.sqlite.stats()
    assert stats["chunks_current"] == statuses[0]["chunks"]
    assert svc.lance.count() == stats["chunks_current"] + stats["chunks_retired"]


def test_path_outside_allowlist_denied(svc, tmp_path):
    outside = tmp_path / "elsewhere.md"
    outside.write_text("# x\ny", encoding="utf-8")
    with pytest.raises(PathNotAllowed):
        ingest_markdown_file(svc, outside, "ocean")


def test_blocked_and_oversized_files(svc, settings):
    root = settings.ingest_roots[0]
    (root / "big.md").write_text("# h\n" + "x " * settings.max_file_bytes, encoding="utf-8")
    with pytest.raises(IngressRejected, match="limit"):
        ingest_markdown_file(svc, root / "big.md", "ocean")


def test_directory_ingest_skips_git_and_respects_budget(svc, settings):
    root = settings.ingest_roots[0]
    (root / ".git").mkdir()
    (root / ".git" / "x.md").write_text("# git internals\nz", encoding="utf-8")
    for i in range(5):
        (root / f"n{i}.md").write_text(f"# Note {i}\nbody {i}", encoding="utf-8")
    results = ingest_markdown_dir(svc, root, "ocean", max_files=3)
    assert sum(r["status"] == "created" for r in results) == 3
    assert results[-1]["status"] == "stopped"
    assert not any(".git" in r["path"] for r in results)


def test_lance_failure_rolls_back_sqlite(svc, monkeypatch):
    def boom(rows):
        raise OSError("disk full")
    monkeypatch.setattr(svc.lance, "add", boom)
    with pytest.raises(OSError):
        svc.memory_write(text="will not persist", kind="note", project="ocean")
    assert svc.sqlite.stats()["documents"] == 0
    assert svc.sqlite.stats()["chunks_current"] == 0


def test_retire_path_prefix_keeps_history(svc, settings):
    root = settings.ingest_roots[0]
    (root / "old").mkdir()
    (root / "old" / "a.md").write_text("# A\nspectral loss notes", encoding="utf-8")
    (root / "keep.md").write_text("# K\nspectral loss decision", encoding="utf-8")
    for f in (root / "old" / "a.md", root / "keep.md"):
        ingest_markdown_file(svc, f, "ocean")
    out = svc.retire_path_prefix((root / "old").resolve().as_posix())
    assert out["documents"] == 1 and out["chunks_retired"] >= 1
    current = svc.memory_search("spectral loss", project="ocean")["results"]
    assert all("old/a.md" not in r["provenance"]["source_path"] for r in current)
    history = svc.memory_search("spectral loss", project="ocean", include_retired=True)["results"]
    assert any(r["provenance"]["verification_status"] == "stale" for r in history)


def test_search_modes_bm25_finds_identifiers_hybrid_fuses(svc):
    svc.memory_write(text="The flag is set by msvcrt_lock_guard in the daemon.", kind="code", project="p")
    svc.memory_write(text="Prevents two background processes from running concurrently.", kind="note", project="p")
    bm = svc.memory_search("msvcrt_lock_guard", project="p", mode="bm25")["results"]
    assert bm and "msvcrt_lock_guard" in bm[0]["text"]
    hy = svc.memory_search("msvcrt_lock_guard", project="p", mode="hybrid")
    assert hy["mode"] == "hybrid" and len(hy["results"]) == 2
    assert all(0 < r["score"] <= round(2 / 61, 4) for r in hy["results"])  # scores are rounded to 4 dp
    with pytest.raises(ValueError):
        svc.memory_search("x", mode="magic")


def test_bm25_respects_retirement_and_punctuation(svc, settings):
    f = settings.ingest_roots[0] / "a.md"
    f.write_text("# A\nzebra_crossing alpha", encoding="utf-8")
    ingest_markdown_file(svc, f, "p")
    f.write_text("# A\nunrelated text", encoding="utf-8")
    ingest_markdown_file(svc, f, "p")
    assert svc.memory_search("zebra_crossing", mode="bm25")["results"] == []
    assert svc.memory_search("zebra_crossing", mode="bm25", include_retired=True)["results"]
    assert svc.memory_search('"; DROP TABLE chunks; --', mode="bm25")["results"] == []
