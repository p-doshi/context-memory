#!/usr/bin/env python3
"""Push Claude Code session transcripts from this machine to the memory hub.

Standard library only; Python 3.8+. Copy this one file to each machine.

Configuration (environment variables, or ~/.config/cmh/push.json with the same
keys in lower case without the CMH_ prefix, e.g. {"hub_url": ..., "account": ...}):

  CMH_HUB_URL      e.g. http://127.0.0.1:8765, the end of an SSH tunnel to the hub
  CMH_TOKEN_FILE   file holding this device's token (default ~/.config/cmh/token,
                   must not be readable by other users)
  CMH_ACCOUNT      Claude account label for these sessions (optional; the hub
                   falls back to the account registered for the device)
  CLAUDE_CONFIG_DIR  where Claude Code keeps projects/ (default ~/.claude)

Commands:

  hook-end     for the SessionEnd hook: queue the ended session, start a detached
               upload, exit at once (the hook has a 1.5 s default budget)
  hook-start   for the SessionStart hook: start a detached sweep + upload
  flush        upload everything queued
  sweep        queue changed transcripts not yet uploaded, then flush
  status       show configuration, queue, and hub reachability

Transcripts are sent as opaque files; the hub parses them. The format is
internal to Claude Code and changes between versions.
"""

import gzip
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "cmh"
QUEUE = STATE_DIR / "queue.jsonl"
SENT = STATE_DIR / "sent.json"
LOCK = STATE_DIR / "flush.lock"
LOG = STATE_DIR / "push.log"
IDLE_SECONDS = 600  # sweep skips transcripts written to in the last 10 minutes


def config():
    cfg = {}
    f = Path.home() / ".config" / "cmh" / "push.json"
    if f.exists():
        cfg.update(json.loads(f.read_text()))
    for key in ("hub_url", "token_file", "account"):
        env = os.environ.get("CMH_" + key.upper())
        if env:
            cfg[key] = env
    cfg.setdefault("token_file", str(Path.home() / ".config" / "cmh" / "token"))
    cfg["claude_dir"] = os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    return cfg


def token(cfg):
    if os.environ.get("CMH_HUB_TOKEN"):
        return os.environ["CMH_HUB_TOKEN"].strip()
    p = Path(cfg["token_file"]).expanduser()
    if os.name == "posix" and p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(f"{p} is readable by other users; run: chmod 600 {p}")
    return p.read_text().strip()


def log(msg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + msg + "\n")


def enqueue(path, session_id=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with QUEUE.open("a") as fh:
        fh.write(json.dumps({"path": str(path), "session_id": session_id or Path(path).stem}) + "\n")


def detach(*args):
    """Run this script again in the background, outliving the hook."""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, os.path.abspath(__file__)] + list(args), **kwargs)


def sent_state():
    return json.loads(SENT.read_text()) if SENT.exists() else {}


def sweep(cfg):
    root = Path(cfg["claude_dir"]) / "projects"
    if not root.is_dir():
        return 0
    sent, now, n = sent_state(), time.time(), 0
    # Sessions and their subagent transcripts (<session>/subagents/agent-*.jsonl).
    for p in list(root.glob("*/*.jsonl")) + list(root.glob("*/*/subagents/*.jsonl")):
        st = p.stat()
        if now - st.st_mtime < IDLE_SECONDS:
            continue
        if sent.get(str(p)) == [st.st_mtime, st.st_size]:
            continue
        enqueue(p)
        n += 1
    return n


