"""SQLite metadata store: documents, chunks, jobs, graph tables, audit log.

SQLite holds the full provenance record for every chunk. LanceDB holds only
what vector search needs, and search results are hydrated from here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from context_memory.util import utcnow

SCHEMA_VERSION = 3

# v3: device permission scopes; tamper-evident audit chain. Each audit row
# written from v3 on stores hash = sha256(previous hash | row fields). Rows
# from before v3 have NULL hash; the chain starts at the first hashed row.
V3_MIGRATION = """
ALTER TABLE devices ADD COLUMN scopes TEXT NOT NULL DEFAULT 'read,write,upload,context';
ALTER TABLE audit_log ADD COLUMN hash TEXT;
"""
GENESIS = "0" * 64

# v2: full-text index over chunks, kept in step by a trigger. Retired chunks
# stay indexed and are filtered by joining on chunks.valid_to.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, title, section_path, chunk_id UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks
BEGIN
  INSERT INTO chunks_fts (text, title, section_path, chunk_id)
  VALUES (new.text, coalesce(new.title, ''), coalesce(new.section_path, ''), new.chunk_id);
END;
"""

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
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, valid_to);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, valid_to);
CREATE INDEX IF NOT EXISTS idx_edges_evidence ON edges(evidence_chunk_id);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  entity_id TEXT,
  event_type TEXT,
  chunk_id TEXT,
  occurred_at TEXT,
  ingested_at TEXT,
  details JSON
);

-- Remote machines allowed to use the hub. Only a SHA-256 of each token is
-- stored; the token itself is shown once when the device is added.
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  account TEXT,
  token_sha256 TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  revoked_at TEXT,
  last_seen TEXT
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

# Dropped from keyword queries (they match most chunks). Kept short on purpose.
STOPWORDS = frozenset("""
a an and are as at be but by can could did do does for from had has have how i if in into is it
its me my not of on or our should so than that the their them then there these they this to was
we were what when where which who why will with would you your
""".split())


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
        if version < 2:
            self.conn.executescript(FTS_SCHEMA)
            self.conn.execute(
                "INSERT INTO chunks_fts (text, title, section_path, chunk_id) "
                "SELECT text, coalesce(title, ''), coalesce(section_path, ''), chunk_id FROM chunks "
                "WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks_fts)")
        if version < 3:
            # Idempotent: add each v3 column only if it is not there yet.
            for table, column, ddl in (("devices", "scopes", V3_MIGRATION.splitlines()[1]),
                                       ("audit_log", "hash", V3_MIGRATION.splitlines()[2])):
                cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self.conn.execute(ddl.rstrip(";"))
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

    @staticmethod
    def _audit_hash(prev: str, ts: str, actor: str, action: str, target: str | None,
                    outcome: str, details: str) -> str:
        payload = "\x1f".join([prev, ts, actor, action, target or "", outcome, details])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def audit(self, actor: str, action: str, target: str | None, outcome: str,
              details: dict | None = None) -> None:
        """Append a hash-chained audit row. The read of the previous hash and the
        insert run under the write lock, so concurrent writers (daemon, hub)
        cannot fork the chain."""
        ts, det = utcnow(), json.dumps(details or {})
        own_txn = not self.conn.in_transaction
        if own_txn:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT hash FROM audit_log WHERE hash IS NOT NULL ORDER BY seq DESC LIMIT 1").fetchone()
            h = self._audit_hash(row[0] if row else GENESIS, ts, actor, action, target, outcome, det)
            self.conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target, outcome, details, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (ts, actor, action, target, outcome, det, h))
        except BaseException:
            if own_txn:
                self.conn.execute("ROLLBACK")
            raise
        if own_txn:
            self.conn.execute("COMMIT")

    def verify_audit_chain(self) -> dict:
        """Recompute the chain. Any edited, deleted or inserted row after the
        chain started shows up as the first broken sequence number."""
        prev, n, started = GENESIS, 0, False
        for seq, ts, actor, action, target, outcome, det, h in self.conn.execute(
                "SELECT seq, ts, actor, action, target, outcome, details, hash FROM audit_log ORDER BY seq"):
            if h is None:
                if started:
                    return {"ok": False, "broken_at": seq, "reason": "unhashed row inside chain", "rows": n}
                continue
            started = True
            if self._audit_hash(prev, ts, actor, action, target, outcome, det) != h:
                return {"ok": False, "broken_at": seq, "reason": "hash mismatch", "rows": n}
            prev, n = h, n + 1
        return {"ok": True, "rows": n, "head": prev}

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

    def bm25_search(self, query: str, k: int, project: str | None = None,
                    kinds: list[str] | None = None, include_retired: bool = False,
                    projects: list[str] | None = None) -> list[dict]:
        """BM25 over chunk text/title/section. Each query word becomes a term;
        identifiers like audit_log_no_update become phrases of their parts.

        Common English words are dropped: they match most chunks and made
        ranking slow (p95 3.4 s at 12k chunks, measured) while adding little.
        The best matches are taken inside FTS5 before joining to chunks."""
        terms = []
        for word in query.split():
            parts = re.findall(r"[A-Za-z0-9]+", word)
            if len(parts) == 1 and parts[0].lower() not in STOPWORDS:
                terms.append(f'"{parts[0]}"')
            elif len(parts) > 1:
                terms.append('"' + " ".join(parts) + '"')
        if not terms:
            return []
        filtered = bool(project or kinds or projects is not None) or not include_retired
        inner = max(4 * k, 100) if filtered else k
        while True:
            sql = ("SELECT c.chunk_id, c.doc_id, f.score FROM ("
                   "SELECT chunk_id, bm25(chunks_fts) AS score FROM chunks_fts "
                   "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?) f "
                   "JOIN chunks c ON c.chunk_id = f.chunk_id WHERE 1 = 1")
            params: list = [" OR ".join(terms), inner]
            if not include_retired:
                sql += " AND c.valid_to IS NULL"
            if project:
                sql += " AND c.project = ?"
                params.append(project)
            if kinds:
                sql += f" AND c.kind IN ({', '.join('?' * len(kinds))})"
                params += kinds
            if projects is not None:
                sql += f" AND c.project IN ({', '.join('?' * len(projects)) or 'NULL'})"
                params += projects
            sql += " ORDER BY f.score LIMIT ?"
            params.append(k)
            rows = self.conn.execute(sql, params).fetchall()
            # Filters can discard most of the inner top-N; widen once if short.
            if len(rows) >= k or inner >= 20_000 or not filtered:
                break
            inner *= 10
        # bm25() is lower-is-better; expose it negated so higher is better.
        return [{"chunk_id": r[0], "doc_id": r[1], "bm25": -r[2]} for r in rows]

    def chunk_ids(self, current_only: bool = False) -> set[str]:
        q = "SELECT chunk_id FROM chunks" + (" WHERE valid_to IS NULL" if current_only else "")
        return {r[0] for r in self.conn.execute(q)}

    def forget_document(self, doc_id: str) -> None:
        """Make the next ingest or web sync re-embed the document: clear the stored
        content hash and the remote updated_at used by browser syncs."""
        self.conn.execute(
            "UPDATE documents SET content_hash = 'sha256:invalidated', "
            "meta = json_remove(meta, '$.updated_at') WHERE doc_id = ?", (doc_id,))

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
