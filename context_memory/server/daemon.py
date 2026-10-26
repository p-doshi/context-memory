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
from context_memory.ingestion.claude_code import ingest_config_dir, ingest_session
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
    svc.project_aliases = sources.project_aliases
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
    remote = process_remote_inbox(svc, settings.data_dir.parent, sources.claude_project_map)
    if remote:
        summary["remote_sessions"] = remote
    code = process_remote_code(svc, settings.data_dir.parent, sources.claude_project_map)
    if code:
        summary["remote_code"] = code
    local_code = index_session_repos(svc, sources)
    if local_code:
        summary["session_repos"] = local_code
    summary.update(browser_syncs(svc, settings, sources))
    # Every ingest appends a small LanceDB fragment; vector search slows as they
    # pile up (456 fragments: ~100 ms per search, measured). Compact regularly.
    if svc.lance.small_fragments() >= 32:
        summary["lance_optimize"] = svc.lance.optimize()
    # Daily verified snapshot, taken here because the daemon is the only writer.
    from context_memory.server import backup
    age = backup.last_snapshot_age(settings.data_dir.parent)
    if age is None or age > settings.backup_interval_seconds:
        try:
            snap = backup.make_snapshot(svc, settings.data_dir.parent, keep=settings.backup_keep)
            summary["backup"] = {k: snap[k] for k in ("chunks", "vectors", "bytes", "seconds")}
        except Exception as e:
            log.exception("backup failed")
            summary["backup"] = {"failed": repr(e)[:200]}
    if settings.public_enabled and sources.public_projects:
        summary.update(public_export_due(svc, settings, sources))
    if settings.public_enabled and settings.public_site_repo:
        summary.update(site_publish_due(settings))
    summary.update(digests_due(svc, settings))
    return summary


def digests_due(svc, settings: Settings) -> dict:
    """Digest a few more conversations for the owner report, if the local model is up."""
    from context_memory import digest, llm
    if not settings.digest_enabled or not settings.public_llm_url or not llm.health(settings.public_llm_url):
        return {}
    from context_memory.secrets import broker
    try:
        return {"digests": digest.run(svc.sqlite.conn, settings.public_llm_url, broker.get_secret("llm-server-key"),
                                      budget_seconds=settings.digest_budget_seconds)}
    except Exception as e:
        log.exception("digests failed")
        return {"digests": {"failed": repr(e)[:200]}}


_publish_lock = threading.Lock()


def site_publish_due(settings: Settings) -> dict:
    """Push the static site with the quick tunnel's address whenever it changes."""
    with _publish_lock:  # the pass and the tunnel watcher never push at once
        return _site_publish(settings)


def watch_tunnel(settings: Settings, stop: threading.Event, every: float = 10) -> threading.Thread:
    """Publish a new quick-tunnel address within seconds of it appearing, instead of
    at the next daemon pass: until then the public site points at a dead address."""
    def loop():
        while not stop.wait(every):
            try:
                out = site_publish_due(settings)
                if out:
                    log.info("tunnel watcher: %s", json.dumps(out))
            except Exception:
                log.exception("tunnel watcher failed")
    t = threading.Thread(target=loop, name="tunnel-watcher", daemon=True)
    t.start()
    return t


def _site_publish(settings: Settings) -> dict:
    from context_memory import site_deploy
    from context_memory.server import children as children_mod
    home = settings.data_dir.parent
    child = children_mod.RUNNING.get("cloudflared")
    if child is None or child.state != "up":
        return {}
    url = children_mod.tunnel_url(home, since=child.since)
    if not url:
        return {}
    # Republish when the tunnel address or any site file changes.
    import hashlib
    digest = hashlib.sha256(b"".join(p.relative_to(site_deploy.SITE).as_posix().encode() + p.read_bytes()
                                     for p in sorted(site_deploy.SITE.rglob("*")) if p.is_file())).hexdigest()[:16]
    marker = home / "public" / "published-api"
    if marker.exists() and marker.read_text().strip() == f"{url} {digest}":
        return {}
    try:
        out = site_deploy.publish(home, url.removeprefix("https://"), settings.public_site_repo)
        marker.write_text(f"{url} {digest}")
        return {"site_publish": {**out, "api": url}}
    except Exception as e:
        log.exception("site publish failed")
        return {"site_publish": {"failed": repr(e)[:200]}}


def public_export_due(svc, settings: Settings, sources) -> dict:
    """Refresh the public node's store every public_export_interval_seconds."""
    from context_memory.public_export import current_store, export_public
    root = settings.data_dir.parent / "public"
    store = current_store(root)
    if store and time.time() - store.stat().st_mtime < settings.public_export_interval_seconds:
        return {}
    try:
        out = export_public(svc, sources, root, actor="daemon")
        return {"public_export": {k: out[k] for k in ("documents", "chunks", "vectors")}}
    except Exception as e:
        log.exception("public export failed")
        return {"public_export": {"failed": repr(e)[:200]}}


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


def process_remote_inbox(svc, home: Path, project_map: dict[str, str]) -> dict:
    """Index transcripts the hub received from other machines
    (<home>/inbox/remote/<device>/<project dir>/<session>.jsonl + .meta.json).
    A state file remembers each file's mtime so unchanged uploads are skipped."""
    root = home / "inbox" / "remote"
    if not root.is_dir():
        return {}
    state_file = home / "run" / "remote-inbox.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    results: list[dict] = []
    for path in sorted(list(root.glob("*/*/*.jsonl")) + list(root.glob("*/*/*/subagents/*.jsonl"))):
        key, mtime = str(path), path.stat().st_mtime
        if state.get(key) == mtime:
            continue
        meta_file = path.with_suffix(".meta.json")
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        account = meta.get("account") or path.relative_to(root).parts[0]
        try:
            r = ingest_session(svc, path, account, project_map, actor="daemon")
            results.append(r)
            state[key] = mtime
        except Exception as e:  # one bad upload must not stop the others
            log.warning("remote session %s: %r", path, e)
            results.append({"status": "error"})
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))
    return _summarize(results) if results else {}


