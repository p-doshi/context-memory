"""Export the public subset of the memory into a separate store.

The public node (server/public_node.py) serves visitors from this copy only,
so a fault in the public server cannot reach private material: the private
store is never opened by that process. The export holds the current chunks of
the projects listed under [public], restricted to the public kinds, with every
text field already passed through the Disclosure Gate as recipient "public".
Vectors are copied, not recomputed; they were embedded from the ungated text,
which differs only where the gate withheld an attribute.

Layout: <root>/store-<UTC stamp>/data/{metadata.sqlite3,lance}, and
<root>/current naming the newest store. Older stores are pruned.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc

from context_memory.memory.graph import Graph
from context_memory.memory.lance_store import LanceStore
from context_memory.memory.sqlite_store import SQLiteStore
from context_memory.policy.disclosure import gate
from context_memory.util import utcnow

KEEP_STORES = 2


def _gated(text: str | None) -> str | None:
    return None if text is None else gate(text, "public", chunk_tier="public").text


def _public_tags(tags: str | None) -> str:
    """Drop account labels; keep e.g. lang:py."""
    try:
        items = json.loads(tags or "[]")
    except ValueError:
        items = []
    return json.dumps([t for t in items if isinstance(t, str) and not t.startswith("account:")])


def current_store(root: Path) -> Path | None:
    pointer = Path(root) / "current"
    if not pointer.exists():
        return None
    store = Path(root) / pointer.read_text().strip()
    return store if (store / "data" / "metadata.sqlite3").exists() else None


def open_stores(settings):
    """The private SQLite and Lance stores without an embedding model (export
    only reads rows and copies vectors)."""
    from types import SimpleNamespace

    import lancedb
    tbl = lancedb.connect(str(settings.lance_path)).open_table("chunks")
    dim = tbl.schema.field("vector").type.list_size
    first = tbl.search().limit(1).select(["embedding_model"]).to_list()
    model = first[0]["embedding_model"] if first else settings.embedding_model
    return SimpleNamespace(sqlite=SQLiteStore(settings.sqlite_path),
                           lance=LanceStore(settings.lance_path, dim, model))


def export_public(svc, sources, root: Path, actor: str = "cli") -> dict:
    root = Path(root)
    projects, kinds = list(sources.public_projects), list(sources.public_kinds)
    if not projects:
        raise ValueError("no public projects: list them under [public] in sources.toml")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    store = root / f"store-{stamp}"
    data = store / "data"
    q = svc.sqlite.conn.execute
    pm, km = ", ".join("?" * len(projects)), ", ".join("?" * len(kinds))
    docs = [dict(r) for r in q(
        f"SELECT * FROM documents d WHERE project IN ({pm}) AND kind IN ({km}) AND EXISTS "
        "(SELECT 1 FROM chunks c WHERE c.doc_id = d.doc_id AND c.valid_to IS NULL)",
        (*projects, *kinds)).fetchall()]
    doc_ids = [d["doc_id"] for d in docs]
    chunks = []
    for i in range(0, len(doc_ids), 500):
        part = doc_ids[i:i + 500]
        chunks += [dict(r) for r in q(
            f"SELECT * FROM chunks WHERE valid_to IS NULL AND doc_id IN ({', '.join('?' * len(part))})",
            part).fetchall()]

    pub = SQLiteStore(data / "metadata.sqlite3")
    try:
        job = "job_public_export"
        with pub.transaction() as conn:
            conn.execute("INSERT INTO ingestion_jobs (job_id, source, target, started_at, finished_at, status) "
                         "VALUES (?, 'export', 'public', ?, ?, 'ok')", (job, utcnow(), utcnow()))
            for d in docs:
                d.update(title=_gated(d["title"]), ingestion_job_id=job, meta="{}",
                         tags=_public_tags(d["tags"]))
                cols = ", ".join(d)
                conn.execute(f"INSERT INTO documents ({cols}) VALUES ({', '.join('?' * len(d))})",
                             tuple(d.values()))
            for c in chunks:
                c.update(text=_gated(c["text"]), title=_gated(c["title"]),
                         section_path=_gated(c["section_path"]), summary=_gated(c["summary"]),
                         ingestion_job_id=job, tags=_public_tags(c["tags"]))
                cols = ", ".join(c)
                conn.execute(f"INSERT INTO chunks ({cols}) VALUES ({', '.join('?' * len(c))})",
                             tuple(c.values()))
        edges = Graph(pub).rebuild()["edges_current"]
    finally:
        pub.close()

    lance = LanceStore(data / "lance", svc.lance.dim, svc.lance.embedding_model)
    ids = pa.array([c["chunk_id"] for c in chunks], pa.string())
    rows = svc.lance.table.to_arrow()
    rows = rows.filter(pc.and_(pc.is_in(rows["chunk_id"], value_set=ids), rows["is_current"]))
    if rows.num_rows:
        lance.table.add(rows)

    (root / "current").write_text(store.name)
    for old in sorted(root.glob("store-*"))[:-KEEP_STORES]:
        shutil.rmtree(old, ignore_errors=True)  # an open store is pruned next time
    out = {"store": store.name, "projects": projects, "kinds": kinds, "documents": len(docs),
           "chunks": len(chunks), "vectors": rows.num_rows, "edges": edges}
    svc.sqlite.audit(actor, "public_export", str(store), "ok",
                     {k: out[k] for k in ("documents", "chunks", "vectors")})
    return out
