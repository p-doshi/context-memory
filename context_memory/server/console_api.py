"""Data behind the owner's web console (served by the hub, `admin` scope).

Everything here is read-only and returns owner-level views (recipient
"owner"): the console runs on the hub machine for its owner only.
"""

from __future__ import annotations

import json
import re
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
    machines: dict[str, dict] = {}
    for path, updated in q("SELECT path, updated_at FROM documents WHERE kind = 'chat'"):
        m = re.search(r"/inbox/remote/([^/]+)/", path or "")
        name = (m.group(1) if m else "claude.ai" if path.startswith("claude.ai://")
                else "perplexity" if "perplexity" in path else "this laptop")
        e = machines.setdefault(name, {"machine": name, "sessions": 0, "updated": None})
        e["sessions"] += 1
        e["updated"] = max(filter(None, (e["updated"], updated)), default=None)
    return {
        "stats": {**svc.sqlite.stats(), "vectors": svc.lance.count(), **svc.graph.stats()},
        "projects": projects,
        "machines": sorted(machines.values(), key=lambda e: e["updated"] or "", reverse=True),
        "sources": sources,
        "devices": devices,
        "daemon": {k: heartbeat.get(k) for k in ("status", "elapsed_s", "gpu", "tunnels")}
                  | {"seconds_since_pass": round(time.time() - heartbeat["last_pass_end"])
                     if heartbeat.get("last_pass_end") else None},
        "backup": latest and {k: latest.get(k) for k in ("created", "ok", "chunks", "bytes", "audit_chain")},
        "audit_chain": svc.sqlite.verify_audit_chain(),
    }


