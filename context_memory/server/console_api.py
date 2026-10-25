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


def _untitled(svc, doc_id: str, path: str) -> str:
    """A label for a document without a title (e.g. a session whose tool recorded
    none): the first line of its first user message, else of its first passage."""
    row = svc.sqlite.conn.execute("SELECT text FROM chunks WHERE doc_id = ? AND valid_to IS NULL "
                                  "ORDER BY ordinal LIMIT 1", (doc_id,)).fetchone()
    text = row[0] if row else ""
    if "**User:**" in text:
        text = text.split("**User:**", 1)[1]
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:100] + ("…" if len(line) > 100 else "")
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _docs(svc) -> list[dict]:
    """Every document with a current passage: id, project, title, kind, group, when."""
    out = []
    for doc_id, project, kind, path, title, meta, created in svc.sqlite.conn.execute(
            "SELECT d.doc_id, d.project, d.kind, d.path, d.title, d.meta, d.created_at FROM documents d "
            "WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = d.doc_id AND c.valid_to IS NULL)"):
        m = json.loads(meta or "{}")
        if not title or title == path or title.endswith(".jsonl"):
            title = _untitled(svc, doc_id, path or "")
        out.append({"id": doc_id, "project": project, "kind": kind, "path": path, "title": title,
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
    retired_top = q("SELECT d.project, COUNT(*) FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
                    "WHERE c.valid_to IS NOT NULL" + (" AND c.valid_to >= ?" if since else "") +
                    " GROUP BY d.project ORDER BY 2 DESC LIMIT 1", (since,) if since else ()).fetchone()
    worked = [d for d in in_period if d["group"] != "Files & notes"]
    worked_before = [d for d in before if d["group"] != "Files & notes"]
    per_project: dict[str, int] = {}
    for d in worked:
        per_project[d["project"]] = per_project.get(d["project"], 0) + 1
    doc_links = q("SELECT COUNT(*) FROM edges e JOIN entities s ON s.entity_id = e.src WHERE e.valid_to IS NULL "
                  "AND s.type = 'document' AND e.relation IN ('mentions', 'defines')").fetchone()[0]
    return {
        "worked_in_period": len(worked), "worked_previous": len(worked_before),
        "projects_in_period": len(per_project),
        "top_project": max(per_project, key=per_project.get) if per_project else None,
        "top_project_share": round(max(per_project.values()) / len(worked), 2) if worked else 0,
        "links_from_documents": doc_links,
        "retired_top_project": retired_top[0] if retired_top else None,
        "interval_seconds": svc.settings.daemon_interval_seconds,
        "search_k": svc.settings.default_search_k,
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
        # Latest item: a session or chat when there is one (files have no date of their own).
        rank = (d["group"] != "Files & notes", d["when"])
        if rank > p.get("_rank", (False, "")):
            p.update(_rank=rank, latest_title=d["title"][:140], latest_group=d["group"], latest_when=d["when"])
    for p in by.values():
        p.pop("_rank", None)
    return sorted(by.values(), key=lambda p: p["last"], reverse=True)


def _span_buckets(first: str, last: str) -> tuple[str, list[str]]:
    """Every week (or month, past ~13 months) from first to last, oldest first."""
    from datetime import date, timedelta
    a, b = date.fromisoformat(first[:10]), date.fromisoformat(last[:10])
    bucket = "week" if (b - a).days <= 400 else "month"
    keys, k = [], date.fromisoformat(_bucket_key(first, bucket))
    while k.isoformat() <= _bucket_key(last, bucket):
        keys.append(k.isoformat())
        k = k + timedelta(days=7) if bucket == "week" else (k.replace(day=28) + timedelta(days=4)).replace(day=1)
    return bucket, keys


def project_story(svc, project: str, max_files: int = 15, max_files_per_item: int = 6) -> dict:
    """One project over time, for reading rather than exploring: activity per week
    by source, the repository files its sessions and chats worked on (which, and in
    which weeks), and a dated list of every session and chat with the files each
    touched. Files and notes have no date of their own, so they are counted only."""
    q = svc.sqlite.conn.execute
    docs = [d for d in _docs(svc) if d["project"] == project]
    dated = sorted((d for d in docs if d["group"] != "Files & notes"), key=lambda d: d["when"])
    counts = {"sessions": sum(d["group"] in ("This laptop", "Other machines") for d in dated),
              "chats": sum(d["group"] in ("claude.ai", "Perplexity") for d in dated),
              "files": sum(d["group"] == "Files & notes" for d in docs)}
    out = {"project": project, "counts": counts, "first": min((d["when"] for d in docs), default=None),
           "groups": GROUPS[:4], "bucket": "week", "columns": [], "files": [], "items": []}
    if not dated:
        return out
    bucket, cols = _span_buckets(dated[0]["when"], dated[-1]["when"])
    activity = {k: {g: 0 for g in GROUPS[:4]} for k in cols}
    for d in dated:
        d["bucket"] = _bucket_key(d["when"], bucket)
        activity[d["bucket"]][d["group"]] += 1
    # Which repository files each dated item mentions (graph "mentions" edges).
    touched: dict[str, list[str]] = {}
    by_path = {d["path"]: d for d in dated}
    paths = list(by_path)
    for i in range(0, len(paths), 500):
        part = paths[i:i + 500]
        marks = ", ".join("?" * len(part))
        for doc_path, ref in q(
                f"SELECT s.name, x.name FROM edges e JOIN entities s ON s.entity_id = e.src "
                f"JOIN entities x ON x.entity_id = e.dst WHERE e.valid_to IS NULL AND e.relation = 'mentions' "
                f"AND s.type = 'document' AND s.name IN ({marks})", part):
            touched.setdefault(by_path[doc_path]["id"], []).append(ref)
    per_file: dict[str, dict[str, int]] = {}
    for d in dated:
        for ref in set(touched.get(d["id"], [])):
            per_file.setdefault(ref, {})
            per_file[ref][d["bucket"]] = per_file[ref].get(d["bucket"], 0) + 1
    top = sorted(per_file.items(), key=lambda kv: -sum(kv[1].values()))[:max_files]
    rank = {name: i for i, (name, _) in enumerate(top)}
    out.update(bucket=bucket,
               columns=[{"t": k, **activity[k], "total": sum(activity[k].values())} for k in cols],
               files=sorted(({"path": name, "first": min(cells), "last": max(cells), "items": sum(cells.values()),
                              "cells": cells} for name, cells in top), key=lambda f: (f["first"], -f["items"])),
               items=[{"when": d["when"], "bucket": d["bucket"], "title": d["title"][:140], "group": d["group"],
                       "files": sorted(set(touched.get(d["id"], [])), key=lambda r: rank.get(r, 1e9))[:max_files_per_item]}
                      for d in reversed(dated)])
    return out


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
