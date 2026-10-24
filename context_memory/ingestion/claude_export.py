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
import re
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


MAX_EMBEDDED_CHARS = 20_000  # per attachment / created file / memory write


def _fenced(text: str, lang: str = "") -> list[str]:
    """Fence text with a backtick run longer than any inside it."""
    text = text if len(text) <= MAX_EMBEDDED_CHARS else (
        text[:MAX_EMBEDDED_CHARS] + f"\n[... truncated, {len(text) - MAX_EMBEDDED_CHARS} chars omitted]")
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + lang, text, fence]


def active_branch(conv: dict) -> list[dict]:
    """Messages on the branch the user kept, root to leaf.

    With tree=True the API returns every edited/retried branch. Walk parent
    links back from current_leaf_message_uuid; fall back to index order when
    the conversation has no tree information (e.g. some exports).
    """
    msgs = conv.get("chat_messages") or []
    by_id = {m.get("uuid"): m for m in msgs if m.get("uuid")}
    leaf = conv.get("current_leaf_message_uuid")
    if leaf in by_id and any(m.get("parent_message_uuid") for m in msgs):
        path, seen, cur = [], set(), by_id.get(leaf)
        while cur is not None and cur.get("uuid") not in seen:
            seen.add(cur.get("uuid"))
            path.append(cur)
            cur = by_id.get(cur.get("parent_message_uuid"))
        return list(reversed(path))
    return sorted(msgs, key=lambda m: m.get("index", 0))


def _human_parts(msg: dict) -> list[str]:
    parts = [_escape_headings(_message_text(msg))] if _message_text(msg) else []
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "image":
            parts.append("_[image attached]_")
    for att in msg.get("attachments") or []:
        content = att.get("extracted_content")
        if content:
            parts += [f"_Attached file: {att.get('file_name') or 'unnamed'}_", *_fenced(content)]
    for f in msg.get("files") or []:
        if f.get("file_name"):
            parts.append(f"_[file uploaded: {f['file_name']}]_")
    return parts


def _assistant_parts(msg: dict) -> tuple[list[str], list[str]]:
    parts, other_tools = [], []
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            parts.append(_escape_headings(block["text"]))
        elif kind == "tool_use":
            name, inp = block.get("name") or "tool", block.get("input") or {}
            if not isinstance(inp, dict):
                other_tools.append(name)
            elif name == "create_file" and inp.get("file_text"):
                parts += [f"_Created file `{inp.get('path', '?')}`"
                          + (f": {inp['description']}" if inp.get("description") else "") + "_",
                          *_fenced(inp["file_text"])]
            elif name == "str_replace" and "new_str" in inp:
                parts += [f"_Edited `{inp.get('path', '?')}`_", "Replaced:",
                          *_fenced(inp.get("old_str") or ""), "With:", *_fenced(inp["new_str"] or "")]
            elif name.startswith("memory_") and name != "memory_read":
                body = inp.get("content") or inp.get("file_text") or inp.get("new_str")
                if body:
                    parts += [f"_Claude memory update ({name}, `{inp.get('path', '?')}`)_", *_fenced(body)]
            elif name == "web_search" and inp.get("query"):
                parts.append(f"_Searched the web: {inp['query']}_")
            elif name == "web_fetch" and inp.get("url"):
                parts.append(f"_Fetched: {inp['url']}_")
            else:
                other_tools.append(name)
        # thinking blocks and tool_result payloads are deliberately dropped
    return parts, other_tools


def render_conversation(conv: dict) -> tuple[str, int]:
    title = conv.get("name") or f"Claude conversation {str(conv.get('uuid', ''))[:8]}"
    out = [f"# {title}", ""]
    if conv.get("summary"):
        out += [_escape_headings(conv["summary"]), ""]
    turn = 0
    for msg in active_branch(conv):
        sender = msg.get("sender")
        if sender == "human":
            parts = _human_parts(msg)
            if not parts:
                continue
            turn += 1
            ts = (msg.get("created_at") or "")[:19]
            mid = str(msg.get("uuid") or "")[:8]
            out += [f"## Turn {turn}" + (f" · {ts}Z" if ts else "") + (f" · msg {mid}" if mid else ""), ""]
            out += ["**User:**", *parts, ""]
        elif sender == "assistant":
            parts, tools = _assistant_parts(msg)
            if not parts and not tools:
                continue
            if turn == 0:
                turn = 1
                out += ["## Turn 1", ""]
            if parts:
                out += ["**Assistant:**", *parts, ""]
            if tools:
                out += [f"_Tools used: {', '.join(sorted(set(tools)))}_", ""]
    return "\n".join(out), turn


def render_project(project: dict, docs: list[dict]) -> str:
    out = [f"# Claude project: {project.get('name') or project.get('uuid')}", ""]
    if project.get("description"):
        out += ["## Description", "", _escape_headings(project["description"]), ""]
    if project.get("prompt_template"):
        out += ["## Project instructions", "", *_fenced(project["prompt_template"]), ""]
    for d in docs:
        content = d.get("content") or d.get("cleaned_content")
        if content:
            out += [f"## Knowledge file: {d.get('file_name') or d.get('uuid')}", "", *_fenced(content), ""]
    return "\n".join(out)


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
