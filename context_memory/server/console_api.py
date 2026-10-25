"""Data behind the owner's web console (served by the hub, `admin` scope).

Everything here is read-only and returns owner-level views (recipient
"owner"): the console runs on the hub machine for its owner only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def overview(svc, home: Path) -> dict:
    q = svc.sqlite.conn.execute
    projects = [dict(project=r[0], documents=r[1], chats=r[2], code=r[3], updated=r[4]) for r in q(
        "SELECT project, COUNT(*), SUM(kind = 'chat'), SUM(kind = 'code'), MAX(updated_at) "
        "FROM documents GROUP BY project ORDER BY MAX(updated_at) DESC")]
    sources = dict(q("SELECT COALESCE(json_extract(meta, '$.platform'), source), COUNT(*) "
                     "FROM documents GROUP BY 1").fetchall())
    hb_file = home / "run" / "heartbeat.json"
    heartbeat = json.loads(hb_file.read_text()) if hb_file.exists() else {}
    snaps = sorted((home / "backup").glob("snapshot-*/manifest.json")) if (home / "backup").exists() else []
    latest = json.loads(snaps[-1].read_text()) if snaps else None
    devices = [dict(r) for r in q("SELECT name, account, scopes, last_seen, revoked_at FROM devices "
                                  "ORDER BY revoked_at IS NOT NULL, name")]
    return {
        "stats": {**svc.sqlite.stats(), "vectors": svc.lance.count(), **svc.graph.stats()},
        "projects": projects,
        "sources": sources,
        "devices": devices,
        "daemon": {k: heartbeat.get(k) for k in ("status", "elapsed_s", "gpu", "tunnels")}
                  | {"seconds_since_pass": round(time.time() - heartbeat["last_pass_end"])
                     if heartbeat.get("last_pass_end") else None},
        "backup": latest and {k: latest.get(k) for k in ("created", "ok", "chunks", "bytes", "audit_chain")},
        "audit_chain": svc.sqlite.verify_audit_chain(),
    }


def project_graph(svc) -> dict:
    """Projection of the document graph onto projects: each project node links
    to the accounts and platforms its documents came from, and projects link to
    each other when their documents mention the same repository file."""
    q = svc.sqlite.conn.execute
    nodes, edges = {}, []

    def node(nid, label, kind, weight=1):
        nodes.setdefault(nid, {"id": nid, "label": label, "kind": kind, "weight": 0})
        nodes[nid]["weight"] += weight

    for project, platform, account, n in q(
            "SELECT project, COALESCE(json_extract(meta, '$.platform'), source), "
            "json_extract(meta, '$.account'), COUNT(*) FROM documents GROUP BY 1, 2, 3"):
        node(f"p:{project}", project, "project", n)
        node(f"s:{platform}", platform, "platform")
        edges.append({"source": f"p:{project}", "target": f"s:{platform}", "weight": n})
        if account:
            node(f"a:{account}", account, "account")
            edges.append({"source": f"p:{project}", "target": f"a:{account}", "weight": n})
    # Two documents (entities with a project) mentioning the same file_ref.
    shared = q("""
        SELECT da.project, db.project, COUNT(DISTINCT x.dst) FROM edges x
        JOIN edges y ON y.dst = x.dst AND y.relation = 'mentions' AND y.valid_to IS NULL
        JOIN entities da ON da.entity_id = x.src
        JOIN entities db ON db.entity_id = y.src
        WHERE x.relation = 'mentions' AND x.valid_to IS NULL AND da.project < db.project
        GROUP BY 1, 2""").fetchall()
    for a, b, n in shared:
        edges.append({"source": f"p:{a}", "target": f"p:{b}", "weight": n, "kind": "shared-files"})
    # Collapse duplicate project-platform/account pairs from the GROUP BY above.
    merged: dict[tuple, dict] = {}
    for e in edges:
        key = (e["source"], e["target"], e.get("kind"))
        merged.setdefault(key, {**e, "weight": 0})["weight"] += e["weight"]
    return {"nodes": list(nodes.values()), "edges": list(merged.values())}


def entity_graph(svc, name: str, depth: int = 1, limit: int = 150) -> dict:
    hits = svc.graph.find(name, limit=1)
    if not hits:
        return {"nodes": [], "edges": [], "center": None}
    res = svc.graph.neighbors(hits[0]["entity_id"], depth=depth, limit=limit)
    nodes, edges = {}, []
    for e in res["edges"]:
        for side in ("src", "dst"):
            nid = f"{e[side + '_type']}:{e[side + '_name']}"
            nodes.setdefault(nid, {"id": nid, "label": e[side + "_name"],
                                   "full": e[side + "_name"], "kind": e[side + "_type"]})
        edges.append({"source": f"{e['src_type']}:{e['src_name']}",
                      "target": f"{e['dst_type']}:{e['dst_name']}", "relation": e["relation"]})
    center = f"{hits[0]['type']}:{hits[0]['name']}"
    return {"nodes": list(nodes.values()), "edges": edges, "center": center}


def audit(svc, n: int = 200) -> list[dict]:
    return svc.sqlite.audit_tail(min(n, 1000))
