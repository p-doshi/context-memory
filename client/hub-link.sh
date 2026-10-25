#!/usr/bin/env bash
# Keep an SSH link from this Linux machine to the hub machine, so that
# http://127.0.0.1:8765 here reaches the hub there. For machines that connect
# to the hub (a second laptop), as opposed to servers the hub connects to.
#
#   bash hub-link.sh key                         # 1. create the key, print the public half
#   bash hub-link.sh install HOST FINGERPRINT    # 2. after the hub side accepted the key
#   bash hub-link.sh status | stop | uninstall
#
# HOST is the hub machine's name or address on the LAN (e.g. mylaptop.local);
# FINGERPRINT is the SHA256:... host key printed by scripts/enable-ssh-link.ps1,
# checked before anything is trusted. The key may only forward to the hub port
# (enforced on the hub side), and the link runs as a systemd user unit.
set -euo pipefail
PORT="${CMH_PORT:-8765}"
USER_ON_HUB="${CMH_LINK_USER:-cmhlink}"
KEY="$HOME/.ssh/cmh_hub_ed25519"
KNOWN="$HOME/.ssh/cmh_hub_known_hosts"
UNIT="$HOME/.config/systemd/user/cmh-hub-link.service"

case "${1:-}" in
key)
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "cmh-link@$(hostname)" -f "$KEY"
  echo "public key (safe to share; paste it into enable-ssh-link.ps1 -PublicKey):"
  cat "$KEY.pub"
  ;;
install)
  HOST="${2:?usage: hub-link.sh install HOST FINGERPRINT}"
  FP="${3:?usage: hub-link.sh install HOST FINGERPRINT}"
  [ -f "$KEY" ] || { echo "run 'hub-link.sh key' first" >&2; exit 2; }
  tmp="$(mktemp)"
  ssh-keyscan -t ed25519 "$HOST" 2>/dev/null > "$tmp"
  got="$(ssh-keygen -lf "$tmp" | awk '{print $2}')"
  if [ "$got" != "$FP" ]; then
    rm -f "$tmp"
    echo "host key mismatch: expected $FP, got ${got:-nothing}. Not trusting $HOST." >&2
    exit 1
  fi
  mv "$tmp" "$KNOWN" && chmod 600 "$KNOWN"
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<EOF
[Unit]
Description=SSH link to the context-memory hub ($HOST)
After=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N -i $KEY -o UserKnownHostsFile=$KNOWN -o StrictHostKeyChecking=yes \\
  -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \\
  -L 127.0.0.1:$PORT:127.0.0.1:$PORT $USER_ON_HUB@$HOST
Restart=always
RestartSec=20

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now cmh-hub-link.service
  # Keep the link up without an open login session (allowed for one's own user
  # on most distributions; harmless if refused).
  loginctl enable-linger "$USER" 2>/dev/null || true
  sleep 3
  if curl -fsS -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/client/claude_memory.md" | grep -q 200; then
    echo "link up: http://127.0.0.1:$PORT reaches the hub"
  else
    echo "link not up yet; see: journalctl --user -u cmh-hub-link -n 20"
  fi
  ;;
status)
  systemctl --user --no-pager status cmh-hub-link.service | head -5 || true
  ;;
stop)
  systemctl --user stop cmh-hub-link.service
  ;;
uninstall)
  systemctl --user disable --now cmh-hub-link.service 2>/dev/null || true
  rm -f "$UNIT" "$KNOWN"
  systemctl --user daemon-reload
  echo "link removed (key kept at $KEY; delete it to revoke from this side)"
  ;;
*)
  sed -n '2,14p' "$0"; exit 2
  ;;
esac
