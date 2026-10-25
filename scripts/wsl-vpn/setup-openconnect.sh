#!/usr/bin/env bash
# Dalhousie VPN inside WSL2 with openconnect + openconnect-sso + vpn-slice, after
# P. Selinger's guide (https://www.mathstat.dal.ca/~selinger/downloads/dal-vpn-setup.html,
# updated 2026/05/06; tested there on Ubuntu 24.04, Python 3.12, openconnect-sso 0.8.1).
#
# Differences from the guide, because this runs in a WSL VM:
#   - no NetworkManager dispatcher (WSL has none); the context-memory daemon runs
#     /usr/local/bin/dal-vpn in the foreground and restarts it when it exits;
#   - the passwordless sudo rule for openconnect is kept: inside this VM it adds
#     nothing, since the Windows user already gets root with `wsl.exe -u root`;
#   - extra Qt/X libraries for the login browser on a minimal Ubuntu.
#
# Run as root inside WSL:  wsl.exe -d Ubuntu -u root bash setup-openconnect.sh [USER]
# Needs network from WSL (disconnect the Windows Cisco client first). Idempotent.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root: wsl.exe -d <distro> -u root bash $0" >&2; exit 2; }
U="${1:-$(getent passwd 1000 | cut -d: -f1)}"
H="$(getent passwd "$U" | cut -d: -f6)"
as_user() { sudo -u "$U" -H env PATH="$H/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }

if ! timeout 8 bash -c "</dev/tcp/archive.ubuntu.com/80" 2>/dev/null; then
  echo "WSL has no network. Disconnect the Windows Cisco client, then run this again." >&2
  exit 1
fi

echo "== 1. packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openconnect pipx netcat-openbsd \
  libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libxkbcommon0 libxkbcommon-x11-0 \
  libegl1 libgl1 libasound2t64 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 \
  libxcb-xinerama0 libxcb-randr0 libxcb-render-util0 libxcb-image0 libdbus-1-3 fonts-dejavu-core \
  libxkbfile1 libpulse0 >/dev/null
as_user pipx install vpn-slice >/dev/null 2>&1 || as_user pipx upgrade vpn-slice >/dev/null 2>&1 || true
ln -sf "$H/.local/bin/vpn-slice" /usr/local/bin/vpn-slice
openconnect --version | head -1
vpn-slice --version

echo "== 2. openconnect-sso"
as_user pipx install openconnect-sso >/dev/null 2>&1 || true
VENV="$H/.local/share/pipx/venvs/openconnect-sso"
as_user "$VENV/bin/python" -m pip install -q "setuptools<72"
as_user "$VENV/bin/python" -c "import pkg_resources; print('pkg_resources ok')"

echo "== 3. patches"
PKG="$(as_user "$VENV/bin/python" -c 'import openconnect_sso, os; print(os.path.dirname(openconnect_sso.__file__))')"
as_user "$VENV/bin/python" - "$PKG" <<'PY'
import pathlib, sys
pkg = pathlib.Path(sys.argv[1])

def patch(path, old, new, done_marker):
    text = path.read_text()
    if done_marker in text:
        print(f"already patched: {path.name}")
        return
    if text.count(old) != 1:
        sys.exit(f"cannot patch {path}: expected text not found exactly once (openconnect-sso changed?)")
    path.write_text(text.replace(old, new))
    print(f"patched: {path.name}")

wp = pkg / "browser" / "webengine_process.py"
# Patch 1a: a named, persistent profile, so the SSO session survives (~2 weeks).
patch(wp,
      "        cookie_store = self.page().profile().cookieStore()",
      '        self._profile = QWebEngineProfile("openconnect-sso", self)\n'
      "        self.setPage(QWebEnginePage(self._profile, self))\n"
      "        cookie_store = self.page().profile().cookieStore()",
      'QWebEngineProfile("openconnect-sso", self)')
# Patch 1b: the two lines appended to _on_cookie_added.
patch(wp,
      "        self._on_update(SetCookie(to_str(cookie.name()), to_str(cookie.value())))",
      "        self._on_update(SetCookie(to_str(cookie.name()), to_str(cookie.value())))\n"
      "        cookie2 = QNetworkCookie()\n"
      "        self.page().profile().cookieStore().deleteCookie(cookie2)",
      "cookie2 = QNetworkCookie()")
# Patch 2: pass unknown arguments (e.g. --script) through to openconnect.
patch(pkg / "cli.py",
      "    args = parser.parse_args()",
      "    args, extra = parser.parse_known_args()\n"
      "    args.openconnect_args = (args.openconnect_args or []) + extra",
      "parser.parse_known_args()")
# The names used by patch 1 must be importable in webengine_process.py.
text = wp.read_text()
for name in ("QWebEngineProfile", "QWebEnginePage", "QNetworkCookie"):
    if name not in text.split("class ")[0]:
        print(f"warning: {name} is not imported at the top of {wp.name}; check the patch")
PY

echo "== 4. sudo rule for openconnect"
printf '%s ALL=(ALL) NOPASSWD: /usr/sbin/openconnect\n%s ALL=(ALL) NOPASSWD: /usr/bin/pkill openconnect\n' "$U" "$U" \
  > /etc/sudoers.d/openconnect
chmod 440 /etc/sudoers.d/openconnect
visudo -cf /etc/sudoers.d/openconnect

echo "== 5. dal-vpn command (foreground; the daemon restarts it)"
# Dal's server offers only the embedded-browser SAML flow (openconnect
# --external-browser gets "No SSO handler"), so the login runs in
# openconnect-sso's Qt WebEngine window through WSLg.
rm -f /usr/local/bin/win-browser
cat > /usr/local/bin/dal-vpn <<EOF
#!/bin/bash
# Dal VPN for this WSL VM. Foreground, so a supervisor (the context-memory
# daemon) can restart it. Log: /tmp/vpn.log
#
# What it took (2026-10-25):
# - Dal's server rejects the default AnyConnect version 4.7.00136 after login
#   ("Failed to create session manager entry"); report the Windows client's 5.1.18.314.
# - WSLg: Chromium's GPU path renders a blank window and its sandbox stalls the
#   renderer inside the VM; draw in software without the sandbox (the page is
#   Dal's and Microsoft's login only).
# - Microsoft's sign-in offers passkeys first, which cannot reach Windows Hello from
#   here and hang; with WebAuthn disabled it falls back to password + code.
# - openconnect-sso drops the first argument after "--" (the guide's alias loses its
#   -b there). Without "--", the patched parser passes unknown options through, so
#   openconnect's options go in single-token form (--opt=value).
export QTWEBENGINE_DISABLE_SANDBOX=1 LIBGL_ALWAYS_SOFTWARE=1 QT_QUICK_BACKEND=software
export QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu --disable-gpu-compositing --no-sandbox --disable-blink-features=WebAuth,WebAuthenticationConditionalUI --disable-features=WebAuthenticationConditionalUI"
exec "$H/.local/bin/openconnect-sso" --ac-version 5.1.18.314 --server vpn.its.dal.ca --authgroup DAL \\
  --reconnect-timeout=30 --force-dpd=10 "--script=vpn-slice 129.173.0.0/16" >>/tmp/vpn.log 2>&1
EOF
chmod 755 /usr/local/bin/dal-vpn
as_user "$H/.local/bin/openconnect-sso" --version
echo "done. First login (browser window: NetID, password, 6-digit code): wsl.exe -d $WSL_DISTRO_NAME -- /usr/local/bin/dal-vpn"
