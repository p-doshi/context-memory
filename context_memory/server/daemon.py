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
import threading
import time
from collections import Counter
from logging.handlers import RotatingFileHandler
from pathlib import Path

from context_memory.config import Settings, get_settings
from context_memory.ingestion.claude_code import ingest_config_dir
from context_memory.ingestion.claude_export import import_export
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

    summary.update(process_inbox(svc, settings.data_dir.parent / "inbox", sources))
    summary.update(browser_syncs(svc, settings, sources))
    # Every ingest appends a small LanceDB fragment; vector search slows as they
    # pile up (456 fragments: ~100 ms per search, measured). Compact regularly.
    if svc.lance.small_fragments() >= 32:
        summary["lance_optimize"] = svc.lance.optimize()
    return summary


def browser_syncs(svc, settings: Settings, sources) -> dict:
    """claude.ai and Perplexity syncs, at most once per browser_sync_interval_seconds
    per account. Accounts without a logged-in profile are skipped."""
    from context_memory.browser import claude_web, perplexity_web
    from context_memory.browser.session import profile_dir

    home = settings.data_dir.parent
    state_file = home / "run" / "browser-sync.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    summary: dict[str, dict] = {}
    for acct in sources.accounts:
        if acct.provider not in ("claude", "perplexity"):
            continue
        if not profile_dir(home, acct.label).exists():
            continue
        if time.time() - state.get(acct.label, 0) < settings.browser_sync_interval_seconds:
            continue
        sync = claude_web.sync if acct.provider == "claude" else perplexity_web.sync
        try:
            res = sync(svc, home, acct.label)
            summary[f"web:{acct.label}"] = _summarize(res)
            state[acct.label] = time.time()
        except claude_web.NotLoggedIn as e:
            log.warning("web sync %s: %s", acct.label, e)
            summary[f"web:{acct.label}"] = {"not_logged_in": 1}
            state[acct.label] = time.time()  # do not retry every pass
        except Exception:
            log.exception("web sync %s failed", acct.label)
            summary[f"web:{acct.label}"] = {"failed": 1}
    state_file.write_text(json.dumps(state))
    return summary


def gpu_status() -> dict | None:
    import subprocess
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,clocks.sm",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        t, u, m, c = (int(x) for x in out.stdout.strip().split(", "))
        return {"temp_c": t, "util_pct": u, "mem_mb": m, "sm_mhz": c}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def process_inbox(svc, inbox: Path, sources) -> dict:
    """Import exports dropped into <home>/inbox/<account-label>/, then move them to done/."""
    summary: dict[str, dict] = {}
    for acct in sources.accounts:
        folder = inbox / acct.label
        folder.mkdir(parents=True, exist_ok=True)
        for f in sorted(folder.iterdir()):
            if not f.is_file() or f.suffix.lower() not in (".zip", ".json"):
                continue
            if acct.provider != "claude":
                log.warning("inbox %s: no importer for provider %s yet", f, acct.provider)
                continue
            try:
                res = import_export(svc, f, acct.label)
            except Exception:
                log.exception("inbox import failed: %s", f)
                continue
            done = folder / "done"
            done.mkdir(exist_ok=True)
            f.replace(done / f"{time.strftime('%Y%m%d-%H%M%S')}-{f.name}")
            summary[f"inbox:{acct.label}:{f.name}"] = _summarize(res)
    return summary


def _attach_std_streams(log_dir: Path) -> None:
    """Under pythonw (the scheduled task) sys.stdout/stderr are None, and any
    library that logs to them (uvicorn, the MCP SDK) crashes its thread
    silently. Point them at a file and log uncaught thread exceptions."""
    if sys.stdout is None or sys.stderr is None:
        stream = open(log_dir / "daemon-stderr.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream

    def excepthook(args):
        log.error("thread %s crashed", args.thread.name if args.thread else "?",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = excepthook


def _wait_for_hub(port: int, timeout: float = 30.0) -> bool:
    import urllib.error
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=2)
            return True
        except urllib.error.HTTPError as e:
            return e.code == 401  # serving, and refusing an unauthenticated probe
        except OSError:
            time.sleep(0.5)
    return False


def main(settings: Settings | None = None, once: bool = False) -> int:
    settings = settings or get_settings()
    home = settings.data_dir.parent
    _setup_logging(home / "logs")
    _attach_std_streams(home / "logs")
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
        log.info("reconcile: %s", svc.reconcile())
        tunnels = []
        if settings.hub_enabled:
            from context_memory.server import tunnels as tunnel_mod
            from context_memory.server.hub import start_in_thread
            start_in_thread(settings, svc.embedder)
            if _wait_for_hub(settings.hub_port):
                log.info("hub serving on %s:%d", settings.hub_host, settings.hub_port)
            else:
                log.error("hub did not come up on %s:%d (see daemon-stderr.log)",
                          settings.hub_host, settings.hub_port)
            tunnel_stop = threading.Event()
            tunnels = tunnel_mod.start(settings.hub_tunnels, settings.hub_port, tunnel_stop)
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
                "gpu": gpu_status(),
                "tunnels": {t.host: {"state": t.state, "since": t.since} for t in tunnels},
            }, indent=2))
            if once:
                break
            deadline = time.time() + settings.daemon_interval_seconds
            while not stop["flag"] and time.time() < deadline:
                time.sleep(1)
        if tunnels:
            tunnel_stop.set()
        svc.close()
        log.info("daemon stopped")
    return 0
