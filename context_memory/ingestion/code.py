"""Source-code ingestion.

Files come from `git ls-files` when the root is a git work tree (so
.gitignore is respected), otherwise from a directory walk with the usual
build/vendor directories skipped. Python is split at top-level definitions
via `ast`; other languages are packed from blank-line-separated blocks.
Every chunk's section_path starts with its line range ("L12-L80"), which is
the trace the code verifier needs (design §7.3).
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Callable

from context_memory.ingestion.markdown import (
    MARKDOWN_SUFFIXES, SKIP_DIRS, PathNotAllowed, check_allowed, git_version,
    ingest_markdown_file,
)
from context_memory.ingestion.sensitive_scanner import blocked_filename
from context_memory.memory.chunker import Chunk
from context_memory.memory.service import IngressRejected, MemoryService

CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".java", ".kt", ".c", ".h",
    ".cc", ".cpp", ".hpp", ".cs", ".go", ".rs", ".r", ".R", ".jl", ".m", ".f90",
    ".sh", ".ps1", ".sql", ".toml", ".yaml", ".yml", ".cfg", ".ini",
}
MAX_CODE_BYTES = 500_000
EXTRA_SKIP = SKIP_DIRS | {"dist", "build", ".mypy_cache", ".ruff_cache", ".tox", ".idea",
                          ".vscode", "site-packages", ".ipynb_checkpoints"}


def list_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                             timeout=30)
        if out.returncode == 0:
            names = [n for n in out.stdout.decode("utf-8", "replace").split("\0") if n]
            return sorted(root / n for n in names if (root / n).is_file())
    except (OSError, subprocess.SubprocessError):
        pass
    files = []
    for p in root.rglob("*"):
        if any(part in EXTRA_SKIP for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            files.append(p)
    return sorted(files)


def _python_units(source: str) -> list[tuple[int, int, str]]:
    """(start_line, end_line, label) for module preamble and each top-level def."""
    tree = ast.parse(source)
    n_lines = source.count("\n") + 1
    units: list[tuple[int, int, str]] = []
    body = tree.body
    first_def = next((i for i, node in enumerate(body)
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))),
                     len(body))
    if first_def > 0:
        end = body[first_def].lineno - 1 if first_def < len(body) else n_lines
        units.append((1, max(1, end), "module"))
    for node in body[first_def:]:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        if isinstance(node, ast.ClassDef):
            label = f"class {node.name}"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            label = f"def {node.name}"
        else:
            label = "module"
        units.append((start, node.end_lineno or start, label))
    return units


def _block_units(source: str) -> list[tuple[int, int, str]]:
    units, start = [], None
    lines = source.splitlines()
    for i, line in enumerate(lines, 1):
        if line.strip():
            start = start or i
        elif start:
            units.append((start, i - 1, ""))
            start = None
    if start:
        units.append((start, len(lines), ""))
    return units


def make_code_chunker(path: Path, count_tokens: Callable[[str], int],
                      max_tokens: int) -> Callable[[str], list[Chunk]]:
    def chunker(source: str) -> list[Chunk]:
        lines = source.splitlines()
        units = None
        if path.suffix in (".py", ".pyi"):
            try:
                units = _python_units(source)
            except SyntaxError:
                units = None
        units = units or _block_units(source)

        chunks: list[Chunk] = []
        cur_start = cur_end = None
        labels: list[str] = []
        # Prefix sums of per-line token counts (+1 per newline): span(s, e) is
        # the token estimate for lines s..e (1-based, inclusive) in O(1).
        prefix = [0]
        for line in lines:
            prefix.append(prefix[-1] + count_tokens(line) + 1)

        def span(s: int, e: int) -> int:
            return prefix[min(e, len(lines))] - prefix[s - 1]

        def emit(start: int, end: int, label_list: list[str]) -> None:
            text = "\n".join(lines[start - 1:end])
            if not text.strip():
                return
            label = ", ".join(dict.fromkeys(l for l in label_list if l))
            section = f"L{start}-L{end}" + (f" · {label}" if label else "")
            chunks.append(Chunk(len(chunks), section, text, count_tokens(text)))

        for start, end, label in units:
            if span(start, end) > max_tokens:
                if cur_start:
                    emit(cur_start, cur_end, labels)
                    cur_start, labels = None, []
                # Window an oversized unit by lines.
                s = start
                while s <= end:
                    e = s
                    while e < end and span(s, e + 1) <= max_tokens:
                        e += 1
                    emit(s, e, [label])
                    s = e + 1
                continue
            if cur_start and span(cur_start, end) > max_tokens:
                emit(cur_start, cur_end, labels)
                cur_start, labels = None, []
            cur_start = cur_start or start
            cur_end = end
            labels.append(label)
        if cur_start:
            emit(cur_start, cur_end, labels)
        return chunks

    return chunker


def ingest_code_file(svc: MemoryService, path: Path, project: str, repo_root: Path,
                     actor: str = "daemon") -> dict:
    if blocked_filename(path):
        raise IngressRejected(f"blocked filename: {path.name}")
    size = path.stat().st_size
    if size > MAX_CODE_BYTES:
        raise IngressRejected(f"file is {size} bytes; code limit is {MAX_CODE_BYTES}")
    raw = path.read_bytes()
    if b"\0" in raw[:8192]:
        raise IngressRejected("binary file")
    text = raw.decode("utf-8")
    rel = path.relative_to(repo_root).as_posix()
    return svc.ingest_text(
        text=text, project=project, source="git" if (repo_root / ".git").exists() else "file",
        kind="code", path=path.as_posix(), title=rel, source_version=git_version(path),
        tags=[f"lang:{path.suffix.lstrip('.').lower()}"], actor=actor,
        chunker=make_code_chunker(path, svc.embedder.count_tokens, svc.settings.chunk_max_tokens),
    )


def ingest_codebase(svc: MemoryService, root: Path, project: str, max_files: int = 2000,
                    actor: str = "daemon") -> list[dict]:
    root = check_allowed(Path(root), svc.settings.ingest_roots)
    results: list[dict] = []
    for p in list_files(root):
        suffix = p.suffix
        if suffix not in CODE_SUFFIXES and suffix.lower() not in MARKDOWN_SUFFIXES:
            continue
        if len(results) >= max_files:
            results.append({"path": str(p), "status": "stopped",
                            "error": f"file budget of {max_files} reached"})
            break
        try:
            if p.stat().st_size == 0 or not p.read_bytes().strip():
                results.append({"path": str(p), "status": "empty"})
                continue
            if suffix.lower() in MARKDOWN_SUFFIXES:
                r = ingest_markdown_file(svc, p, project, actor)
            else:
                r = ingest_code_file(svc, p, project, root, actor)
            results.append({"path": str(p), **r})
        except (IngressRejected, PathNotAllowed, UnicodeDecodeError, ValueError) as e:
            results.append({"path": str(p), "status": "rejected", "error": str(e)})
    return results
