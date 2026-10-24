"""Always-on indexing daemon.

Every `daemon_interval_seconds` it re-reads the sources config and indexes
Claude Code sessions (idle ones only), codebases, and documentation folders.
Unchanged files are skipped by content hash, so a pass over an unchanged
corpus costs file reads and hashing, not embedding.

A lock file guarantees a single instance. A heartbeat JSON records the last
pass so the web console (and `cmh status`) can show liveness.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from collections import Counter
from logging.handlers import RotatingFileHandler
from pathlib import Path

from context_memory.config import Settings, get_settings
from context_memory.ingestion.claude_code import ingest_config_dir
from context_memory.ingestion.code import ingest_codebase
from context_memory.ingestion.markdown import ingest_markdown_dir
from context_memory.sources import load_sources

log = logging.getLogger("cmh.daemon")


class SingleInstance:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise SystemExit(f"another daemon holds {self.path}")
        return self

    def __exit__(self, *exc):
        self.fh.close()


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "daemon.log", maxBytes=5_000_000, backupCount=5,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("cmh")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr and sys.stderr.isatty():
        root.addHandler(logging.StreamHandler())


def _summarize(results: list[dict]) -> dict:
    return dict(Counter(r.get("status", "?") for r in results))


def run_pass(svc, settings: Settings) -> dict:
    sources = load_sources(settings.resolved_sources_file)
    # Owner-listed source paths extend the allowlist for this pass.
    svc.settings.ingest_roots = list(dict.fromkeys(settings.ingest_roots + sources.roots()))
    summary: dict[str, dict] = {}

    for acct in sources.accounts:
        if acct.provider == "claude" and acct.claude_config_dir:
            res = ingest_config_dir(svc, acct.claude_config_dir, acct.label,
                                    settings.session_idle_seconds, sources.claude_project_map)
            summary[f"claude:{acct.label}"] = _summarize(res)
            for r in res:
                if r.get("status") == "error":
                    log.warning("session %s: %s", r["path"], r.get("error"))

    for src in sources.codebases:
        res = ingest_codebase(svc, src.path, src.project, src.max_files)
        summary[f"code:{src.project}"] = _summarize(res)

    for src in sources.docs:
        res = ingest_markdown_dir(svc, src.path, src.project, src.max_files, actor="daemon")
        summary[f"docs:{src.project}"] = _summarize(res)
    return summary


def main(settings: Settings | None = None, once: bool = False) -> int:
    settings = settings or get_settings()
    home = settings.data_dir.parent
    _setup_logging(home / "logs")
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    with SingleInstance(home / "run" / "daemon.lock"):
        from context_memory.server.app import build_service
        log.info("daemon starting; sources=%s data=%s", settings.resolved_sources_file,
                 settings.data_dir)
        svc = build_service(settings)
        log.info("embedder %s dim=%d", svc.embedder.model_name, svc.embedder.dim)
        heartbeat = home / "run" / "heartbeat.json"
        while not stop["flag"]:
            t0 = time.time()
            try:
                summary = run_pass(svc, settings)
                status = "ok"
            except Exception as e:  # keep the daemon alive; the next pass retries
                log.exception("pass failed")
                summary, status = {"error": repr(e)}, "error"
            elapsed = time.time() - t0
            log.info("pass %s in %.1fs: %s", status, elapsed, json.dumps(summary))
            heartbeat.write_text(json.dumps({
                "pid": os.getpid(), "last_pass_end": time.time(), "elapsed_s": elapsed,
                "status": status, "summary": summary,
                "stats": svc.sqlite.stats(), "vectors": svc.lance.count(),
            }, indent=2))
            if once:
                break
            deadline = time.time() + settings.daemon_interval_seconds
            while not stop["flag"] and time.time() < deadline:
                time.sleep(1)
        svc.close()
        log.info("daemon stopped")
    return 0
