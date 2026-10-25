"""Route selected SSH hosts through a VPN that runs inside WSL2.

An organisation's VPN client on the Windows host can block the home LAN
(e.g. a profile with local LAN access disabled). Running the VPN inside a WSL2
VM instead confines it, and ssh on Windows reaches only the hosts that need the
VPN through the VM:

    Host bigdata1.example.edu
      ProxyCommand C:\\Windows\\System32\\wsl.exe -d Ubuntu -- nc %h %p  # cmh:wsl-vpn

`cmh vpn-route wsl` adds that line to every Host block matching the patterns;
`cmh vpn-route host` removes it again. A backup of the config is written first.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

MARK = "# cmh:wsl-vpn"


def _proxy_line(distro: str) -> str:
    return f"  ProxyCommand C:\\Windows\\System32\\wsl.exe -d {distro} -- nc %h %p  {MARK}"


def set_route(config: Path, patterns: list[str], mode: str, distro: str = "Ubuntu") -> list[str]:
    """mode "wsl" or "host". Returns the Host names changed."""
    if mode not in ("wsl", "host"):
        raise ValueError("mode is wsl or host")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", distro):
        raise ValueError(f"bad distribution name {distro!r}")
    text = config.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if MARK not in l]  # start from the plain config
    out, changed = [], []
    for line in lines:
        out.append(line)
        m = re.match(r"\s*Host\s+(.+)$", line, re.I)
        if m and mode == "wsl":
            names = m.group(1).split()
            if any(fnmatch.fnmatch(n, p) for n in names for p in patterns):
                out.append(_proxy_line(distro))
                changed.append(names[0])
    if mode == "host":
        changed = [l.strip() for l in text.splitlines() if MARK in l]
    new = "\n".join(out) + "\n"
    if new != text:
        config.with_name(config.name + ".bak-cmh").write_text(text, encoding="utf-8")
        config.write_text(new, encoding="utf-8")
    return changed


def current(config: Path) -> str:
    return "wsl" if config.exists() and MARK in config.read_text(encoding="utf-8") else "host"
