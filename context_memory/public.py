"""The public surface: what a visitor to the project may see.

Only projects listed under [public] in the owner's sources.toml, and only the
document kinds listed there (chats excluded by default), are ever searched.
Every string passes the Disclosure Gate as recipient "public" (contact,
precise location, financial and credential attributes withheld), and results
carry no local paths, account tags or internal ids beyond an opaque chunk id.

The visitor agent speaks the AG-UI event protocol (docs.ag-ui.com): a run is a
stream of RUN_STARTED, STEP_*, TOOL_CALL_*, TEXT_MESSAGE_* and RUN_FINISHED
events. Its default answerer is extractive (no language model): it quotes the
retrieved memory with sources, so no third party sees anything.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Iterator

from context_memory.policy.disclosure import gate

SNIPPET = 420


def _public_text(text: str) -> str:
    # Public projects are opted in as a whole, so the chunk-level "research-private"
    # block does not apply; attribute redaction still does.
    return gate(text or "", "public", chunk_tier="public").text


def _public_source(rec_path: str, title: str | None, kind: str) -> str:
    """A visitor-safe source label: repo-relative file names for code and docs,
    never an absolute local path."""
    if kind in ("code", "note") and title:
        return _public_text(title)
    return kind


def search(svc, sources, query: str, k: int = 8) -> dict:
    projects = list(sources.public_projects)
    if not projects:
        return {"hits": [], "note": "no public projects configured"}
    res = svc.memory_search(query, k=k * 2, kinds=list(sources.public_kinds), projects=projects,
                            recipient="owner", actor="public")
    hits, per_doc = [], {}
    for r in res["results"]:
        doc = r["provenance"]["doc_id"]
        if per_doc.get(doc, 0) >= 2 or r["project"] not in projects:
            continue
        per_doc[doc] = per_doc.get(doc, 0) + 1
        body = r["text"].split("\n", 1)[-1].rsplit("\n", 1)[0]
        hits.append({
            "id": r["provenance"]["chunk_id"],
            "project": r["project"],
            "kind": r["kind"],
            "title": _public_text(r["title"] or ""),
            "section": _public_text(r["section_path"] or ""),
            "source": _public_source(r["provenance"]["source_path"], r["title"], r["kind"]),
            "status": r["provenance"]["verification_status"],
            "snippet": _public_text(" ".join(body.split())[:SNIPPET]),
        })
        hits[-1]["where"] = _where(hits[-1])
        if len(hits) >= k:
            break
    return {"hits": hits}


def projects(svc, sources) -> list[dict]:
    out = []
    kinds = list(sources.public_kinds)
    q = svc.sqlite.conn.execute
    # Only documents that still have a current chunk; retired files drop out.
    live = "EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = documents.doc_id AND c.valid_to IS NULL)"
    for p in sources.public_projects:
        marks = ", ".join("?" * len(kinds))
        counts = dict(q(f"SELECT kind, COUNT(*) FROM documents WHERE project = ? AND kind IN ({marks}) "
                        f"AND {live} GROUP BY kind", (p, *kinds)).fetchall())
        recent = [_public_text(t) for (t,) in q(
            f"SELECT title FROM documents WHERE project = ? AND kind IN ({marks}) AND title IS NOT NULL "
            f"AND {live} ORDER BY updated_at DESC LIMIT 8", (p, *kinds)).fetchall()]
        out.append({"project": p, "documents": counts, "recent": recent})
    return out


def architecture_graph(svc, sources, limit: int = 250) -> dict:
    """Modules defined in public projects and the imports between them; the
    visitor-facing projection of the knowledge graph."""
    q = svc.sqlite.conn.execute
    projects_ = list(sources.public_projects)
    if not projects_:
        return {"nodes": [], "edges": []}
    marks = ", ".join("?" * len(projects_))
    modules = {r[0]: r[1] for r in q(f"""
        SELECT DISTINCT m.entity_id, m.name FROM edges e
        JOIN entities d ON d.entity_id = e.src AND d.type = 'document' AND d.project IN ({marks})
        JOIN entities m ON m.entity_id = e.dst AND m.type = 'module'
        WHERE e.relation = 'defines' AND e.valid_to IS NULL""", projects_).fetchall()}
    edges = [{"source": m[a], "target": m[b]} for a, b in q(
        "SELECT src, dst FROM edges WHERE relation = 'imports' AND valid_to IS NULL").fetchall()
        for m in [modules] if a in m and b in m]
    nodes = [{"id": name, "label": name.split(".")[-1], "full": name, "kind": "module"}
             for name in sorted(set(modules.values()))][:limit]
    keep = {n["id"] for n in nodes}
    return {"nodes": nodes, "edges": [e for e in edges if e["source"] in keep and e["target"] in keep]}


# -- AG-UI visitor agent ---------------------------------------------------------

def _event(type_: str, **fields) -> dict:
    return {"type": type_, **fields}


def _where(h: dict) -> str:
    """Title › section, without the section repeating the title."""
    title, section = h["title"], h["section"]
    if title and section.startswith(title):
        section = section[len(title):].lstrip(" >›")
    return " › ".join(x for x in (title, section) if x) or h["project"]


def extractive_answer(question: str, hits: list[dict]) -> str:
    """No language model: say where the relevant material is; the excerpts
    themselves travel in the tool result and are shown as cards."""
    if not hits:
        return ("I could not find anything about that in the public part of this project's memory. "
                "Try different words, or ask about the code, design or decisions.")
    n = min(len(hits), 4)
    lines = [f"The project's public memory has {n} relevant part{'s' if n > 1 else ''}:"]
    lines += [f"{i}. {_where(h)} ({h['kind']}, {h['status']})" for i, h in enumerate(hits[:4], 1)]
    lines.append("The excerpts are shown below. They are retrieved from memory, not generated; "
                 "'verified' means the excerpt still matches its source file.")
    return "\n".join(lines)


def agent_run(svc, sources, body: dict) -> Iterator[dict]:
    """One AG-UI run: retrieve from public memory, then answer extractively."""
    thread_id = str(body.get("threadId") or uuid.uuid4())
    run_id = str(body.get("runId") or uuid.uuid4())
    messages = body.get("messages") or []
    question = next((m.get("content", "") for m in reversed(messages)
                     if isinstance(m, dict) and m.get("role") == "user"), "")
    question = question if isinstance(question, str) else json.dumps(question)
    question = re.sub(r"\s+", " ", question).strip()[:500]
    yield _event("RUN_STARTED", threadId=thread_id, runId=run_id)
    if not question:
        yield _event("RUN_ERROR", message="no user message in the request", code="bad_request")
        return
    yield _event("STEP_STARTED", stepName="retrieve")
    call_id = f"call_{uuid.uuid4().hex[:12]}"
    args = json.dumps({"query": question, "k": 6})
    yield _event("TOOL_CALL_START", toolCallId=call_id, toolCallName="public_memory_search")
    yield _event("TOOL_CALL_ARGS", toolCallId=call_id, delta=args)
    yield _event("TOOL_CALL_END", toolCallId=call_id)
    result = search(svc, sources, question, k=6)
    yield _event("TOOL_CALL_RESULT", messageId=f"msg_{uuid.uuid4().hex[:12]}", toolCallId=call_id,
                 content=json.dumps(result), role="tool")
    yield _event("STEP_FINISHED", stepName="retrieve")
    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    yield _event("TEXT_MESSAGE_START", messageId=msg_id, role="assistant")
    answer = extractive_answer(question, result["hits"])
    for i in range(0, len(answer), 200):  # stream in chunks, as a model would
        yield _event("TEXT_MESSAGE_CONTENT", messageId=msg_id, delta=answer[i:i + 200])
    yield _event("TEXT_MESSAGE_END", messageId=msg_id)
    yield _event("RUN_FINISHED", threadId=thread_id, runId=run_id,
                 result={"hits": len(result["hits"])})