def process_remote_code(svc, home: Path, project_map: dict[str, str]) -> dict:
    """Index code bundles other machines uploaded (<home>/inbox/remote/<device>/_code/*.json):
    changed files under the project of that repository's sessions, deleted files retired.
    A bundle is removed once indexed; the store keeps the text."""
    from context_memory.ingestion.claude_code import project_slug
    from context_memory.ingestion.code import ingest_code_text
    from context_memory.memory.service import IngressRejected
    results: list[dict] = []
    for f in sorted((home / "inbox" / "remote").glob("*/_code/*.json")):
        try:
            b = json.loads(f.read_text(encoding="utf-8"))
            device, root = b["device"], b["root"].rstrip("/")
            project = project_map.get(b.get("project_dir") or "") or project_slug(b.get("cwd") or root,
                                                                                    b.get("project_dir") or "")
            for item in b.get("files") or []:
                try:
                    r = ingest_code_text(svc, item["text"], item["path"], project,
                                         f"remote://{device}{root}/{item['path']}", version=item.get("sha"))
                    results.append(r)
                except (IngressRejected, ValueError) as e:
                    results.append({"status": "rejected", "error": str(e)[:200]})
            gone = [f"remote://{device}{root}/{rel}" for rel in b.get("removed") or []]
            for _ in range(svc.retire_paths(project, gone, "source-deleted")):
                results.append({"status": "retired-deleted"})
            f.unlink()
        except Exception as e:  # one bad bundle must not stop the others
            log.warning("code bundle %s: %r", f, e)
            results.append({"status": "error"})
    return _summarize(results) if results else {}


def index_session_repos(svc, sources, max_files: int = 2000) -> dict:
    """Git repositories this laptop's Claude Code sessions ran in are indexed as code,
    like configured codebases (owner's choice, 2026-10-25: 'repos Claude Code works in')."""
    import subprocess
    from context_memory.ingestion.code import ingest_codebase
    configured = {Path(c.path).resolve() for c in sources.codebases}
    roots: dict[Path, str] = {}
    for cwd, project in svc.sqlite.conn.execute(
            "SELECT DISTINCT json_extract(meta, '$.cwd'), project FROM documents "
            "WHERE json_extract(meta, '$.platform') = 'claude-code' AND path NOT LIKE '%/inbox/remote/%' "
            "AND json_extract(meta, '$.cwd') IS NOT NULL"):
        try:
            if not Path(cwd).is_dir():
                continue
            out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                                 text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            root = Path(out.stdout.strip()).resolve()
            if root not in configured:
                roots.setdefault(root, project)
    summary: dict[str, dict] = {}
    for root, project in roots.items():
        svc.settings.ingest_roots = list(dict.fromkeys(svc.settings.ingest_roots + [root]))
        try:
            summary[f"repo:{project}"] = _summarize(ingest_codebase(svc, root, project, max_files))
        except Exception as e:
            log.warning("session repo %s: %r", root, e)
            summary[f"repo:{project}"] = {"failed": 1}
    return summary


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
    summary.update(process_project_exports(svc, inbox / "claude-projects"))
    return summary


def process_project_exports(svc, root: Path) -> dict:
    """claude.ai Project knowledge exports dropped into <home>/inbox/claude-projects/<project>/
    are imported under that memory project, then moved to done/."""
    from context_memory.ingestion.claude_project import import_project_export, is_project_export
    from context_memory.util import validate_project
    summary: dict[str, dict] = {}
    root.mkdir(parents=True, exist_ok=True)
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            validate_project(folder.name)
        except ValueError:
            log.warning("inbox %s: folder name is not a valid project name", folder)
            continue
        for f in sorted(folder.glob("*.zip")):
            try:
                if not is_project_export(f):
                    log.warning("inbox %s: not a project export (holds conversations); use the account inbox", f)
                    continue
                res = import_project_export(svc, f, folder.name, actor="daemon")
            except Exception:
                log.exception("project export import failed: %s", f)
                continue
            done = folder / "done"
            done.mkdir(exist_ok=True)
            f.replace(done / f"{time.strftime('%Y%m%d-%H%M%S')}-{f.name}")
            summary[f"project-export:{folder.name}:{f.name}"] = _summarize(res)
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
        # Warm the model (kernels, GPU clocks) so the first real search is not
        # ~2 s slower than the rest (seen after restarts).
        svc.embedder.embed_query("warm up")
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
        # Public node, answer model, Cloudflare tunnel (only with CMH_PUBLIC_ENABLED).
        from context_memory.secrets import broker
        from context_memory.server import children as children_mod
        child_stop = threading.Event()
        children = [] if once else children_mod.start(settings, child_stop, broker.get_secret)
        if children and settings.public_enabled and settings.public_site_repo:
            watch_tunnel(settings, child_stop)
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
                "children": {c.label: {"state": c.state, "since": c.since} for c in children},
            }, indent=2))
            if once:
                break
            deadline = time.time() + settings.daemon_interval_seconds
            while not stop["flag"] and time.time() < deadline:
                time.sleep(1)
        if tunnels:
            tunnel_stop.set()
        child_stop.set()
        for c in children:
            c.join(15)
        svc.close()
        log.info("daemon stopped")
    return 0
