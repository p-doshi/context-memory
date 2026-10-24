"""Temporal knowledge graph over the same SQLite store (design §4.4).

Version 1 is deterministic: no language model. Entities and edges come from
structure that can be read exactly:

  document  -part_of->      project
  document  -from_account-> account        (chats)
  document  -on_platform->  platform       (chats)
  file      -defines->      symbol         (Python: top-level classes/functions)
  module    -imports->      module         (Python import statements)
  chat      -mentions->     file           (repo file paths named in chat text)

Every edge carries the chunk it was read from as evidence. When a document
is re-ingested, edges whose evidence chunk was retired get valid_to, so the
graph follows the same supersession rules as retrieval.

Entity names are identifiers, paths, labels and titles only; no free text from
chunks is copied into the graph (contextual-integrity addendum §10.3).
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from context_memory.memory.sqlite_store import SQLiteStore
from context_memory.util import stable_id, utcnow

RELATIONS = {"part_of", "from_account", "on_platform", "defines", "imports", "mentions"}

# Repo-relative file paths in chat text, e.g. context_memory/memory/service.py
_FILE_MENTION = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|md|toml|yml|yaml|json|ps1|sh|ts|tsx|js|ipynb|csv|nc))(?![\w/])")


class Graph:
    def __init__(self, sqlite: SQLiteStore):
        self.db = sqlite

    # -- writes ------------------------------------------------------------

    def entity(self, name: str, type_: str, project: str | None = None) -> str:
        eid = stable_id("e", type_, name, project or "")
        now = utcnow()
        self.db.conn.execute(
            "INSERT INTO entities (entity_id, name, type, project, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET last_seen = excluded.last_seen",
            (eid, name, type_, project, now, now))
        return eid

    def edge(self, src: str, dst: str, relation: str, evidence_chunk_id: str | None,
             confidence: float = 1.0) -> None:
        assert relation in RELATIONS, relation
        eid = stable_id("g", src, dst, relation, evidence_chunk_id or "")
        now = utcnow()
        self.db.conn.execute(
            "INSERT OR IGNORE INTO edges (edge_id, src, dst, relation, confidence, evidence_chunk_id, "
            "valid_from, valid_to, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (eid, src, dst, relation, confidence, evidence_chunk_id, now, now))

    def retire_evidence(self, chunk_ids: list[str], when: str) -> None:
        self.db.conn.executemany(
            "UPDATE edges SET valid_to = ? WHERE evidence_chunk_id = ? AND valid_to IS NULL",
            [(when, c) for c in chunk_ids])

    # -- extraction ----------------------------------------------------------

    def index_document(self, doc: dict, chunks: list[dict]) -> int:
        """Extract entities and edges for one freshly ingested document."""
        if not chunks:
            return 0
        n0 = self._edge_count()
        project, first = doc["project"], chunks[0]["chunk_id"]
        p = self.entity(project, "project")
        d = self.entity(doc["path"], "document", project)
        self._label(d, doc.get("title"))
        self.edge(d, p, "part_of", first)

        meta = doc.get("meta") or {}
        if doc["kind"] == "chat":
            if meta.get("account"):
                self.edge(d, self.entity(meta["account"], "account"), "from_account", first)
            if meta.get("platform"):
                self.edge(d, self.entity(meta["platform"], "platform"), "on_platform", first)
            for c in chunks:
                for path in sorted(set(_FILE_MENTION.findall(c["text"]))):
                    if "/" in path:  # bare names like "loss.py" are too ambiguous
                        self.edge(d, self.entity(path, "file_ref"), "mentions", c["chunk_id"], 0.7)

        if doc["kind"] == "code" and doc["path"].endswith(".py"):
            self._python(doc, chunks, d, project)
        return self._edge_count() - n0

    def _python(self, doc: dict, chunks: list[dict], file_entity: str, project: str) -> None:
        module = _module_name(doc.get("title") or doc["path"])
        m = self.entity(module, "module", project)
        self.edge(file_entity, m, "defines", chunks[0]["chunk_id"])
        for c in chunks:
            try:
                tree = ast.parse(c["text"])
            except SyntaxError:
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    s = self.entity(f"{module}.{node.name}", kind, project)
                    self.edge(m, s, "defines", c["chunk_id"])
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.edge(m, self.entity(alias.name, "module"), "imports", c["chunk_id"])
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    self.edge(m, self.entity(node.module, "module"), "imports", c["chunk_id"])

    def _label(self, entity_id: str, title: str | None) -> None:
        if title:
            self.db.conn.execute("UPDATE entities SET description = ? WHERE entity_id = ?",
                                 (title[:200], entity_id))

    def _edge_count(self) -> int:
        return self.db.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    # -- reads -----------------------------------------------------------------

    def find(self, name: str, limit: int = 10) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT entity_id, name, type, project, description FROM entities "
            "WHERE name = ? OR name LIKE ? ORDER BY (name = ?) DESC, length(name) LIMIT ?",
            (name, f"%{name}%", name, limit)).fetchall()
        return [dict(r) for r in rows]

    def neighbors(self, entity_id: str, relation: str | None = None, depth: int = 1,
                  include_retired: bool = False, limit: int = 200) -> dict:
        depth = max(1, min(depth, 3))
        seen = {entity_id}
        frontier = [entity_id]
        edges_out: list[dict] = []
        for _ in range(depth):
            if not frontier:
                break
            marks = ", ".join("?" * len(frontier))
            sql = (f"SELECT e.*, s.name AS src_name, s.type AS src_type, d.name AS dst_name, "
                   f"d.type AS dst_type FROM edges e JOIN entities s ON s.entity_id = e.src "
                   f"JOIN entities d ON d.entity_id = e.dst "
                   f"WHERE (e.src IN ({marks}) OR e.dst IN ({marks}))")
            params: list = frontier + frontier
            if not include_retired:
                sql += " AND e.valid_to IS NULL"
            if relation:
                sql += " AND e.relation = ?"
                params.append(relation)
            sql += " LIMIT ?"
            params.append(limit)
            nxt = []
            for r in self.db.conn.execute(sql, params):
                edges_out.append({k: r[k] for k in ("relation", "src_name", "src_type", "dst_name",
                                                    "dst_type", "confidence", "evidence_chunk_id",
                                                    "valid_from", "valid_to")})
                for other in (r["src"], r["dst"]):
                    if other not in seen:
                        seen.add(other)
                        nxt.append(other)
            frontier = nxt
        # Deduplicate edges seen from both ends.
        uniq = {(e["relation"], e["src_name"], e["dst_name"], e["evidence_chunk_id"]): e
                for e in edges_out}
        return {"entity_id": entity_id, "edges": list(uniq.values())[:limit]}

    def stats(self) -> dict:
        one = lambda q: self.db.conn.execute(q).fetchone()[0]  # noqa: E731
        return {
            "entities": one("SELECT COUNT(*) FROM entities"),
            "edges_current": one("SELECT COUNT(*) FROM edges WHERE valid_to IS NULL"),
            "edges_retired": one("SELECT COUNT(*) FROM edges WHERE valid_to IS NOT NULL"),
            "by_relation": dict(self.db.conn.execute(
                "SELECT relation, COUNT(*) FROM edges WHERE valid_to IS NULL GROUP BY relation").fetchall()),
        }


def _module_name(rel_path: str) -> str:
    p = PurePosixPath(rel_path.replace("\\", "/"))
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(x for x in parts if x not in ("", "/")) or p.stem
