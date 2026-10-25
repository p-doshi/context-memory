#!/usr/bin/env bash
# Keep an SSH link from this Linux machine to the hub machine, so that
# http://127.0.0.1:8765 here reaches the hub there. For machines that connect
# to the hub (a second laptop), as opposed to servers the hub connects to.
#
#   Direct, on the same LAN as the hub machine:
#   bash hub-link.sh key                         # 1. create the key, print the public half
#   bash hub-link.sh install HOST FINGERPRINT    # 2. after the hub side accepted the key
#
#   Through a server the hub keeps a tunnel to (works anywhere, and when a VPN
#   on the hub machine blocks LAN traffic):
#   bash hub-link.sh via USER@SERVER             # uses your normal SSH login to SERVER
#
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

write_unit() {  # $1 description, $2 ssh arguments
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<EOF
[Unit]
Description=SSH link to the context-memory hub ($1)
After=network-online.target

[Service]
ExecStart=/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \\
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \\
  -L 127.0.0.1:$PORT:127.0.0.1:$PORT $2
Restart=always
RestartSec=20

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now cmh-hub-link.service
  systemctl --user restart cmh-hub-link.service
  # Keep the link up without an open login session (allowed for one's own user
  # on most distributions; harmless if refused).
  loginctl enable-linger "$USER" 2>/dev/null || true
  sleep 4
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/client/claude_memory.md" 2>/dev/null; then
    echo "link up: http://127.0.0.1:$PORT reaches the hub"
  else
    echo "link not up yet; see: journalctl --user -u cmh-hub-link -n 20 --no-pager"
  fi
}

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
  ssh-keyscan -T 5 -t ed25519 "$HOST" 2>/dev/null > "$tmp" || true
  got="$(ssh-keygen -lf "$tmp" 2>/dev/null | awk '{print $2}' || true)"
  if [ -z "$got" ]; then
    rm -f "$tmp"
    echo "cannot reach SSH on $HOST port 22 (a VPN on the hub machine may block LAN traffic;" >&2
    echo "then use: bash hub-link.sh via USER@SERVER)" >&2
    exit 1
  fi
  if [ "$got" != "$FP" ]; then
    rm -f "$tmp"
    echo "host key mismatch: expected $FP, got $got. Not trusting $HOST." >&2
    exit 1
  fi
  mv "$tmp" "$KNOWN" && chmod 600 "$KNOWN"
  write_unit "$HOST" "-i $KEY -o UserKnownHostsFile=$KNOWN -o StrictHostKeyChecking=yes $USER_ON_HUB@$HOST"
  ;;
via)
  SERVER="${2:?usage: hub-link.sh via USER@SERVER}"
  # The hub keeps 127.0.0.1:$PORT open on SERVER through its reverse tunnel; forward to it.
  if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER" true 2>/dev/null; then
    echo "cannot log in to $SERVER without a prompt. Check the VPN is connected, then run" >&2
    echo "  ssh $SERVER true" >&2
    echo "once (accept the host key; if it asks for a password, run: ssh-copy-id $SERVER)." >&2
    exit 1
  fi
  write_unit "via $SERVER" "$SERVER"
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
  sed -n "2,22p" "$0"; exit 2
  ;;
esac
