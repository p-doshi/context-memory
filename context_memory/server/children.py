"""Long-running helper processes the daemon keeps alive, when enabled:

  llm        llama.cpp llama-server on 127.0.0.1 (the visitor answer model)
  public     the public node (server/public_node.py) on 127.0.0.1
  cloudflared  the named Cloudflare tunnel that carries visitor traffic to it

Each runs with a restart backoff like the SSH tunnels. Secrets reach a child
through its environment, never its command line. Output goes to
<home>/logs/<name>.log.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from context_memory.util import NO_WINDOW

log = logging.getLogger("cmh.children")


class Child(threading.Thread):
    def __init__(self, name: str, argv: list[str], stop: threading.Event, log_path: Path,
                 env: dict[str, str] | None = None, fresh_log: bool = False):
        super().__init__(name=f"child-{name}", daemon=True)
        self.label, self.argv, self.stop_event, self.log_path = name, argv, stop, log_path
        self.fresh_log = fresh_log  # start each run with an empty log (cloudflared: its URL)
        self.env = {**os.environ, **(env or {})}
        self.state, self.since, self.pid = "starting", time.time(), None

    def run(self) -> None:
        backoff = 5
        while not self.stop_event.is_set():
            started = time.time()
            with open(self.log_path, "wb" if self.fresh_log else "ab") as out:
                proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                        env=self.env, creationflags=NO_WINDOW)
                self.state, self.since, self.pid = "up", time.time(), proc.pid
                log.info("%s started (pid %d)", self.label, proc.pid)
                while proc.poll() is None and not self.stop_event.is_set():
                    time.sleep(1)
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            self.state, self.since, self.pid = "down", time.time(), None
            log.warning("%s exited (%s)", self.label, proc.returncode)
            backoff = 5 if time.time() - started > 300 else min(backoff * 2, 600)
            self.stop_event.wait(backoff)


def plan(settings, secrets_get) -> list[tuple[str, list[str], dict[str, str]]]:
    """(name, argv, env) for each helper that is enabled and configured."""
    if not settings.public_enabled:
        return []
    out = []
    if settings.llm_server_exe and settings.llm_model_path and settings.public_llm_url:
        port = settings.public_llm_url.rstrip("/").rsplit(":", 1)[-1]
        out.append(("llm", [str(settings.llm_server_exe), "-m", str(settings.llm_model_path),
                            "--host", "127.0.0.1", "--port", port, "-ngl", str(settings.llm_gpu_layers),
                            "-c", str(settings.llm_context_tokens), "--parallel", "1", "--no-webui"],
                    {"LLAMA_API_KEY": secrets_get("llm-server-key") or ""}))
    out.append(("public", [sys.executable, "-m", "context_memory.cli", "public", "serve"], {}))
    if settings.cloudflared_exe and settings.cloudflared_config:
        # Named tunnel (needs a Cloudflare account and a domain there).
        out.append(("cloudflared", [str(settings.cloudflared_exe), "tunnel", "--no-autoupdate",
                                    "--config", str(settings.cloudflared_config), "run"], {}))
    elif settings.cloudflared_exe:
        # Quick tunnel: no account, no domain; a random trycloudflare.com address that
        # changes on restart (read from the log by tunnel_url()).
        out.append(("cloudflared", [str(settings.cloudflared_exe), "tunnel", "--no-autoupdate",
                                    "--url", f"http://127.0.0.1:{settings.public_port}"], {}))
    return out


_QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def tunnel_url(home: Path, since: float = 0) -> str | None:
    """The quick tunnel's current public address: the last one cloudflared
    logged, if the log was written after `since` (the child's start time)."""
    log_path = home / "logs" / "cloudflared.log"
    try:
        if log_path.stat().st_mtime < since:
            return None
        with open(log_path, "rb") as f:
            f.seek(max(0, log_path.stat().st_size - 200_000))
            found = _QUICK_URL.findall(f.read().decode("utf-8", "replace"))
    except OSError:
        return None
    return found[-1] if found else None


def start(settings, stop: threading.Event, secrets_get) -> list[Child]:
    logs = settings.data_dir.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    children = [Child(name, argv, stop, logs / f"{name}.log", env, fresh_log=(name == "cloudflared"))
                for name, argv, env in plan(settings, secrets_get)]
    for c in children:
        RUNNING[c.label] = c
        c.start()
    return children


RUNNING: dict[str, Child] = {}  # name -> child, for the daemon pass (tunnel URL)
