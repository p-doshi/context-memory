"""Local snapshots of the memory store.

A snapshot is <home>/backup/snapshot-YYYYMMDD-HHMMSS/ with:
  metadata.sqlite3   copied with SQLite's online backup API (consistent while live)
  lance/             the vector table directory
  config/            sources.toml and other owner config (device bundles excluded)
  inbox-remote/      raw transcripts received from other machines
  manifest.json      counts, sizes, verification result

Browser profiles (session cookies) and device tokens are deliberately left
out: both are re-creatable and sensitive. Snapshots live on the same disk as
the data, so they guard against corruption and mistakes, not disk failure.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

PREFIX = "snapshot-"


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def verify(snapshot: Path, dim: int, embedding_model: str) -> dict:
    """Integrity check of a snapshot: SQLite integrity_check, LanceDB opens, and
    the vector table has a row for every chunk row in SQLite."""
    from context_memory.memory.lance_store import LanceStore

    con = sqlite3.connect(f"file:{snapshot / 'metadata.sqlite3'}?mode=ro", uri=True)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    docs = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    con.close()
    vectors = LanceStore(snapshot / "lance", dim, embedding_model).count()
    ok = integrity == "ok" and vectors == chunks
    return {"ok": ok, "sqlite_integrity": integrity, "documents": docs, "chunks": chunks,
            "vectors": vectors}


def make_snapshot(svc, home: Path, keep: int = 7) -> dict:
    t0 = time.time()
    root = home / "backup"
    root.mkdir(parents=True, exist_ok=True)
    snap = root / f"{PREFIX}{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    tmp = snap.with_name(snap.name + ".partial")
    tmp.mkdir()
    try:
        dst = sqlite3.connect(tmp / "metadata.sqlite3")
        with dst:
            svc.sqlite.conn.backup(dst)
        dst.close()
        shutil.copytree(svc.settings.lance_path, tmp / "lance")
        cfg = home / "config"
        if cfg.exists():
            shutil.copytree(cfg, tmp / "config", ignore=shutil.ignore_patterns("devices"))
        remote = home / "inbox" / "remote"
        if remote.exists():
            shutil.copytree(remote, tmp / "inbox-remote")
        result = verify(tmp, svc.embedder.dim, svc.embedder.model_name)
        chain = svc.sqlite.verify_audit_chain()
        manifest = {"created": datetime.now().isoformat(timespec="seconds"), **result,
                    "bytes": _dir_bytes(tmp), "seconds": round(time.time() - t0, 1),
                    # An outside copy of the audit chain head: later tampering with
                    # the log no longer matches the head recorded here.
                    "audit_chain": {k: chain.get(k) for k in ("ok", "rows", "head", "broken_at")}}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
        if not result["ok"]:
            raise RuntimeError(f"snapshot failed verification: {result}")
        tmp.rename(snap)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    # Retention: keep the newest `keep` verified snapshots.
    snaps = sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(PREFIX)
                   and not p.name.endswith(".partial"))
    for old in snaps[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    svc.sqlite.audit("daemon", "backup", snap.name, "ok", manifest)
    return {"snapshot": str(snap), **manifest}


def last_snapshot_age(home: Path) -> float | None:
    root = home / "backup"
    if not root.exists():
        return None
    snaps = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(PREFIX)
             and not p.name.endswith(".partial")]
    return time.time() - max(p.stat().st_mtime for p in snaps) if snaps else None
