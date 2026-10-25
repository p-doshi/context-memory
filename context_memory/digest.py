"""Per-conversation digests for the owner's report: short notes under six headings
(context, input, architecture, results, findings, errors), written by the local
answer model (llama-server on loopback; nothing leaves the machine).

Each note carries a short quote. A note is kept only if that quote occurs word for
word in the conversation (markdown, punctuation and case ignored). This proves the
supporting words exist; it does not prove the one-line summary is faithful, so the
console always shows the quote and its source next to the note.

Measured on 10 conversations (seed 42, 4 Perplexity, 3 claude.ai, 3 Claude Code):
54 of 71 notes kept (0.76), about 8 s per conversation on a GTX 1660 Ti.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.request

from context_memory.util import utcnow

log = logging.getLogger("cmh.digest")

FIELDS = ["context", "input", "architecture", "results", "findings", "errors"]
MODEL = "qwen3-1.7b-q8"
PROMPT_VERSION = "1"

TABLE = """
CREATE TABLE IF NOT EXISTS digests (
  doc_id TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,          -- ok | failed
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  items TEXT NOT NULL,           -- JSON list of {field, text, quote}
  offered INTEGER NOT NULL,
  kept INTEGER NOT NULL,
  seconds REAL,
  created_at TEXT NOT NULL
);
-- Conversations the owner hid from the report (tests, noise). Reversible.
CREATE TABLE IF NOT EXISTS report_hidden (
  doc_id TEXT PRIMARY KEY,
  hidden_at TEXT NOT NULL
);
"""

_ITEM = {"type": "object", "properties": {"text": {"type": "string"}, "quote": {"type": "string"}},
         "required": ["text", "quote"]}
SCHEMA = {"type": "object", "required": FIELDS,
          "properties": {f: {"type": "array", "items": _ITEM, "maxItems": 2} for f in FIELDS}}
SYSTEM = ("You summarise one conversation for its owner. Return JSON with these keys:\n"
          "context: what the work is about (project, data, goal).\n"
          "input: what the owner asked for or provided.\n"
          "architecture: design choices, methods, pipeline or system structure decided.\n"
          "results: concrete outcomes, numbers, measurements, things that now work.\n"
          "findings: conclusions or insights reached.\n"
          "errors: problems, bugs, mistakes, failures, or open issues.\n"
          "Each item: 'text' = one short plain sentence (max 25 words); 'quote' = 3 to 12 words copied "
          "EXACTLY from the conversation that support it. Use an empty list when the conversation has nothing "
          "for a key. Do not invent anything.")
_ERR = re.compile(r"error|exception|traceback|failed|failure|bug|broken|doesn't work|not working|wrong", re.I)
_RES = re.compile(r"\d+(\.\d+)?\s*(%|ms|s\b|x\b)|accuracy|f1|recall|precision|passed|result|works now|verified", re.I)


def ensure(conn) -> None:
    conn.executescript(TABLE)


def set_hidden(conn, doc_id: str, hidden: bool = True) -> None:
    ensure(conn)
    if hidden:
        conn.execute("INSERT OR REPLACE INTO report_hidden VALUES (?, ?)", (doc_id, utcnow()))
    else:
        conn.execute("DELETE FROM report_hidden WHERE doc_id = ?", (doc_id,))
    conn.commit()


def hidden_ids(conn) -> set[str]:
    ensure(conn)
    return {r[0] for r in conn.execute("SELECT doc_id FROM report_hidden")}


def words(text: str) -> str:
    """Word-for-word form used to check quotes: lower case, letters and digits only."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def fingerprint(chunk_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(chunk_ids) + [PROMPT_VERSION]).encode()).hexdigest()[:32]


def build_input(chunks: list[str], budget: int = 7000) -> str:
    """The opening and closing passages, then passages that mention errors or
    results, in conversation order, within a character budget that fits the
    model's 3k-token context."""
    if not chunks:
        return ""
    ends = {0, len(chunks) - 1}
    extra = [i for i, t in enumerate(chunks) if i not in ends and (_ERR.search(t) or _RES.search(t))]
    chosen, used = [], 0
    for i in sorted(ends) + extra:
        piece = chunks[i][: budget // 3]
        if used + len(piece) <= budget:
            chosen.append((i, piece))
            used += len(piece)
    return "\n\n".join(p for _, p in sorted(chosen))


def ask_model(text: str, base_url: str, api_key: str | None, timeout: float = 180) -> dict:
    if not base_url.startswith(("http://127.0.0.1", "http://localhost")):
        raise ValueError("digests use the local model only")
    body = {"messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": "Conversation:\n\n" + text + "\n\n/no_think"}],
            "max_tokens": 1200, "temperature": 0.1,
            "response_format": {"type": "json_schema", "json_schema": {"schema": SCHEMA}},
            "chat_template_kwargs": {"enable_thinking": False}}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(json.load(r)["choices"][0]["message"]["content"])


def first_request(chunks: list[str]) -> str:
    """The owner's first message, if the conversation records one."""
    if not chunks or "**User:**" not in chunks[0]:
        return ""
    text = re.split(r"\*\*[A-Z][A-Za-z ]*:\*\*", chunks[0].split("**User:**", 1)[1])[0]
    text = re.sub(r"<[^>]+>[^<]*</[^>]+>", " ", text)       # IDE/system tags, not the owner's words
    return " ".join(text.split())


