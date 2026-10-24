"""Sync Perplexity threads and Spaces through a logged-in profile.

The thread list is collected from the library page's own GraphQL responses
while scrolling (the queries are persisted, so we do not replay them).
Thread content comes from GET /rest/thread/<slug>, fetched from inside the
page so cookies apply; thread pages are never opened, because opening one
marks it as viewed.

Per entry we keep the question (`query_str`) and the final answer (Markdown
in the FINAL step's `answer` JSON) plus up to 10 cited sources. Intermediate
agent steps are dropped.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from context_memory.browser.claude_web import NotLoggedIn, evaluate_fetch
from context_memory.browser.session import browser_context
from context_memory.ingestion.claude_code import _escape_headings
from context_memory.memory.service import IngressRejected, MemoryService
from context_memory.util import slugify, stable_id

MAX_SOURCES = 10


def _get(page, url: str):
    res = evaluate_fetch(page, url)
    if res["status"] in (401, 403):
        raise NotLoggedIn(f"perplexity returned {res['status']} for {url}; run `cmh browser login`")
    if res["status"] != 200:
        raise RuntimeError(f"perplexity returned {res['status']} for {url}")
    return res["body"]


def _thread_objects(obj, out: dict) -> None:
    """Collect every {slug, ...} thread object anywhere in a GraphQL payload."""
    if isinstance(obj, dict):
        if obj.get("__typename") and obj.get("slug") and "updatedAt" in obj:
            out[obj["slug"]] = obj
        for v in obj.values():
            _thread_objects(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _thread_objects(v, out)


def final_answer(entry: dict) -> tuple[str, list[dict]]:
    """(answer markdown, web_results) from an entry's step list."""
    try:
        steps = json.loads(entry.get("text") or "[]")
    except (json.JSONDecodeError, TypeError):
        return "", []
    for step in reversed(steps if isinstance(steps, list) else []):
        if step.get("step_type") != "FINAL":
            continue
        content = step.get("content") or {}
        raw = content.get("answer")
        try:
            ans = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            return raw or "", []
        if isinstance(ans, dict):
            return (ans.get("answer") or "").strip(), ans.get("web_results") or []
        return str(ans), []
    return "", []


def render_thread(title: str | None, entries: list[dict]) -> tuple[str, int]:
    out = [f"# {title or 'Perplexity thread'}", ""]
    turns = 0
    for e in entries:
        q = (e.get("query_str") or "").strip()
        answer, sources = final_answer(e)
        if not q and not answer:
            continue
        turns += 1
        ts = (e.get("updated_datetime") or "")[:19]
        mid = str(e.get("uuid") or "")[:8]
        out += [f"## Turn {turns}" + (f" · {ts}Z" if ts else "") + (f" · entry {mid}" if mid else ""), ""]
        if q:
            out += ["**User:**", _escape_headings(q), ""]
        if answer:
            out += ["**Perplexity:**", _escape_headings(answer), ""]
        cited = [s for s in sources if isinstance(s, dict) and s.get("url")][:MAX_SOURCES]
        if cited:
            out += ["_Sources:_"] + [f"- {s.get('name') or s['url']}: {s['url']}" for s in cited] + [""]
    return "\n".join(out), turns


def _collect_threads(page, max_threads: int, max_scrolls: int = 200) -> dict[str, dict]:
    threads: dict[str, dict] = {}

    def on_response(resp):
        if resp.url.split("?")[0].endswith("/rest/perplexity_ask/graphql"):
            try:
                _thread_objects(resp.json(), threads)
            except Exception:
                pass

    page.on("response", on_response)
    page.goto("https://www.perplexity.ai/library", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    stale = 0
    for _ in range(max_scrolls):
        if len(threads) >= max_threads:
            break
        before = len(threads)
        page.mouse.move(640, 500)
        page.mouse.wheel(0, 4000)
        page.keyboard.press("End")
        page.wait_for_timeout(1500)
        stale = stale + 1 if len(threads) == before else 0
        if stale >= 4:
            break
    page.remove_listener("response", on_response)
    return threads


def sync(svc: MemoryService, home: Path, account: str, max_threads: int = 1000,
         delay_s: float = 0.75, actor: str = "daemon") -> list[dict]:
    results: list[dict] = []
    with browser_context(home, account, headless=True, provider="perplexity") as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        threads = _collect_threads(page, max_threads)
        if not threads:
            _get(page, "/api/auth/session")  # raises NotLoggedIn on 401/403
            raise RuntimeError("no threads found on the library page; the page layout may have changed")

        spaces = _sync_spaces(svc, page, account, actor, results)
        for slug, obj in threads.items():
            doc_id = stable_id("d", "perplexity", slug)
            existing = svc.sqlite.get_document(doc_id)
            if existing and existing["meta"].get("updated_at") == obj.get("updatedAt"):
                results.append({"thread": slug, "status": "unchanged"})
                continue
            time.sleep(delay_s)
            body = _get(page, f"/rest/thread/{slug}") or {}
            text, turns = render_thread(obj.get("name"), body.get("entries") or [])
            if turns == 0:
                results.append({"thread": slug, "status": "empty"})
                continue
            space = obj.get("space") or {}
            project = spaces.get(space.get("uuid") or space.get("id"), "perplexity") \
                if isinstance(space, dict) else "perplexity"
            try:
                r = svc.ingest_text(
                    text=text, project=project, source="perplexity", kind="chat",
                    path=f"perplexity://{account}/search/{slug}", title=obj.get("name"),
                    doc_id=doc_id, tags=["perplexity", f"account:{account}"],
                    meta={"platform": "perplexity", "account": account, "thread_slug": slug,
                          "updated_at": obj.get("updatedAt"), "mode": obj.get("mode"),
                          "turns": turns, "truncated": bool(body.get("has_next_page")),
                          "via": "browser-sync",
                          "approval": "owner-approved: all Perplexity threads (2026-10-24)"},
                    on_secret="redact", actor=actor,
                )
                results.append({"thread": slug, **r})
            except (IngressRejected, ValueError) as e:
                results.append({"thread": slug, "status": "error", "error": str(e)})
    return results


def _sync_spaces(svc, page, account: str, actor: str, results: list[dict]) -> dict[str, str]:
    slugs: dict[str, str] = {}
    try:
        spaces = _get(page, "/rest/collections/list_user_collections") or []
    except RuntimeError:
        return slugs
    for s in spaces if isinstance(spaces, list) else []:
        uuid, title = s.get("uuid"), s.get("title")
        slugs[uuid] = slugify(title, "perplexity-space")
        parts = [f"# Perplexity Space: {title}", ""]
        if s.get("description"):
            parts += ["## Description", "", _escape_headings(s["description"]), ""]
        if s.get("instructions"):
            parts += ["## Space instructions", "", _escape_headings(s["instructions"]), ""]
        if len(parts) <= 2:
            continue
        try:
            r = svc.ingest_text(
                text="\n".join(parts), project=slugs[uuid], source="perplexity", kind="note",
                path=f"perplexity://{account}/space/{uuid}", title=f"Perplexity Space: {title}",
                doc_id=stable_id("d", "perplexity-space", uuid),
                tags=["perplexity", "perplexity-space", f"account:{account}"],
                meta={"platform": "perplexity", "account": account, "space_id": uuid,
                      "updated_at": s.get("updated_datetime"), "via": "browser-sync"},
                on_secret="redact", actor=actor,
            )
            results.append({"space": uuid, **r})
        except (IngressRejected, ValueError) as e:
            results.append({"space": uuid, "status": "error", "error": str(e)})
    return slugs
