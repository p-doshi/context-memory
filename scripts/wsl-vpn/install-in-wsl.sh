#!/usr/bin/env bash
# Install Cisco Secure Client for Linux inside a WSL2 distribution, so the
# organisation's VPN runs in the VM only and the Windows host keeps its LAN and
# direct internet routes. Run as root inside WSL (wsl.exe -d Ubuntu -u root ...).
#
#   bash install-in-wsl.sh [INSTALLER.tar.gz] [PROFILE.xml]
#
# Defaults: the newest cisco-secure-client-linux64-*.tar.gz in a Windows
# Downloads folder, and the VPN profile the Windows client already uses.
# Needs network access from WSL (disconnect the Windows VPN client first).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (wsl.exe -d <distro> -u root bash $0)" >&2; exit 2; }

TARBALL="${1:-$(ls -t /mnt/c/Users/*/Downloads/cisco-secure-client-linux64-*.tar.gz /mnt/c/Users/*/Downloads/anyconnect-linux64-*.tar.gz 2>/dev/null | head -1 || true)}"
PROFILE="${2:-/mnt/c/ProgramData/Cisco/Cisco Secure Client/VPN/Profile/General_Access_client_profile.xml}"
[ -f "$TARBALL" ] || { echo "no cisco-secure-client-linux64-*.tar.gz or anyconnect-linux64-*.tar.gz in Downloads; put the Linux installer there" >&2; exit 1; }
echo "installer: $TARBALL"

if ! timeout 8 bash -c "</dev/tcp/archive.ubuntu.com/80" 2>/dev/null; then
  echo "WSL has no network. Disconnect the Windows VPN client, then run this again." >&2
  exit 1
fi

# GUI login window (WSLg) and its embedded browser for single sign-on.
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libgtk-3-0t64 libwebkit2gtk-4.1-0 netcat-openbsd >/dev/null

work="$(mktemp -d)"
tar -xzf "$TARBALL" -C "$work"
installer="$(find "$work" -path '*/vpn/vpn_install.sh' | head -1)"
[ -n "$installer" ] || { echo "vpn/vpn_install.sh not found in the tarball" >&2; exit 1; }
# The installer asks to accept Cisco's licence; accepting is the point of running it.
(cd "$(dirname "$installer")" && yes | bash ./vpn_install.sh >/dev/null)
rm -rf "$work"

if [ -f "$PROFILE" ]; then
  install -m 644 "$PROFILE" /opt/cisco/secureclient/vpn/profile/
  echo "profile: $(basename "$PROFILE")"
fi
systemctl enable --now vpnagentd >/dev/null 2>&1 || systemctl restart vpnagentd
systemctl is-active vpnagentd
echo "installed. Log in with: wsl.exe -d $WSL_DISTRO_NAME -- /opt/cisco/secureclient/bin/vpnui"
