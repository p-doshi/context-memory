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
