import sqlite3

import pytest

from context_memory.memory.lance_store import LanceStore
from context_memory.memory.sqlite_store import SQLiteStore


def test_audit_log_is_append_only(tmp_path):
    db = SQLiteStore(tmp_path / "m.sqlite3")
    db.audit("test", "search", None, "ok")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("UPDATE audit_log SET outcome = 'tampered'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("DELETE FROM audit_log")
    assert len(db.audit_tail()) == 1


def test_reopen_is_idempotent_and_versioned(tmp_path):
    SQLiteStore(tmp_path / "m.sqlite3").close()
    db = SQLiteStore(tmp_path / "m.sqlite3")
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_refuses_newer_schema(tmp_path):
    db = SQLiteStore(tmp_path / "m.sqlite3")
    db.conn.execute("PRAGMA user_version = 99")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        SQLiteStore(tmp_path / "m.sqlite3")


def test_transaction_rolls_back(tmp_path):
    db = SQLiteStore(tmp_path / "m.sqlite3")
    with pytest.raises(ZeroDivisionError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO ingestion_jobs (job_id, source, started_at, status) "
                         "VALUES ('j1', 'file', 'now', 'running')")
            1 / 0
    assert db.conn.execute("SELECT COUNT(*) FROM ingestion_jobs").fetchone()[0] == 0


def _row(cid, vec, project="p", current=True):
    return {"chunk_id": cid, "doc_id": "d1", "project": project, "kind": "note",
            "embedding_model": "m", "is_current": current, "vector": vec}


def test_lance_filters_and_retire(tmp_path):
    ls = LanceStore(tmp_path / "lance", dim=3, embedding_model="m")
    ls.add([_row("a", [1, 0, 0]), _row("b", [0.9, 0.1, 0]), _row("c", [1, 0, 0], project="q")])
    assert [h["chunk_id"] for h in ls.search([1, 0, 0], 5, project="p")] == ["a", "b"]
    ls.retire(["a"])
    assert [h["chunk_id"] for h in ls.search([1, 0, 0], 5, project="p")] == ["b"]
    assert {h["chunk_id"] for h in ls.search([1, 0, 0], 5, project="p", include_retired=True)} == {"a", "b"}


def test_lance_quotes_filter_values(tmp_path):
    ls = LanceStore(tmp_path / "lance", dim=3, embedding_model="m")
    ls.add([_row("x'y", [1, 0, 0])])
    ls.retire(["x'y"])
    assert ls.search([1, 0, 0], 5) == []


def test_lance_dimension_mismatch(tmp_path):
    LanceStore(tmp_path / "lance", dim=3, embedding_model="m")
    with pytest.raises(RuntimeError, match="Re-embedding"):
        LanceStore(tmp_path / "lance", dim=4, embedding_model="m")


def test_v1_database_migrates_and_backfills_fts(tmp_path):
    path = tmp_path / "m.sqlite3"
    db = SQLiteStore(path)
    # Simulate a v1 database: drop the FTS objects, insert a chunk, reset version.
    db.conn.executescript("DROP TRIGGER chunks_fts_insert; DROP TABLE chunks_fts;")
    db.conn.execute("INSERT INTO ingestion_jobs (job_id, source, started_at, status) VALUES ('j','file','t','ok')")
    db.conn.execute("INSERT INTO documents (doc_id, project, source, source_type, kind, path, "
                    "source_version, content_hash, sensitivity, created_at, updated_at, ingestion_job_id) "
                    "VALUES ('d','p','file','file','note','x','v','h','public','t','t','j')")
    db.conn.execute("INSERT INTO chunks (chunk_id, doc_id, ordinal, project, source, kind, path, text, "
                    "tokens, embedding_model, source_version, created_at, updated_at, valid_from, "
                    "sensitivity, ingestion_job_id, content_hash) VALUES ('c','d',0,'p','file','note',"
                    "'x','the audit_log_no_update trigger',3,'m','v','t','t','t','public','j','h')")
    db.conn.execute("PRAGMA user_version = 1")
    db.close()
    db = SQLiteStore(path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert [h["chunk_id"] for h in db.bm25_search("audit_log_no_update", 5)] == ["c"]


def test_second_connection_sees_new_writes(tmp_path):
    import time
    writer = LanceStore(tmp_path / "lance", dim=3, embedding_model="m")
    writer.add([_row("first", [1, 0, 0])])
    reader = LanceStore(tmp_path / "lance", dim=3, embedding_model="m")  # e.g. the hub
    writer.add([_row("second", [1, 0, 0])])
    time.sleep(1.2)  # consistency interval is 1 s
    assert {h["chunk_id"] for h in reader.search([1, 0, 0], 10)} == {"first", "second"}


def test_optimize_compacts_fragments_and_keeps_rows(tmp_path):
    ls = LanceStore(tmp_path / "lance", dim=3, embedding_model="m")
    for i in range(40):
        ls.add([_row(f"c{i}", [1, 0, i / 40])])
    assert ls.small_fragments() >= 32
    out = ls.optimize()
    assert out["small_fragments_after"] < out["small_fragments_before"]
    assert ls.count() == 40 and len(ls.search([1, 0, 0], 50)) == 40


def test_in_memory_search_matches_lancedb_exactly(tmp_path):
    import random
    rnd = random.Random(0)
    ls = LanceStore(tmp_path / "lance", dim=8, embedding_model="m")
    rows = []
    for i in range(300):
        v = [rnd.gauss(0, 1) for _ in range(8)]
        rows.append({"chunk_id": f"c{i}", "doc_id": f"d{i % 30}", "project": "p" if i % 3 else "q",
                     "kind": "note" if i % 2 else "code", "embedding_model": "m",
                     "is_current": i % 10 != 0, "vector": v})
    ls.add(rows)
    for _ in range(10):
        q = [rnd.gauss(0, 1) for _ in range(8)]
        for kw in ({}, {"project": "p"}, {"kinds": ["code"]}, {"include_retired": True}):
            fast = ls.search(q, 20, **kw)
            slow = ls._lance_search(q, 20, kw.get("project"), kw.get("kinds"), kw.get("include_retired", False))
            assert [h["chunk_id"] for h in fast] == [h["chunk_id"] for h in slow]
            assert all(abs(a["distance"] - b["distance"]) < 1e-4 for a, b in zip(fast, slow))


def test_in_memory_cache_follows_new_versions_and_size_cap(tmp_path):
    ls = LanceStore(tmp_path / "lance", dim=3, embedding_model="m")
    ls.add([_row("a", [1, 0, 0])])
    assert [h["chunk_id"] for h in ls.search([1, 0, 0], 5)] == ["a"]
    ls.add([_row("b", [1, 0, 0])])
    assert {h["chunk_id"] for h in ls.search([1, 0, 0], 5)} == {"a", "b"}
    capped = LanceStore(tmp_path / "lance", dim=3, embedding_model="m", max_cache_rows=1)
    assert capped._snapshot() is None and len(capped.search([1, 0, 0], 5)) == 2


def test_snapshot_is_verified_and_retained(svc, settings):
    import time
    from context_memory.server import backup
    home = settings.data_dir.parent
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "sources.toml").write_text("# owner config")
    (home / "config" / "devices").mkdir(exist_ok=True)
    (home / "config" / "devices" / "x.token").write_text("secret")
    svc.memory_write(text="alpha beta", kind="note", project="p")
    snaps = []
    for _ in range(3):
        snaps.append(backup.make_snapshot(svc, home, keep=2))
        time.sleep(1.1)  # snapshot names have one-second resolution
    assert all(s["ok"] and s["chunks"] == s["vectors"] == 1 for s in snaps)
    kept = sorted(p.name for p in (home / "backup").iterdir())
    assert len(kept) == 2                                   # retention
    latest = home / "backup" / kept[-1]
    assert (latest / "config" / "sources.toml").exists()
    assert not (latest / "config" / "devices").exists()     # tokens never backed up
    assert backup.verify(latest, svc.embedder.dim, svc.embedder.model_name)["ok"]
    assert backup.last_snapshot_age(home) < 60
