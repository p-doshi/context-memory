"""Pre-commit guard: refuse commits that contain private strings.

The deny list is built at commit time from local, never-committed sources:
titles of indexed chat/web documents in the metadata database, and account
emails in the owner's sources.toml. Only added lines of staged files are
checked. Install with:  python scripts/check_private.py --install
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIN_LEN = 12  # shorter titles ("Notes", "Untitled") would block ordinary words


def home() -> Path:
    data = os.environ.get("CMH_DATA_DIR") or str(
        Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "context-memory" / "data")
    return Path(data).parent


def deny_list() -> set[str]:
    terms: set[str] = set()
    db = home() / "data" / "metadata.sqlite3"
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for (title,) in con.execute(
                "SELECT DISTINCT title FROM documents WHERE source IN ('claude', 'perplexity', 'chatgpt') "
                "AND title IS NOT NULL"):
            t = title.removeprefix("Claude project: ").removeprefix("Perplexity Space: ").strip()
            if len(t) >= MIN_LEN:
                terms.add(t.lower())
        con.close()
    src = home() / "config" / "sources.toml"
    if src.exists():
        for acct in tomllib.loads(src.read_text(encoding="utf-8")).get("accounts", []):
            if acct.get("email"):
                terms.add(acct["email"].lower())
    return terms


def staged_added_lines() -> list[tuple[str, str]]:
    diff = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color"], cwd=REPO,
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    out, current = [], None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and current and current != "uv.lock":
            out.append((current, line[1:]))
    return out


def main() -> int:
    if "--install" in sys.argv:
        hook = REPO / ".git" / "hooks" / "pre-commit"
        hook.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).resolve().as_posix()}"\n')
        hook.chmod(0o755)
        print(f"installed {hook}")
        return 0
    terms = deny_list()
    if not terms:
        return 0
    pattern = re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)))
    hits = [(f, m.group(0)) for f, text in staged_added_lines() for m in [pattern.search(text.lower())] if m]
    if hits:
        print("commit blocked: staged changes contain private strings (titles/emails from local data):")
        for f, term in hits[:20]:
            print(f"  {f}: {term[:4]}… ({len(term)} chars)")
        print("Remove them, or keep that file outside the repository.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