def verify(raw: dict, chunks: list[str]) -> tuple[list[dict], int]:
    """Keep notes whose quote is found in the conversation; drop vague or repeated
    ones. Returns (kept notes, number offered)."""
    full = words("\n".join(chunks))
    kept, seen, offered = [], set(), 0
    for field in FIELDS:
        for it in raw.get(field) or []:
            offered += 1
            text, quote = str(it.get("text", "")).strip(), str(it.get("quote", "")).strip()
            q, key = words(quote), words(text)
            if len(q.split()) < 3 or q not in full or len(text.split()) < 4 or key in seen:
                continue
            seen.add(key)
            kept.append({"field": field, "text": text[:240], "quote": quote[:200]})
    if not any(k["field"] == "input" for k in kept):
        ask = first_request(chunks)
        if len(ask.split()) >= 4:
            kept.insert(0, {"field": "input", "text": ask[:240], "quote": " ".join(ask.split()[:12])})
    return kept, offered


def pending(conn, limit: int = 50) -> list[tuple[str, str]]:
    """(doc_id, fingerprint) of conversations with no digest for their current
    passages, newest first. A conversation that failed twice on the same passages
    is skipped until it changes."""
    ensure(conn)
    rows = conn.execute(
        "SELECT d.doc_id, group_concat(c.chunk_id, '\n'), coalesce(json_extract(d.meta, '$.last_timestamp'), "
        "json_extract(d.meta, '$.updated_at'), d.updated_at) AS t FROM documents d "
        "JOIN chunks c ON c.doc_id = d.doc_id AND c.valid_to IS NULL WHERE d.kind = 'chat' "
        "AND json_extract(d.meta, '$.parent_session') IS NULL "  # subagents: Claude briefing Claude
        "GROUP BY d.doc_id ORDER BY t DESC").fetchall()
    done = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT doc_id, fingerprint, status FROM digests")}
    failures = {r[0]: r[1] for r in conn.execute(
        "SELECT doc_id, offered FROM digests WHERE status = 'failed'")}  # offered = failure count
    out = []
    for doc_id, ids, _ in rows:
        fp = fingerprint(ids.split("\n"))
        prev = done.get(doc_id)
        if prev and prev[0] == fp and (prev[1] == "ok" or failures.get(doc_id, 0) >= 2):
            continue
        out.append((doc_id, fp))
        if len(out) >= limit:
            break
    return out


def digest_document(conn, doc_id: str, fp: str, ask) -> dict:
    chunks = [t for (t,) in conn.execute(
        "SELECT text FROM chunks WHERE doc_id = ? AND valid_to IS NULL ORDER BY ordinal", (doc_id,))]
    t0 = time.time()
    try:
        raw = ask(build_input(chunks))
    except OSError:
        # The model is unreachable or timed out (restart, GPU busy): not this
        # conversation's fault, so nothing is recorded; the caller stops the run.
        raise
    except Exception as e:  # the model answered, but not with usable JSON
        prev = conn.execute("SELECT fingerprint, offered FROM digests WHERE doc_id = ? AND status = 'failed'",
                            (doc_id,)).fetchone()
        count = (prev[1] + 1) if prev and prev[0] == fp else 1
        conn.execute("INSERT OR REPLACE INTO digests VALUES (?, ?, 'failed', ?, ?, '[]', ?, 0, ?, ?)",
                     (doc_id, fp, MODEL, PROMPT_VERSION, count, time.time() - t0, utcnow()))
        conn.commit()
        return {"status": "failed", "error": repr(e)[:200]}
    items, offered = verify(raw, chunks)
    secs = time.time() - t0
    conn.execute("INSERT OR REPLACE INTO digests VALUES (?, ?, 'ok', ?, ?, ?, ?, ?, ?, ?)",
                 (doc_id, fp, MODEL, PROMPT_VERSION, json.dumps(items), offered, len(items), secs, utcnow()))
    conn.commit()
    return {"status": "ok", "offered": offered, "kept": len(items), "seconds": round(secs, 1)}


def run(conn, base_url: str, api_key: str | None, budget_seconds: float = 150, limit: int = 40) -> dict:
    """Digest pending conversations until the time budget is spent."""
    todo = pending(conn, limit)
    out = {"done": 0, "failed": 0, "offered": 0, "kept": 0}
    t0 = time.time()
    for doc_id, fp in todo:
        if time.time() - t0 > budget_seconds:
            break
        try:
            r = digest_document(conn, doc_id, fp, lambda text: ask_model(text, base_url, api_key))
        except OSError as e:
            out["stopped"] = f"model unavailable: {e!r}"[:200]
            break
        if r["status"] == "ok":
            out["done"] += 1
            out["offered"] += r["offered"]
            out["kept"] += r["kept"]
        else:
            out["failed"] += 1
            log.warning("digest %s failed: %s", doc_id, r.get("error"))
    out["pending"] = max(0, len(pending(conn, 10_000)))
    return out
