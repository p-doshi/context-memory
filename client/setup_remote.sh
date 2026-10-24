#!/usr/bin/env bash
# Set up a Linux machine to use the memory hub through an SSH tunnel.
# Run from a bundle directory holding: cmh_push.py, claude_memory.md, cmh.token
# (and this script). The bundle is deleted afterwards, token included.
# Idempotent: safe to run again.
set -euo pipefail
PORT="${CMH_PORT:-8765}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p ~/.local/bin ~/.config/cmh ~/.claude
install -m 755 "$DIR/cmh_push.py" ~/.local/bin/cmh_push.py
if [ -f "$DIR/cmh.token" ]; then install -m 600 "$DIR/cmh.token" ~/.config/cmh/token; fi
chmod 600 ~/.config/cmh/token
printf '{"hub_url": "http://127.0.0.1:%s"}\n' "$PORT" > ~/.config/cmh/push.json

# The token reaches Claude Code through an environment variable, never a command
# line: on a shared server, other users can read process arguments, not environments.
if ! grep -q "CMH_HUB_TOKEN" ~/.bashrc 2>/dev/null; then
  printf '\n# memory hub token (file is chmod 600)\nexport CMH_HUB_TOKEN="$(cat ~/.config/cmh/token 2>/dev/null)"\n' >> ~/.bashrc
fi
export CMH_HUB_TOKEN="$(cat ~/.config/cmh/token)"

# Hooks (SessionStart sweep, SessionEnd push) and the memory-use guidance, merged
# into the user's Claude Code config with backups.
python3 - "$DIR/claude_memory.md" <<'PY'
import json, os, pathlib, sys
# Same home as the shell's ~ (Path.home() can differ, e.g. USERPROFILE on Windows).
home = pathlib.Path(os.environ.get("HOME") or pathlib.Path.home())
p = home / ".claude" / "settings.json"
settings = json.loads(p.read_text()) if p.exists() else {}
if p.exists():
    p.with_name("settings.json.bak-cmh").write_text(p.read_text())
hooks = settings.setdefault("hooks", {})
script = home / ".local" / "bin" / "cmh_push.py"
for event, arg in (("SessionStart", "hook-start"), ("SessionEnd", "hook-end")):
    cmd = f"python3 {script} {arg}"
    groups = hooks.setdefault(event, [])
    if not any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
p.write_text(json.dumps(settings, indent=2))
print("hooks merged into", p)

md = home / ".claude" / "CLAUDE.md"
block = "<!-- cmh:begin -->\n" + pathlib.Path(sys.argv[1]).read_text().strip() + "\n<!-- cmh:end -->\n"
text = md.read_text() if md.exists() else ""
if md.exists():
    md.with_name("CLAUDE.md.bak-cmh").write_text(text)
if "<!-- cmh:begin -->" in text:
    head, _, rest = text.partition("<!-- cmh:begin -->")
    _, _, tail = rest.partition("<!-- cmh:end -->\n")
    text = head + block + tail
else:
    text = (text.rstrip() + "\n\n" if text.strip() else "") + block
md.write_text(text)
print("memory guidance in", md)
PY

if command -v claude >/dev/null 2>&1; then
  claude mcp remove --scope user context-memory >/dev/null 2>&1 || true
  # Single quotes: the literal ${CMH_HUB_TOKEN} is stored and expanded by Claude Code.
  claude mcp add --transport http --scope user context-memory "http://127.0.0.1:${PORT}/mcp" \
    --header 'Authorization: Bearer ${CMH_HUB_TOKEN}'
  echo "--- claude mcp list (context-memory should show as connected while the tunnel is up):"
  claude mcp list 2>&1 | grep -i "context-memory" || true
else
  echo "claude is not on PATH in this shell; run the 'claude mcp add' line from docs/remote-setup.md later."
fi

echo "--- push client:"
python3 ~/.local/bin/cmh_push.py status

# Remove the bundle (it held the token) unless it is the repository checkout.
if [ ! -d "$DIR/../.git" ]; then rm -rf "$DIR"; echo "bundle removed"; fi
