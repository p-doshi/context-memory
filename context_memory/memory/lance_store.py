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
    def __init__(self, path: Path, dim: int, embedding_model: str,
                 max_cache_rows: int = 250_000):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.dim = dim
        self.embedding_model = embedding_model
        # 250k x 1024 float32 is ~1 GB of RAM; above that, search goes to LanceDB.
        self.max_cache_rows = max_cache_rows
        self._cache: dict | None = None
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

    def rename_project(self, old: str, new: str) -> None:
        self.table.update(where=f"project = {_quote(old)}", values={"project": new})

    def set_project(self, chunk_ids: list[str], project: str) -> None:
        for i in range(0, len(chunk_ids), 500):
            ids = ", ".join(_quote(c) for c in chunk_ids[i:i + 500])
            self.table.update(where=f"chunk_id IN ({ids})", values={"project": project})

    def delete(self, chunk_ids: list[str]) -> None:
        if chunk_ids:
            ids = ", ".join(_quote(c) for c in chunk_ids)
            self.table.delete(f"chunk_id IN ({ids})")

    def _snapshot(self):
        """In-memory copy of the table for exact search, reloaded when the table
        version changes. Brute force over a NumPy matrix took 3.9 ms vs ~70 ms
        for LanceDB's unindexed search at 12k x 1024 (measured); results are
        identical, since both are exact cosine. None above max_cache_rows."""
        import numpy as np

        version = self.table.version
        if self._cache is not None and self._cache["version"] == version:
            return self._cache
        if self.table.count_rows() > self.max_cache_rows:
            self._cache = None
            return None
        tbl = self.table.to_arrow()
        # np.array copies: Arrow's zero-copy view is read-only and is normalised below.
        m = np.array(tbl.column("vector").combine_chunks().flatten().to_numpy(),
                     dtype=np.float32).reshape(-1, self.dim)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        m /= np.where(norms == 0, 1, norms)
        self._cache = {
            "version": version, "matrix": m,
            "chunk_id": np.array(tbl.column("chunk_id").to_pylist(), dtype=object),
            "doc_id": np.array(tbl.column("doc_id").to_pylist(), dtype=object),
            "project": np.array(tbl.column("project").to_pylist(), dtype=object),
            "kind": np.array(tbl.column("kind").to_pylist(), dtype=object),
            "model_ok": np.array(tbl.column("embedding_model").to_pylist(), dtype=object) == self.embedding_model,
            "current": np.asarray(tbl.column("is_current").to_pylist(), dtype=bool),
        }
        return self._cache

    def search(self, vector: list[float], k: int, project: str | None = None,
               kinds: list[str] | None = None, include_retired: bool = False,
               projects: list[str] | None = None) -> list[dict]:
        import numpy as np

        snap = self._snapshot()
        if snap is not None:
            mask = snap["model_ok"].copy()
            if not include_retired:
                mask &= snap["current"]
            if project:
                mask &= snap["project"] == project
            if kinds:
                mask &= np.isin(snap["kind"], kinds)
            if projects is not None:
                mask &= np.isin(snap["project"], projects)
            idx = np.flatnonzero(mask)
            if idx.size == 0:
                return []
            q = np.asarray(vector, dtype=np.float32)
            q /= np.linalg.norm(q) or 1.0
            sims = snap["matrix"][idx] @ q
            top = idx[np.argsort(-sims)[:k]] if idx.size <= k else \
                idx[np.argpartition(-sims, k - 1)[:k]]
            order = np.argsort(-(snap["matrix"][top] @ q))
            return [{"chunk_id": snap["chunk_id"][i], "doc_id": snap["doc_id"][i],
                     "distance": float(1.0 - snap["matrix"][i] @ q)} for i in top[order]]
        return self._lance_search(vector, k, project, kinds, include_retired, projects)

    def _lance_search(self, vector: list[float], k: int, project: str | None,
                      kinds: list[str] | None, include_retired: bool,
                      projects: list[str] | None = None) -> list[dict]:
        clauses = [f"embedding_model = {_quote(self.embedding_model)}"]
        if projects is not None:
            clauses.append(f"project IN ({', '.join(_quote(x) for x in projects) or chr(39)*2})")
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
