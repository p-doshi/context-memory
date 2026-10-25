"""Source verification: check a retrieved chunk against its authoritative file.

Design §7: a memory is a claim until checked against its source. For chunks
from local files (code, docs) the hub can check directly:

  verified  the file exists and still contains the chunk's text
  stale     the file exists but no longer contains that text (changed since
            indexing), or the chunk was retired
  missing   the file no longer exists

Chats, web pages and other sources without a local file stay `unverified`.
Whitespace is normalised before comparing, so re-indentation alone does not
make a chunk stale. File reads are cached by (path, mtime, size).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

FILE_SOURCES = {"git", "file"}
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


@lru_cache(maxsize=256)
def _file_text(path: str, mtime: float, size: int) -> str:
    return _norm(Path(path).read_text(encoding="utf-8", errors="replace"))


def check(rec: dict) -> tuple[str, str | None]:
    """(status, warning) for one chunk record from SQLite."""
    if rec.get("valid_to"):
        return "stale", "superseded by a newer version of its source"
    if rec.get("source") not in FILE_SOURCES:
        return "unverified", None
    p = Path(rec["path"])
    try:
        st = p.stat()
    except OSError:
        return "missing", f"source file no longer exists: {rec['path']}"
    try:
        current = _file_text(str(p), st.st_mtime, st.st_size)
    except OSError:
        return "unverified", None
    if _norm(rec["text"]) in current:
        return "verified", None
    return "stale", f"source file changed since indexing: {rec['path']}"
