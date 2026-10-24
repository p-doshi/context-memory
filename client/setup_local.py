"""Wire every local Claude Code config directory to the memory hub.

For each config dir (default: ~/.claude, plus any given with --config-dir):
  - SessionStart hook that injects the memory context pack (cmh_push.py context)
  - the memory-use guidance block in <config dir>/CLAUDE.md (between markers)
  - the hub registered as a user-scope MCP server, token read from the
    CMH_HUB_TOKEN environment variable at connection time
Backups are written next to every file changed. Idempotent.

Usage (Windows example):
  python client/setup_local.py --config-dir %USERPROFILE%/.claude --config-dir %USERPROFILE%/.claude-personal
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BEGIN, END = "<!-- cmh:begin -->", "<!-- cmh:end -->"


def backup(p: Path) -> None:
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + ".bak-cmh"))


def merge_hook(config_dir: Path, command: str) -> None:
    p = config_dir / "settings.json"
    settings = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    groups = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    # Replace any earlier cmh hook (e.g. an old path) instead of stacking copies.
    groups[:] = [g for g in groups
                 if not any("cmh_push.py" in h.get("command", "") for h in g.get("hooks", []))]
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    backup(p)
    p.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def merge_guidance(config_dir: Path) -> None:
    md = config_dir / "CLAUDE.md"
    block = f"{BEGIN}\n{(HERE / 'claude_memory.md').read_text(encoding='utf-8').strip()}\n{END}\n"
    text = md.read_text(encoding="utf-8") if md.exists() else ""
    if BEGIN in text:
        head, _, rest = text.partition(BEGIN)
        text = head + block + rest.partition(END + "\n")[2]
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + block
    backup(md)
    md.write_text(text, encoding="utf-8")


def register_mcp(config_dir: Path, hub_url: str) -> str:
    claude = shutil.which("claude")
    if not claude:
        return "claude not on PATH; MCP server not registered"
    env = {**os.environ}
    if config_dir.resolve() != (Path.home() / ".claude").resolve():
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([claude, "mcp", "remove", "--scope", "user", "context-memory"], env=env,
                   capture_output=True, text=True, creationflags=flags)
    add = subprocess.run([claude, "mcp", "add", "--transport", "http", "--scope", "user",
                          "context-memory", f"{hub_url.rstrip('/')}/mcp",
                          "--header", "Authorization: Bearer ${CMH_HUB_TOKEN}"],
                         env=env, capture_output=True, text=True, creationflags=flags)
    if add.returncode != 0:
        return f"mcp add failed: {add.stderr.strip()[:300]}"
    listed = subprocess.run([claude, "mcp", "list"], env=env, capture_output=True, text=True,
                            timeout=120, creationflags=flags)
    line = next((l for l in listed.stdout.splitlines() if "context-memory" in l), "(not listed)")
    return line.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", action="append", type=Path, default=[])
    ap.add_argument("--hub-url", default=os.environ.get("CMH_HUB_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter for the hook (absolute path)")
    args = ap.parse_args()
    if not os.environ.get("CMH_HUB_TOKEN"):
        raise SystemExit("set CMH_HUB_TOKEN (the device token) before running")
    dirs = args.config_dir or [Path.home() / ".claude"]
    py, script = Path(args.python).as_posix(), (HERE / "cmh_push.py").as_posix()
    command = f'"{py}" "{script}" context'
    for d in dirs:
        d = d.expanduser()
        d.mkdir(parents=True, exist_ok=True)
        merge_hook(d, command)
        merge_guidance(d)
        print(f"{d}: hook + guidance installed; mcp: {register_mcp(d, args.hub_url)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
