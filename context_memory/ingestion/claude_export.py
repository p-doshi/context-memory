"""claude.ai data export (covers Claude web and desktop: one account history).

Input: the export .zip, an extracted folder, or conversations.json. Each
conversation becomes one document in the same store as every other source,
tagged with the account label and platform "claude.ai".

The parser is tolerant: it reads the fields the export is known to use
(`uuid`, `name`, `chat_messages[].sender/text/content/created_at`) and skips
anything it does not recognise. It was written against synthetic fixtures;
confirm against a real export before relying on it.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from context_memory.memory.service import IngressRejected, MemoryService
from context_memory.util import stable_id

from context_memory.ingestion.claude_code import _escape_headings


def _load_conversations(path: Path) -> list[dict]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            name = next((n for n in zf.namelist() if n.endswith("conversations.json")), None)
            if name is None:
                raise IngressRejected(f"{path.name}: no conversations.json in archive")
            return json.loads(zf.read(name).decode("utf-8"))
    if path.is_dir():
        path = path / "conversations.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _message_text(msg: dict) -> str:
    parts = []
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    if not parts and msg.get("text"):
        parts.append(msg["text"])
    return "\n\n".join(parts).strip()


def render_conversation(conv: dict) -> tuple[str, int]:
    title = conv.get("name") or f"Claude conversation {str(conv.get('uuid', ''))[:8]}"
    out = [f"# {title}", ""]
    turn = 0
    for msg in conv.get("chat_messages") or []:
        text = _message_text(msg)
        if not text:
            continue
        sender = msg.get("sender")
        if sender == "human":
            turn += 1
            ts = (msg.get("created_at") or "")[:19]
            mid = str(msg.get("uuid") or "")[:8]
            out += [f"## Turn {turn}" + (f" · {ts}Z" if ts else "") + (f" · msg {mid}" if mid else ""), ""]
            out += ["**User:**", _escape_headings(text), ""]
        elif sender == "assistant":
            if turn == 0:
                turn = 1
                out += ["## Turn 1", ""]
            out += ["**Assistant:**", _escape_headings(text), ""]
    return "\n".join(out), turn


def import_export(svc: MemoryService, path: Path, account: str, project: str = "claude-web",
                  actor: str = "daemon") -> list[dict]:
    results = []
    for conv in _load_conversations(Path(path)):
        uuid = conv.get("uuid")
        if not uuid:
            continue
        text, turns = render_conversation(conv)
        if turns == 0:
            results.append({"conversation": uuid, "status": "empty"})
            continue
        try:
            r = svc.ingest_text(
                text=text, project=project, source="claude", kind="chat",
                path=f"claude.ai://{account}/chat/{uuid}", title=conv.get("name"),
                doc_id=stable_id("d", "claude.ai", uuid),
                tags=["claude.ai", f"account:{account}"],
                meta={"platform": "claude.ai", "account": account, "conversation_id": uuid,
                      "created_at": conv.get("created_at"), "updated_at": conv.get("updated_at"),
                      "turns": turns, "export_file": Path(path).name,
                      "approval": "owner-approved: all Claude web/desktop chats (2026-10-24)"},
                on_secret="redact", actor=actor,
            )
            results.append({"conversation": uuid, **r})
        except (IngressRejected, ValueError) as e:
            results.append({"conversation": uuid, "status": "error", "error": str(e)})
    return results
