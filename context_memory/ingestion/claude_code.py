"""Claude Code session logs (<config_dir>/projects/<project>/<session>.jsonl).

Each session becomes one document: a Markdown rendering with one heading per
turn (user prompt plus the assistant's text replies). Tool calls are reduced
to their names; tool inputs/outputs and thinking blocks are dropped, because
they are mostly file contents already covered by codebase ingestion.

Session files carry no account identifier. The account label comes from the
sources config entry for the config directory, so attribution is only
reliable when each account uses its own CLAUDE_CONFIG_DIR.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from context_memory.memory.service import IngressRejected, MemoryService
from context_memory.util import slugify, stable_id


@dataclass
class Turn:
    user: str
    timestamp: str | None
    message_id: str | None
    assistant: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    sidechain: bool = False


@dataclass
class Session:
    session_id: str
    path: Path
    title: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    version: str | None = None
    models: set[str] = field(default_factory=set)
    turns: list[Turn] = field(default_factory=list)
    first_ts: str | None = None
    last_ts: str | None = None


def _texts(content) -> tuple[list[str], list[str], bool]:
    """(text blocks, tool names, only_tool_results) from a message content field."""
    if isinstance(content, str):
        return [content], [], False
    texts, tools, kinds = [], [], set()
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kinds.add(block.get("type"))
        if block.get("type") == "text" and block.get("text"):
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            tools.append(block.get("name", "tool"))
    return texts, tools, bool(kinds) and kinds <= {"tool_result"}


def parse_session(path: Path) -> Session:
    s = Session(session_id=path.stem, path=path)
    current: Turn | None = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("type")
            if t == "ai-title" and rec.get("aiTitle"):
                s.title = rec["aiTitle"]
                continue
            if t not in ("user", "assistant"):
                continue
            ts = rec.get("timestamp")
            if ts:
                s.first_ts = s.first_ts or ts
                s.last_ts = ts
            s.cwd = s.cwd or rec.get("cwd")
            s.git_branch = s.git_branch or rec.get("gitBranch")
            s.version = rec.get("version") or s.version
            msg = rec.get("message") or {}
            texts, tools, only_results = _texts(msg.get("content"))
            if t == "user":
                if only_results or not texts or rec.get("isMeta"):
                    continue
                current = Turn("\n\n".join(texts), ts, rec.get("uuid"),
                               sidechain=bool(rec.get("isSidechain")))
                s.turns.append(current)
            else:
                if msg.get("model"):
                    s.models.add(msg["model"])
                if current is None:
                    current = Turn("", ts, rec.get("uuid"))
                    s.turns.append(current)
                current.assistant.extend(texts)
                current.tools.extend(tools)
    return s


def _escape_headings(text: str) -> str:
    # Chat text must not create document sections of its own.
    return re.sub(r"(?m)^(\s{0,3})#", r"\1\\#", text)


def render(s: Session) -> str:
    out = [f"# {s.title or 'Claude Code session ' + s.session_id[:8]}", ""]
    for i, turn in enumerate(s.turns, 1):
        head = f"## Turn {i}"
        if turn.timestamp:
            head += f" · {turn.timestamp[:19]}Z"
        if turn.message_id:
            head += f" · msg {turn.message_id[:8]}"
        if turn.sidechain:
            head += " · subagent"
        out += [head, ""]
        if turn.user:
            out += ["**User:**", _escape_headings(turn.user.strip()), ""]
        if turn.assistant:
            out += ["**Assistant:**", _escape_headings("\n\n".join(turn.assistant).strip()), ""]
        if turn.tools:
            names = sorted(set(turn.tools))
            out += [f"_Tools used: {', '.join(names)}_", ""]
    return "\n".join(out)


def project_slug(cwd: str | None, fallback: str) -> str:
    return slugify(Path(cwd).name if cwd else fallback, "claude-sessions")


def discover(config_dir: Path) -> list[Path]:
    root = Path(config_dir) / "projects"
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/*.jsonl") if p.is_file())


def ingest_session(svc: MemoryService, path: Path, account: str,
                   project_map: dict[str, str] | None = None, actor: str = "daemon") -> dict:
    s = parse_session(path)
    if not s.turns:
        return {"status": "empty", "session_id": s.session_id}
    project_dir = path.parent.name
    project = (project_map or {}).get(project_dir) or project_slug(s.cwd, project_dir)
    return svc.ingest_text(
        text=render(s),
        project=project,
        source="claude",
        kind="chat",
        path=path.as_posix(),
        title=s.title,
        doc_id=stable_id("d", "claude-code", s.session_id),
        tags=["claude-code", f"account:{account}"],
        meta={
            "platform": "claude-code",
            "account": account,
            "session_id": s.session_id,
            "cwd": s.cwd,
            "git_branch": s.git_branch,
            "claude_code_version": s.version,
            "models": sorted(s.models),
            "first_timestamp": s.first_ts,
            "last_timestamp": s.last_ts,
            "turns": len(s.turns),
            "approval": "owner-approved: all Claude Code sessions (2026-10-24)",
        },
        on_secret="redact",
        actor=actor,
    )


def ingest_config_dir(svc: MemoryService, config_dir: Path, account: str,
                      idle_seconds: int, project_map: dict[str, str] | None = None,
                      actor: str = "daemon") -> list[dict]:
    now = time.time()
    results = []
    for path in discover(config_dir):
        if now - path.stat().st_mtime < idle_seconds:
            results.append({"path": str(path), "status": "active"})
            continue
        try:
            results.append({"path": str(path),
                            **ingest_session(svc, path, account, project_map, actor)})
        except (IngressRejected, ValueError, OSError) as e:
            results.append({"path": str(path), "status": "error", "error": str(e)})
    return results
