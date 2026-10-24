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
from context_memory.ingestion.claude_export import render_conversation
from context_memory.memory.service import IngressRejected, MemoryService
from context_memory.util import stable_id

FETCH_JSON = """async (url) => {
  const r = await fetch(url, {credentials: 'include', headers: {'accept': 'application/json'}});
  return {status: r.status, body: r.ok ? await r.json() : null};
}"""


class NotLoggedIn(Exception):
    pass


def _get(page, url: str):
    res = page.evaluate(FETCH_JSON, url)
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
        page.goto("https://claude.ai/", wait_until="domcontentloaded")
        orgs = _get(page, "/api/organizations")
        for org in orgs or []:
            org_id = org.get("uuid")
            convs = _get(page, f"/api/organizations/{org_id}/chat_conversations") or []
            convs.sort(key=lambda c: c.get("updated_at") or "", reverse=True)
            for conv in convs[:max_conversations]:
                uuid = conv.get("uuid")
                doc_id = stable_id("d", "claude.ai", uuid)
                existing = svc.sqlite.get_document(doc_id)
                if existing and existing["meta"].get("updated_at") == conv.get("updated_at"):
                    results.append({"conversation": uuid, "status": "unchanged"})
                    continue
                time.sleep(delay_s)
                detail = _get(page, f"/api/organizations/{org_id}/chat_conversations/{uuid}"
                                    "?tree=True&rendering_mode=messages")
                text, turns = render_conversation(detail)
                if turns == 0:
                    results.append({"conversation": uuid, "status": "empty"})
                    continue
                try:
                    r = svc.ingest_text(
                        text=text, project="claude-web", source="claude", kind="chat",
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
