"""LanceDB vector index over chunks.

Rows carry only the fields vector search filters on. The provenance record
lives in SQLite; callers hydrate results by chunk_id.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import lancedb
import pyarrow as pa

TABLE = "chunks"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class LanceStore:
    def __init__(self, path: Path, dim: int, embedding_model: str):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.dim = dim
        self.embedding_model = embedding_model
        # The daemon and the hub write/read through separate connections. Without
        # a consistency interval a connection keeps reading the table version it
        # opened and never sees later writes (measured). Re-check every second.
        self.db = lancedb.connect(str(self.path), read_consistency_interval=timedelta(seconds=1))
        self.table = self._open_or_create()

    def _schema(self) -> pa.Schema:
        return pa.schema([
            pa.field("chunk_id", pa.string(), nullable=False),
            pa.field("doc_id", pa.string(), nullable=False),
            pa.field("project", pa.string(), nullable=False),
            pa.field("kind", pa.string(), nullable=False),
            pa.field("embedding_model", pa.string(), nullable=False),
            pa.field("is_current", pa.bool_(), nullable=False),
            pa.field("vector", pa.list_(pa.float32(), self.dim), nullable=False),
        ])

    def _open_or_create(self):
        listed = self.db.list_tables()
        existing = getattr(listed, "tables", listed)
        if TABLE not in existing:
            return self.db.create_table(TABLE, schema=self._schema())
        table = self.db.open_table(TABLE)
        stored_dim = table.schema.field("vector").type.list_size
        if stored_dim != self.dim:
            raise RuntimeError(
                f"Lance index has {stored_dim}-d vectors but the embedder produces "
                f"{self.dim}-d. Re-embedding is required before switching models."
            )
        return table

    def add(self, rows: list[dict]) -> None:
        if rows:
            self.table.add(rows)

    def retire(self, chunk_ids: list[str]) -> None:
        if chunk_ids:
            ids = ", ".join(_quote(c) for c in chunk_ids)
            self.table.update(where=f"chunk_id IN ({ids})", values={"is_current": False})

    def delete(self, chunk_ids: list[str]) -> None:
        if chunk_ids:
            ids = ", ".join(_quote(c) for c in chunk_ids)
            self.table.delete(f"chunk_id IN ({ids})")

    def search(self, vector: list[float], k: int, project: str | None = None,
               kinds: list[str] | None = None, include_retired: bool = False) -> list[dict]:
        clauses = [f"embedding_model = {_quote(self.embedding_model)}"]
        if not include_retired:
            clauses.append("is_current = true")
        if project:
            clauses.append(f"project = {_quote(project)}")
        if kinds:
            clauses.append(f"kind IN ({', '.join(_quote(k) for k in kinds)})")
        q = (
            self.table.search(vector)
            .distance_type("cosine")
            .where(" AND ".join(clauses), prefilter=True)
            .select(["chunk_id", "doc_id"])
            .limit(k)
        )
        return [
            {"chunk_id": r["chunk_id"], "doc_id": r["doc_id"], "distance": float(r["_distance"])}
            for r in q.to_list()
        ]

    def count(self) -> int:
        return self.table.count_rows()

    def small_fragments(self) -> int:
        try:
            return int(self.table.stats()["fragment_stats"]["num_small_fragments"])
        except Exception:
            return 0

    def optimize(self) -> dict:
        """Compact small fragments (each ingest appends one) and drop versions
        older than an hour. The hour leaves other connections, which refresh
        every second, far from ever reading a deleted file."""
        before = self.small_fragments()
        self.table.optimize(cleanup_older_than=timedelta(hours=1))
        return {"small_fragments_before": before, "small_fragments_after": self.small_fragments()}

    def ids(self, current_only: bool = False) -> set[str]:
        tbl = self.table.to_arrow()
        if current_only:
            tbl = tbl.filter(tbl.column("is_current"))
        return set(tbl.column("chunk_id").to_pylist())
