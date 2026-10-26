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
  doctor       explain why Claude Code does not list the context-memory MCP server;
               `doctor --fix` registers it (only while no Claude Code process runs)

Transcripts are sent as opaque files; the hub parses them. The format is
internal to Claude Code and changes between versions.

Code: when a session ends inside a git repository, hook-end also queues that
repository. Its tracked source and documentation files (no data folders, key or
environment files, notebook outputs, or files over 500 KB) are sent if they
changed since the last upload, with the list of files deleted since. Set
CMH_CODE=0 (or "code": false in push.json) to turn this off on a machine.
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
CODE_STATE = STATE_DIR / "code.json"
CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".java", ".kt", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".cs", ".go", ".rs", ".r", ".jl", ".m", ".f90", ".sh", ".ps1", ".sql", ".toml",
    ".yaml", ".yml", ".cfg", ".ini", ".md", ".markdown", ".rst", ".txt", ".ipynb", ".slurm", ".sbatch",
}
CODE_SKIP_DIRS = {"data", "datasets", "raw", "raw_data", "checkpoints", "ckpt", "wandb", "mlruns", "outputs",
                  "logs", "node_modules", "venv", ".venv", "env", "vendor", "site-packages", "__pycache__",
                  ".ipynb_checkpoints", "build", "dist"}
CODE_SKIP_NAME = ("secret", "credential", "password", "token", "id_rsa", "id_ed25519")
CODE_MAX_BYTES = 500_000
CODE_BATCH_BYTES = 3_000_000


def config():
    cfg = {}
    f = Path.home() / ".config" / "cmh" / "push.json"
    if f.exists():
        cfg.update(json.loads(f.read_text()))
    for key in ("hub_url", "token_file", "account", "code"):
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


def code_files(root):
    """Tracked files worth indexing: source and docs, no data, keys or big files."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=60)
    if out.returncode != 0:
        return {}
    files = {}
    for rel in (n for n in out.stdout.decode("utf-8", "replace").split("\0") if n):
        p = Path(root) / rel
        parts = [x.lower() for x in Path(rel).parts]
        name = parts[-1]
        if (p.suffix.lower() not in CODE_SUFFIXES or any(x in CODE_SKIP_DIRS for x in parts[:-1])
                or name.startswith(".env") or name.endswith((".pem", ".key", ".min.js"))
                or any(w in name for w in CODE_SKIP_NAME)):
            continue
        try:
            if not p.is_file() or p.stat().st_size > CODE_MAX_BYTES:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", "replace")
        if p.suffix.lower() == ".ipynb":
            text = notebook_source(text)
            if text is None:
                continue
        files[rel] = text
    return files


def notebook_source(text):
    """A notebook's cells as Markdown and code, without outputs (which can hold data)."""
    try:
        cells = json.loads(text).get("cells") or []
    except ValueError:
        return None
    parts = []
    for c in cells:
        src = c.get("source") or ""
        src = "".join(src) if isinstance(src, list) else src
        if c.get("cell_type") == "code":
            parts.append("```python\n" + src + "\n```")
        elif src.strip():
            parts.append(src)
    return "\n\n".join(parts)


def upload_code(cfg, tok, item):
    """Send a repository's changed source files (and deletions) since the last upload."""
    import hashlib
    cwd = item["code"]
    top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, timeout=30)
    if top.returncode != 0:
        return True   # not a git repository: nothing to index
    root = top.stdout.decode().strip()
    state = json.loads(CODE_STATE.read_text()) if CODE_STATE.exists() else {}
    known = state.get(root, {})
    files = code_files(root)
    digest = {rel: hashlib.sha256(text.encode("utf-8")).hexdigest() for rel, text in files.items()}
    changed = [rel for rel in sorted(files) if known.get(rel) != digest[rel]]
    removed = sorted(set(known) - set(files))
    batches, cur, size = [], [], 0
    for rel in changed:
        if cur and size + len(files[rel]) > CODE_BATCH_BYTES:
            batches.append(cur)
            cur, size = [], 0
        cur.append(rel)
        size += len(files[rel])
    batches.append(cur)
    for i, batch in enumerate(batches):
        body = {"root": root, "cwd": cwd, "project_dir": item.get("project_dir") or "",
                "files": [{"path": rel, "sha": digest[rel], "text": files[rel]} for rel in batch],
                "removed": removed if i == len(batches) - 1 else []}
        if not body["files"] and not body["removed"]:
            continue
        req = urllib.request.Request(cfg["hub_url"].rstrip("/") + "/v1/code",
                                     data=gzip.compress(json.dumps(body).encode()), method="POST")
        req.add_header("Authorization", "Bearer " + tok)
        req.add_header("Content-Encoding", "gzip")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=120) as resp:
            log(f"code {root}: sent {len(batch)} file(s), {len(body['removed'])} removed: {resp.status}")
        known.update({rel: digest[rel] for rel in batch})
        for rel in body["removed"]:
            known.pop(rel, None)
        state[root] = known
        CODE_STATE.write_text(json.dumps(state))
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
        key = it.get("path") or "code:" + it.get("code", "")
        if key not in seen:  # newest upload of a file covers older entries
            seen.add(key)
            items.append(it)
    tok = token(cfg)
    remaining = []
    for it in items:
        try:
            upload_code(cfg, tok, it) if "code" in it else upload(cfg, tok, it)
        except (urllib.error.URLError, OSError, subprocess.SubprocessError) as e:
            log(f"failed {it.get('path') or it.get('code')}: {e}; will retry")
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