def public_status(home: Path, n: int = 100) -> dict:
    """The public site as the owner sees it: processes, address, export, model
    switch, and the moderation log (flagged visitor attempts, newest first)."""
    from context_memory.public_export import current_store
    from context_memory.public_guard import read_log
    hb_file = home / "run" / "heartbeat.json"
    heartbeat = json.loads(hb_file.read_text()) if hb_file.exists() else {}
    marker = home / "public" / "published-api"
    store = current_store(home / "public")
    return {
        "children": heartbeat.get("children") or {},
        "address": marker.read_text().split()[0] if marker.exists() else None,
        "export": store.name.removeprefix("store-") if store else None,
        "model_enabled": not (home / "public" / "llm-disabled").exists(),
        "guard_config": (home / "config" / "public_guard.toml").exists(),
        "moderation": list(reversed(read_log(home, n))),
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


# -- time-aware views (console v2) -------------------------------------------------
#
# "When it happened" is the content's own date where the source records one
# (session start, conversation creation, thread update); files have none, so they
# use the date they were first indexed.

GROUPS = ["This laptop", "Other machines", "claude.ai", "Perplexity", "Files & notes"]


def _when(meta: dict, created_at: str) -> str:
    for k in ("first_timestamp", "created_at", "updated_at"):
        v = meta.get(k)
        if isinstance(v, str) and len(v) >= 10:
            return v[:19]
    return (created_at or "")[:19]


def _group(kind: str, path: str, meta: dict) -> str:
    platform = meta.get("platform")
    if platform == "claude-code":
        return "Other machines" if "/inbox/remote/" in (path or "") else "This laptop"
    if platform == "claude.ai":
        return "claude.ai"
    if platform == "perplexity":
        return "Perplexity"
    return "Files & notes"


def _docs(svc) -> list[dict]:
    """Every document with a current passage: id, project, title, kind, group, when."""
    out = []
    for doc_id, project, kind, path, title, meta, created in svc.sqlite.conn.execute(
            "SELECT d.doc_id, d.project, d.kind, d.path, d.title, d.meta, d.created_at FROM documents d "
            "WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = d.doc_id AND c.valid_to IS NULL)"):
        m = json.loads(meta or "{}")
        out.append({"id": doc_id, "project": project, "kind": kind, "path": path, "title": title or path,
                    "group": _group(kind, path, m), "when": _when(m, created)})
    return out


def _bucket(days: int) -> str:
    return "day" if 0 < days <= 45 else "week" if 0 < days <= 400 else "month"


def _bucket_key(ts: str, bucket: str) -> str:
    from datetime import date, timedelta
    d = date.fromisoformat(ts[:10])
    if bucket == "week":
        d = d - timedelta(days=d.weekday())  # Monday
    elif bucket == "month":
        d = d.replace(day=1)
    return d.isoformat()


def _since(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return "" if days <= 0 else (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def summary(svc, home: Path, days: int = 30) -> dict:
    """Headline numbers with their meaning and change over the period."""
    q = svc.sqlite.conn.execute
    docs = _docs(svc)
    since = _since(days)
    prev = _since(days * 2) if days > 0 else ""
    in_period = [d for d in docs if d["when"] >= since]
    before = [d for d in docs if prev <= d["when"] < since] if days > 0 else []
    bucket = _bucket(days if days > 0 else 10_000)
    spark: dict[str, int] = {}
    for d in in_period:
        if d["group"] == "Files & notes":
            continue  # files have no date of their own; the trend follows sessions and chats
        k = _bucket_key(d["when"], bucket)
        spark[k] = spark.get(k, 0) + 1
    hb_file = home / "run" / "heartbeat.json"
    heartbeat = json.loads(hb_file.read_text()) if hb_file.exists() else {}
    retired = q("SELECT COUNT(*) FROM chunks WHERE valid_to IS NOT NULL" + (" AND valid_to >= ?" if since else ""),
                (since,) if since else ()).fetchone()[0]
    return {
        "days": days,
        "documents": len(docs), "documents_in_period": len(in_period), "documents_previous": len(before),
        "passages": q("SELECT COUNT(*) FROM chunks WHERE valid_to IS NULL").fetchone()[0],
        "connections": q("SELECT COUNT(*) FROM edges WHERE valid_to IS NULL").fetchone()[0],
        "retired_in_period": retired,
        "projects": len({d["project"] for d in docs}),
        "sources": {g: sum(1 for d in docs if d["group"] == g) for g in GROUPS},
        "spark": [{"t": k, "n": spark[k]} for k in sorted(spark)],
        "bucket": bucket,
        "last_pass_seconds": round(time.time() - heartbeat["last_pass_end"]) if heartbeat.get("last_pass_end") else None,
        "oldest": min((d["when"] for d in docs), default=None),
    }


def timeline(svc, days: int = 90) -> dict:
    """Documents per time bucket and source group, by when they happened; plus
    passages retired (replaced by a newer version) per bucket."""
    q = svc.sqlite.conn.execute
    since = _since(days)
    bucket = _bucket(days if days > 0 else 10_000)
    rows: dict[str, dict[str, int]] = {}
    for d in _docs(svc):
        if d["when"] >= since:
            k = _bucket_key(d["when"], bucket)
            rows.setdefault(k, {g: 0 for g in GROUPS})[d["group"]] += 1
    retired: dict[str, int] = {}
    for (ts,) in q("SELECT valid_to FROM chunks WHERE valid_to IS NOT NULL" + (" AND valid_to >= ?" if since else ""),
                   (since,) if since else ()):
        k = _bucket_key(ts, bucket)
        retired[k] = retired.get(k, 0) + 1
    keys = sorted(set(rows) | set(retired))
    return {"bucket": bucket, "groups": GROUPS,
            "rows": [{"t": k, **rows.get(k, {g: 0 for g in GROUPS}), "retired": retired.get(k, 0)} for k in keys]}


def projects_breakdown(svc) -> list[dict]:
    """Per project: documents by source group, first and last activity."""
    by: dict[str, dict] = {}
    for d in _docs(svc):
        p = by.setdefault(d["project"], {"project": d["project"], **{g: 0 for g in GROUPS},
                                         "total": 0, "first": d["when"], "last": d["when"]})
        p[d["group"]] += 1
        p["total"] += 1
        p["first"], p["last"] = min(p["first"], d["when"]), max(p["last"], d["when"])
    return sorted(by.values(), key=lambda p: p["last"], reverse=True)


def project_map(svc, project: str, max_docs: int = 80, max_refs: int = 60) -> dict:
    """A project's knowledge graph with a date on every node, for a time slider:
    project -> documents (sessions, chats, files) -> code modules they define, and
    the repository files that sessions mention. Large projects are sampled evenly
    across time so early history stays visible."""
    q = svc.sqlite.conn.execute
    docs = sorted((d for d in _docs(svc) if d["project"] == project), key=lambda d: d["when"])
    total = len(docs)
    if total > max_docs:
        step = total / max_docs
        docs = [docs[int(i * step)] for i in range(max_docs)]
    nodes: dict[str, dict] = {f"p:{project}": {"id": f"p:{project}", "label": project, "type": "project",
                                               "when": docs[0]["when"] if docs else None}}
    edges: list[dict] = []
    by_path = {d["path"]: d for d in docs}
    ent = {}
    if by_path:
        marks = ", ".join("?" * len(by_path))
        ent = {name: eid for eid, name in q(
            f"SELECT entity_id, name FROM entities WHERE type = 'document' AND name IN ({marks})", list(by_path))}
    for path, eid in ent.items():
        d = by_path[path]
        kind = "session" if d["group"] in ("This laptop", "Other machines") else \
               "chat" if d["kind"] == "chat" else "file"
        did = f"d:{d['id']}"
        nodes[did] = {"id": did, "label": d["title"][:60], "type": kind, "when": d["when"], "group": d["group"],
                      "detail": path}
        edges.append({"source": did, "target": f"p:{project}", "relation": "part of"})
    if ent:
        marks = ", ".join("?" * len(ent))
        rev = {eid: path for path, eid in ent.items()}
        refs: dict[str, list] = {}
        for src, rel, dst_id, dst_name, dst_type in q(
                f"SELECT e.src, e.relation, e.dst, x.name, x.type FROM edges e JOIN entities x ON x.entity_id = e.dst "
                f"WHERE e.valid_to IS NULL AND e.relation IN ('defines', 'mentions') AND e.src IN ({marks})",
                list(ent.values())):
            d = by_path[rev[src]]
            if rel == "defines" and dst_type == "module":
                mid = f"m:{dst_name}"
                n = nodes.setdefault(mid, {"id": mid, "label": dst_name.split(".")[-1], "type": "module",
                                           "when": d["when"], "detail": dst_name, "eid": dst_id})
                n["when"] = min(n["when"], d["when"])
                edges.append({"source": f"d:{d['id']}", "target": mid, "relation": "defines"})
            elif rel == "mentions":
                refs.setdefault(dst_name, []).append(d)
        # The repository files mentioned most often (by distinct documents).
        for name, ds in sorted(refs.items(), key=lambda kv: -len({x["id"] for x in kv[1]}))[:max_refs]:
            rid = f"f:{name}"
            nodes[rid] = {"id": rid, "label": name.split("/")[-1], "type": "mentioned file",
                          "when": min(x["when"] for x in ds), "detail": name}
            for x in {x["id"]: x for x in ds}.values():
                edges.append({"source": f"d:{x['id']}", "target": rid, "relation": "mentions"})
        # Imports between modules shown.
        mods = {n["eid"]: nid for nid, n in nodes.items() if n["type"] == "module"}
        if mods:
            mm = ", ".join("?" * len(mods))
            for src, dst in q(f"SELECT src, dst FROM edges WHERE valid_to IS NULL AND relation = 'imports' "
                              f"AND src IN ({mm}) AND dst IN ({mm})", list(mods) * 2):
                if src != dst:
                    edges.append({"source": mods[src], "target": mods[dst], "relation": "imports"})
    for n in nodes.values():
        n.pop("eid", None)
    whens = [n["when"] for n in nodes.values() if n.get("when")]
    return {"project": project, "documents_total": total, "documents_shown": len(docs),
            "nodes": list(nodes.values()), "edges": edges,
            "range": [min(whens), max(whens)] if whens else None}


def activity(svc, n: int = 60) -> list[dict]:
    """Recent audit events in plain words (newest first)."""
    words = {"ingest": "indexed", "retire": "retired", "rename_project": "renamed project",
             "public_export": "published the public copy", "device_add": "added device", "device_pair": "paired device",
             "device_revoke": "revoked device", "device_rotate": "new token for device", "reconcile": "repaired the stores",
             "graph_rebuild": "rebuilt the knowledge graph", "snapshot": "backed up", "public_agent": "visitor question"}
    out = []
    for r in svc.sqlite.audit_tail(min(n * 4, 2000)):
        if r["action"] in ("search", "get", "context", "public_search") or r["outcome"] in ("unchanged", "skipped"):
            continue  # reads, not changes
        out.append({"ts": r["ts"], "what": words.get(r["action"], r["action"].replace("_", " ")),
                    "target": (r["target"] or "")[-90:], "outcome": r["outcome"], "actor": r["actor"]})
        if len(out) >= n:
            break
    return out
