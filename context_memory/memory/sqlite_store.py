"""SQLite metadata store: documents, chunks, jobs, graph tables, audit log.

SQLite holds the full provenance record for every chunk. LanceDB holds only
what vector search needs, and search results are hydrated from here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from context_memory.util import utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingestion_jobs (
  job_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  target TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','ok','skipped','rejected','failed')),
  details JSON
);

CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  source TEXT NOT NULL,
  source_type TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  title TEXT,
  source_version TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  sensitivity TEXT NOT NULL,
  tags JSON NOT NULL DEFAULT '[]',
  meta JSON NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  ingestion_job_id TEXT NOT NULL REFERENCES ingestion_jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_path ON documents(project, path);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL REFERENCES documents(doc_id),
  ordinal INTEGER NOT NULL,
  project TEXT NOT NULL,
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  title TEXT,
  section_path TEXT,
  text TEXT NOT NULL,
  summary TEXT,
  tokens INTEGER NOT NULL,
  embedding_model TEXT NOT NULL,
  source_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  superseded_by TEXT,
  confidence REAL NOT NULL DEFAULT 0.5,
  importance REAL NOT NULL DEFAULT 0.5,
  tags JSON NOT NULL DEFAULT '[]',
  sensitivity TEXT NOT NULL,
  ingestion_job_id TEXT NOT NULL REFERENCES ingestion_jobs(job_id),
  content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_current ON chunks(project, valid_to);

CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  project TEXT,
  description TEXT,
  embedding BLOB,
  first_seen TEXT,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS edges (
  edge_id TEXT PRIMARY KEY,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  relation TEXT NOT NULL,
  confidence REAL,
  evidence_chunk_id TEXT,
  valid_from TEXT,
  valid_to TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  entity_id TEXT,
  event_type TEXT,
  chunk_id TEXT,
  occurred_at TEXT,
  ingested_at TEXT,
  details JSON
);

CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  outcome TEXT NOT NULL,
  details JSON
);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

JSON_COLUMNS = {"tags", "meta", "details"}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in JSON_COLUMNS & d.keys():
        if isinstance(d[k], str):
            d[k] = json.loads(d[k])
    return d


class SQLiteStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{version} is newer than this code (v{SCHEMA_VERSION})"
            )
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- audit -------------------------------------------------------------

    def audit(self, actor: str, action: str, target: str | None, outcome: str,
              details: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (ts, actor, action, target, outcome, details) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (utcnow(), actor, action, target, outcome, json.dumps(details or {})),
        )

    def audit_tail(self, n: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (n,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- jobs --------------------------------------------------------------

    def start_job(self, job_id: str, source: str, target: str | None) -> None:
        self.conn.execute(
            "INSERT INTO ingestion_jobs (job_id, source, target, started_at, status) "
            "VALUES (?, ?, ?, ?, 'running')",
            (job_id, source, target, utcnow()),
        )

    def finish_job(self, job_id: str, status: str, details: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE ingestion_jobs SET status = ?, finished_at = ?, details = ? WHERE job_id = ?",
            (status, utcnow(), json.dumps(details or {}), job_id),
        )

    # -- documents and chunks ----------------------------------------------

    def get_document(self, doc_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def upsert_document(self, conn: sqlite3.Connection, doc: dict) -> None:
        cols = list(doc.keys())
        vals = [json.dumps(doc[c]) if c in JSON_COLUMNS else doc[c] for c in cols]
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("doc_id", "created_at"))
        conn.execute(
            f"INSERT INTO documents ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(doc_id) DO UPDATE SET {updates}",
            vals,
        )

    def insert_chunks(self, conn: sqlite3.Connection, chunks: list[dict]) -> None:
        if not chunks:
            return
        cols = list(chunks[0].keys())
        conn.executemany(
            f"INSERT INTO chunks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [[json.dumps(c[k]) if k in JSON_COLUMNS else c[k] for k in cols] for c in chunks],
        )

    def current_chunk_ids(self, doc_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT chunk_id FROM chunks WHERE doc_id = ? AND valid_to IS NULL ORDER BY ordinal",
            (doc_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def retire_chunks(self, conn: sqlite3.Connection, chunk_ids: list[str], when: str,
                      superseded_by_doc_version: str | None = None) -> None:
        conn.executemany(
            "UPDATE chunks SET valid_to = ?, updated_at = ?, superseded_by = ? WHERE chunk_id = ?",
            [(when, when, superseded_by_doc_version, cid) for cid in chunk_ids],
        )

    def get_chunks(self, chunk_ids: list[str]) -> dict[str, dict]:
        if not chunk_ids:
            return {}
        q = f"SELECT * FROM chunks WHERE chunk_id IN ({', '.join('?' * len(chunk_ids))})"
        return {r["chunk_id"]: _row_to_dict(r) for r in self.conn.execute(q, chunk_ids)}

    def stats(self) -> dict:
        one = lambda q: self.conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "chunks_current": one("SELECT COUNT(*) FROM chunks WHERE valid_to IS NULL"),
            "chunks_retired": one("SELECT COUNT(*) FROM chunks WHERE valid_to IS NOT NULL"),
            "projects": [r[0] for r in self.conn.execute(
                "SELECT DISTINCT project FROM documents ORDER BY project")],
            "audit_events": one("SELECT COUNT(*) FROM audit_log"),
        }