def doctor(cfg, fix=False):
    """Why does Claude Code (CLI or VS Code) not list context-memory? Reports what
    each config file holds; --fix registers it in ~/.claude.json, but only while
    no Claude Code process runs (a running one rewrites the file on exit)."""
    home = Path(os.environ.get("HOME") or Path.home())
    files = [home / ".claude.json"]
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        files.insert(0, Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / ".claude.json")
    print("CLAUDE_CONFIG_DIR:", os.environ.get("CLAUDE_CONFIG_DIR", "(unset)"))
    found = False
    for f in files:
        if not f.exists():
            print(f"{f}: missing")
            continue
        try:
            data = json.loads(f.read_text())
        except ValueError as e:
            print(f"{f}: unreadable JSON ({e})")
            continue
        user = sorted((data.get("mcpServers") or {}).keys())
        proj = [p for p, v in (data.get("projects") or {}).items() if "context-memory" in (v.get("mcpServers") or {})]
        found = found or "context-memory" in user
        print(f"{f}: user-scope MCP servers {user or 'none'}; project-scoped context-memory in {len(proj)} project(s)")
    rc = home / ".bashrc"
    if rc.exists():
        text = rc.read_text(errors="replace")
        guard = min([i for i in (text.find("*i*) ;;"), text.find('[ -z "$PS1" ] && return')) if i >= 0] or [-1])
        pos = text.find("CMH_HUB_TOKEN")
        if pos >= 0 and 0 <= guard < pos:
            print("~/.bashrc: CMH_HUB_TOKEN is exported after the non-interactive return, so the VS Code "
                  "server never sees it (--fix writes the header without the variable)")
    print("CMH_HUB_TOKEN in this shell:", "yes" if os.environ.get("CMH_HUB_TOKEN") else "no")
    status(cfg)
    try:
        # Only Claude Code itself rewrites ~/.claude.json: the CLI or the binary the VS Code
        # extension runs. A bare "claude" match also catches editors with ~/.claude files open.
        lines = subprocess.run(["pgrep", "-a", "-u", str(os.getuid()), "-f", "claude"], capture_output=True,
                               text=True).stdout.splitlines()
        procs = []
        for line in lines:
            pid, _, cmd = line.partition(" ")
            exe = cmd.split(" ", 1)[0]
            if pid != str(os.getpid()) and (os.path.basename(exe) == "claude" or "anthropic.claude-code" in exe):
                procs.append((pid, cmd))
    except (OSError, AttributeError):
        procs = []
    if found and not fix:
        print("context-memory is registered; if /mcp still misses it, restart the VS Code window.")
        return 0
    if not fix:
        print("not registered at user scope. Close every Claude Code session and VS Code window on this "
              "machine, then run: python3 ~/.local/bin/cmh_push.py doctor --fix")
        return 1
    if procs:
        print(f"{len(procs)} Claude Code process(es) running; they would overwrite ~/.claude.json on exit:")
        for pid, cmd in procs[:8]:
            print(f"  {pid}  {cmd[:140]}")
        print("Close them (quit VS Code; or: kill " + " ".join(p for p, _ in procs) + ") and rerun --fix.")
        return 1
    target = files[0]
    data = json.loads(target.read_text()) if target.exists() else {}
    if target.exists():
        target.with_name(target.name + ".bak-cmh").write_text(target.read_text())
    # Literal token (file is 600): works where the VS Code server lacks shell variables.
    data.setdefault("mcpServers", {})["context-memory"] = {
        "type": "http", "url": cfg["hub_url"].rstrip("/") + "/mcp",
        "headers": {"Authorization": "Bearer " + token(cfg)}}
    tmp = target.with_name(target.name + ".tmp-cmh")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, target)
    print(f"registered context-memory in {target} (backup: {target.name}.bak-cmh). Open VS Code again; "
          "/mcp should list it as connected while the tunnel is up.")
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    cfg = config()
    if cmd == "doctor":
        return doctor(cfg, fix="--fix" in argv)
    if cmd == "hook-end":
        try:
            data = json.load(sys.stdin)
            if data.get("transcript_path"):
                enqueue(data["transcript_path"], data.get("session_id"))
            if data.get("cwd") and str(cfg.get("code", "1")).lower() not in ("0", "false", "no"):
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                with QUEUE.open("a") as fh:
                    fh.write(json.dumps({"code": data["cwd"], "project_dir": Path(
                        data.get("transcript_path") or "x/x").parent.name}) + "\n")
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