def upload(cfg, tok, item):
    p = Path(item["path"])
    if not p.exists():
        return True  # deleted locally (e.g. cleanupPeriodDays); nothing to send
    body = gzip.compress(p.read_bytes())
    req = urllib.request.Request(cfg["hub_url"].rstrip("/") + "/v1/sessions", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Encoding", "gzip")
    req.add_header("Content-Type", "application/x-ndjson")
    req.add_header("X-Session-Id", item["session_id"])
    subagent = p.parent.name == "subagents"
    req.add_header("X-Project-Dir", p.parents[2].name if subagent else p.parent.name)
    if subagent:
        req.add_header("X-Parent-Session", p.parents[1].name)
    if cfg.get("account"):
        req.add_header("X-Account", cfg["account"])
    with urllib.request.urlopen(req, timeout=120) as resp:
        log(f"sent {p.name}: {resp.status} {resp.read(300).decode(errors='replace')}")
    st = p.stat()
    sent = sent_state()
    sent[str(p)] = [st.st_mtime, st.st_size]
    SENT.write_text(json.dumps(sent))
    return True


def flush(cfg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        import fcntl
        lock = LOCK.open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another flush is running
    if not QUEUE.exists():
        return
    items, seen = [], set()
    for line in QUEUE.read_text().splitlines():
        try:
            it = json.loads(line)
        except ValueError:
            continue
        if it["path"] not in seen:  # newest upload of a file covers older entries
            seen.add(it["path"])
            items.append(it)
    tok = token(cfg)
    remaining = []
    for it in items:
        try:
            upload(cfg, tok, it)
        except (urllib.error.URLError, OSError) as e:
            log(f"failed {it['path']}: {e}; will retry")
            remaining.append(it)
    QUEUE.write_text("".join(json.dumps(it) + "\n" for it in remaining))


UNREACHABLE_NOTE = (
    "Memory hub not reachable from this machine right now (its SSH tunnel is not up), so "
    "the context-memory tools will fail this session. Do not try to open tunnels or change "
    "SSH settings; tell the user the memory tunnel is down if memory is needed.")


def fetch_context(cfg, cwd, timeout=3):
    """Context pack for cwd from the hub. If the hub cannot be reached, a one-line
    note instead, so Claude does not guess at tunnels or configuration."""
    if not cfg.get("hub_url"):
        return ""
    from urllib.parse import quote
    req = urllib.request.Request(cfg["hub_url"].rstrip("/") + "/v1/context?cwd=" + quote(cwd))
    req.add_header("Authorization", "Bearer " + token(cfg))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("context", "")
    except urllib.error.HTTPError as e:
        log(f"context fetch failed: {e}")
        return ""  # hub reachable but refused (e.g. old token): stay silent, see push.log
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"context fetch failed: {e}")
        return UNREACHABLE_NOTE


def emit_context(text):
    """Hand the pack to Claude Code as SessionStart additional context."""
    if not text:
        return
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def status(cfg):
    print("hub_url:   ", cfg.get("hub_url", "(not set)"))
    print("token_file:", cfg["token_file"], "(exists)" if Path(cfg["token_file"]).expanduser().exists() else "(missing)")
    print("account:   ", cfg.get("account", "(hub default)"))
    print("claude_dir:", cfg["claude_dir"])
    queued = {json.loads(l)["path"] for l in QUEUE.read_text().splitlines() if l.strip()} if QUEUE.exists() else set()
    print("queued:    ", len(queued), "transcript(s)")
    print("sent:      ", len(sent_state()))
    if cfg.get("hub_url"):
        req = urllib.request.Request(cfg["hub_url"].rstrip("/") + "/v1/health")
        req.add_header("Authorization", "Bearer " + token(cfg))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                print("hub:       ", resp.status, resp.read().decode())
        except (urllib.error.URLError, OSError) as e:
            print("hub:        unreachable:", e)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    cfg = config()
    if cmd == "hook-end":
        try:
            data = json.load(sys.stdin)
            if data.get("transcript_path"):
                enqueue(data["transcript_path"], data.get("session_id"))
            detach("flush")
        except Exception as e:  # a hook must never break the user's session
            log(f"hook-end error: {e!r}")
        return 0
    if cmd in ("hook-start", "context"):
        # SessionStart: inject the memory context pack, then (hook-start only)
        # sweep unsent transcripts in the background. Never fails the session.
        try:
            raw = sys.stdin.read() if not sys.stdin.isatty() else ""
            raw = raw.lstrip("﻿")  # PowerShell pipes add a byte-order mark
            data = json.loads(raw) if raw.strip() else {}
            emit_context(fetch_context(cfg, data.get("cwd") or os.getcwd()))
        except Exception as e:
            log(f"{cmd} context error: {e!r}")
        if cmd == "hook-start":
            try:
                detach("sweep")
            except Exception as e:
                log(f"hook-start error: {e!r}")
        return 0
    if not cfg.get("hub_url") and cmd != "status":
        raise SystemExit("CMH_HUB_URL is not set")
    if cmd == "flush":
        flush(cfg)
    elif cmd == "sweep":
        n = sweep(cfg)
        log(f"sweep queued {n}")
        flush(cfg)
    elif cmd == "status":
        status(cfg)
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
