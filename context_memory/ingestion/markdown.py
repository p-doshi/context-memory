"""Markdown file and directory ingestion under the configured path allowlist."""

from __future__ import annotations

import subprocess
from pathlib import Path

from context_memory.util import NO_WINDOW

from context_memory.ingestion.sensitive_scanner import blocked_filename
from context_memory.memory.service import IngressRejected, MemoryService

MARKDOWN_SUFFIXES = {".md", ".markdown"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}


class PathNotAllowed(Exception):
    pass


def check_allowed(path: Path, roots: list[Path]) -> Path:
    resolved = path.resolve()
    for root in roots:
        if resolved == root.resolve() or resolved.is_relative_to(root.resolve()):
            return resolved
    raise PathNotAllowed(
        f"{resolved} is outside the ingest allowlist; add a root via CMH_INGEST_ROOTS"
    )


def git_version(path: Path) -> str | None:
    """'git:<sha>' when the file is tracked and unmodified, else None."""
    try:
        cwd = path.parent
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
                             text=True, timeout=5, creationflags=NO_WINDOW)
        if sha.returncode != 0:
            return None
        dirty = subprocess.run(["git", "status", "--porcelain", "--", path.name], cwd=cwd,
                               capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
        if dirty.returncode != 0 or dirty.stdout.strip():
            return None
        return "git:" + sha.stdout.strip()[:12]
    except (OSError, subprocess.SubprocessError):
        return None


def ingest_markdown_file(svc: MemoryService, path: Path, project: str,
                         actor: str = "cli") -> dict:
    path = check_allowed(Path(path), svc.settings.ingest_roots)
    if path.suffix.lower() not in MARKDOWN_SUFFIXES:
        raise IngressRejected(f"not a Markdown file: {path.name}")
    if blocked_filename(path):
        svc.sqlite.audit(actor, "ingest", str(path), "rejected", {"reason": "blocked_filename"})
        raise IngressRejected(f"blocked filename: {path.name}")
    size = path.stat().st_size
    if size > svc.settings.max_file_bytes:
        svc.sqlite.audit(actor, "ingest", str(path), "rejected", {"reason": "too_large", "bytes": size})
        raise IngressRejected(f"file is {size} bytes; limit is {svc.settings.max_file_bytes}")

    text = path.read_text(encoding="utf-8")
    title = next((ln.lstrip("#").strip() for ln in text.splitlines() if ln.startswith("# ")),
                 path.stem)
    return svc.ingest_text(
        text=text, project=project, source="file", kind="note", path=path.as_posix(),
        title=title, source_version=git_version(path), actor=actor,
    )


def ingest_markdown_dir(svc: MemoryService, root: Path, project: str,
                        max_files: int = 200, actor: str = "cli") -> list[dict]:
    root = check_allowed(Path(root), svc.settings.ingest_roots)
    results: list[dict] = []
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if not p.is_file() or p.suffix.lower() not in MARKDOWN_SUFFIXES:
            continue
        if len(results) >= max_files:
            results.append({"path": str(p), "status": "stopped",
                            "error": f"file budget of {max_files} reached"})
            break
        try:
            results.append({"path": str(p), **ingest_markdown_file(svc, p, project, actor)})
        except (IngressRejected, PathNotAllowed, UnicodeDecodeError) as e:
            results.append({"path": str(p), "status": "rejected", "error": str(e)})
    return results
