#!/usr/bin/env bash
# Set up a Linux machine to use the memory hub through an SSH tunnel.
# Expects ~/cmh_push.py and ~/cmh.token copied here first (see docs/remote-setup.md).
# Idempotent: safe to run again.
set -euo pipefail
PORT="${CMH_PORT:-8765}"

mkdir -p ~/.local/bin ~/.config/cmh
[ -f ~/cmh_push.py ] && install -m 755 ~/cmh_push.py ~/.local/bin/cmh_push.py && rm -f ~/cmh_push.py
[ -f ~/cmh.token ] && install -m 600 ~/cmh.token ~/.config/cmh/token && rm -f ~/cmh.token
chmod 600 ~/.config/cmh/token
printf '{"hub_url": "http://127.0.0.1:%s"}\n' "$PORT" > ~/.config/cmh/push.json

# The token reaches Claude Code through an environment variable, never a command
# line: on a shared server, other users can read process arguments, not environments.
if ! grep -q "CMH_HUB_TOKEN" ~/.bashrc 2>/dev/null; then
  printf '\n# memory hub token (file is chmod 600)\nexport CMH_HUB_TOKEN="$(cat ~/.config/cmh/token 2>/dev/null)"\n' >> ~/.bashrc
fi
export CMH_HUB_TOKEN="$(cat ~/.config/cmh/token)"

# Merge SessionStart/SessionEnd hooks into ~/.claude/settings.json (backup kept).
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".claude" / "settings.json"
p.parent.mkdir(exist_ok=True)
settings = json.loads(p.read_text()) if p.exists() else {}
if p.exists():
    p.with_name("settings.json.bak-cmh").write_text(p.read_text())
hooks = settings.setdefault("hooks", {})
script = pathlib.Path.home() / ".local" / "bin" / "cmh_push.py"
for event, arg in (("SessionStart", "hook-start"), ("SessionEnd", "hook-end")):
    cmd = f"python3 {script} {arg}"
    groups = hooks.setdefault(event, [])
    if not any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
p.write_text(json.dumps(settings, indent=2))
print("hooks merged into", p)
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
