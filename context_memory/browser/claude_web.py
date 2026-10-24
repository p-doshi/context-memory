"""Sync claude.ai conversations (web + desktop history) through a logged-in profile.

Uses the JSON endpoints the claude.ai page itself calls, from inside the
page so the session cookies apply. Conversations share doc ids with the
official-export importer, so an export and a browser sync of the same chat
deduplicate instead of producing two documents.
"""

from __future__ import annotations

import time
from pathlib import Path

from context_memory.browser.session import browser_context
from context_memory.ingestion.claude_export import render_conversation, render_project
from context_memory.memory.service import IngressRejected, MemoryService
from context_memory.util import slugify, stable_id

FETCH_JSON = """async (url) => {
  const r = await fetch(url, {credentials: 'include', headers: {'accept': 'application/json'}});
  return {status: r.status, body: r.ok ? await r.json() : null};
}"""


class NotLoggedIn(Exception):
    pass


def evaluate_fetch(page, url: str) -> dict:
    """Run FETCH_JSON in the page, retrying once if a navigation destroyed the
    execution context."""
    from playwright.sync_api import Error as PlaywrightError
    for attempt in range(3):
        try:
            return page.evaluate(FETCH_JSON, url)
        except PlaywrightError as e:
            if "context was destroyed" not in str(e) or attempt == 2:
                raise
            page.wait_for_load_state("load")
            page.wait_for_timeout(2000)


def _get(page, url: str):
    res = evaluate_fetch(page, url)
    if res["status"] in (401, 403):
        raise NotLoggedIn(f"claude.ai returned {res['status']} for {url}; run `cmh browser login`")
    if res["status"] != 200:
        raise RuntimeError(f"claude.ai returned {res['status']} for {url}")
    return res["body"]


def sync(svc: MemoryService, home: Path, account: str, max_conversations: int = 500,
         delay_s: float = 0.75, actor: str = "daemon") -> list[dict]:
    results: list[dict] = []
    with browser_context(home, account, headless=True) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # The app shell redirects after load (e.g. to /new); let it settle so
        # in-page fetches are not cut off. API URLs cannot be opened directly
        # as documents (403).
        page.goto("https://claude.ai/", wait_until="load")
        page.wait_for_timeout(3000)
        orgs = _get(page, "/api/organizations")
        for org in orgs or []:
            org_id = org.get("uuid")
            base = f"/api/organizations/{org_id}"
            project_slugs = _sync_projects(svc, page, base, account, org, delay_s, actor, results)
            convs = _get(page, f"{base}/chat_conversations") or []
            convs.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
            consecutive_errors = 0
            for conv in convs[:max_conversations]:
                if consecutive_errors >= 5:
                    results.append({"conversation": None, "status": "stopped",
                                    "error": "5 consecutive fetch errors; possible rate limit"})
                    break
                uuid = conv.get("uuid")
                if conv.get("is_temporary"):
                    results.append({"conversation": uuid, "status": "skipped-temporary"})
                    continue
                doc_id = stable_id("d", "claude.ai", uuid)
                existing = svc.sqlite.get_document(doc_id)
                if existing and existing["meta"].get("updated_at") == conv.get("updated_at"):
                    results.append({"conversation": uuid, "status": "unchanged"})
                    continue
                time.sleep(delay_s)
                url = f"{base}/chat_conversations/{uuid}?tree=True&rendering_mode=messages&render_all_tools=true"
                try:
                    try:
                        detail = _get(page, url)
                    except (NotLoggedIn, RuntimeError):
                        # A long embedding step can leave the page's session
                        # stale (seen as a transient 403); reload and retry once.
                        page.goto("https://claude.ai/", wait_until="load")
                        page.wait_for_timeout(3000)
                        detail = _get(page, url)
                except (NotLoggedIn, RuntimeError) as e:
                    # One unreadable conversation (e.g. a shared or workspace
                    # session) must not abort the sync.
                    consecutive_errors += 1
                    results.append({"conversation": uuid, "status": "error", "error": str(e),
                                    "platform": conv.get("platform")})
                    continue
                consecutive_errors = 0
                text, turns = render_conversation(detail)
                if turns == 0:
                    results.append({"conversation": uuid, "status": "empty"})
                    continue
                project = project_slugs.get(conv.get("project_uuid"), "claude-web")
                try:
                    r = svc.ingest_text(
                        text=text, project=project, source="claude", kind="chat",
                        path=f"claude.ai://{account}/chat/{uuid}", title=detail.get("name"),
                        doc_id=doc_id, tags=["claude.ai", f"account:{account}"],
                        meta={"platform": "claude.ai", "account": account,
                              "organization": org.get("name"), "conversation_id": uuid,
                              "created_at": conv.get("created_at"),
                              "updated_at": conv.get("updated_at"), "turns": turns,
                              "via": "browser-sync",
                              "approval": "owner-approved: all Claude web/desktop chats (2026-10-24)"},
                        on_secret="redact", actor=actor,
                    )
                    results.append({"conversation": uuid, **r})
                except (IngressRejected, ValueError) as e:
                    results.append({"conversation": uuid, "status": "error", "error": str(e)})
    return results


def _sync_projects(svc, page, base: str, account: str, org: dict, delay_s: float,
                   actor: str, results: list[dict]) -> dict[str, str]:
    """Index each claude.ai project's instructions and knowledge files; return
    project_uuid -> memory project slug, so its chats land in the same project."""
    slugs: dict[str, str] = {}
    for p in _get(page, f"{base}/projects") or []:
        uuid = p.get("uuid")
        slug = slugify(p.get("name"), "claude-project")
        slugs[uuid] = slug
        if p.get("archived_at"):
            continue
        time.sleep(delay_s)
        detail = _get(page, f"{base}/projects/{uuid}") or p
        docs = _get(page, f"{base}/projects/{uuid}/docs") or []
        try:
            r = svc.ingest_text(
                text=render_project(detail, docs), project=slug, source="claude", kind="note",
                path=f"claude.ai://{account}/project/{uuid}", title=f"Claude project: {p.get('name')}",
                doc_id=stable_id("d", "claude.ai-project", uuid),
                tags=["claude.ai", "claude-project", f"account:{account}"],
                meta={"platform": "claude.ai", "account": account, "organization": org.get("name"),
                      "project_id": uuid, "updated_at": p.get("updated_at"),
                      "knowledge_files": len(docs), "via": "browser-sync"},
                on_secret="redact", actor=actor,
            )
            results.append({"project": uuid, **r})
        except (IngressRejected, ValueError) as e:
            results.append({"project": uuid, "status": "error", "error": str(e)})
    return slugs
