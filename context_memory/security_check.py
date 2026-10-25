"""`cmh security-check`: one report on the security posture of this install.

Each check returns PASS, WARN or FAIL with a one-line reason. Checks that need
administrator rights report UNKNOWN instead of guessing.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from context_memory.config import Settings
from context_memory.util import NO_WINDOW

BROAD_PRINCIPALS = ("Everyone", "BUILTIN\\Users", "Authenticated Users", "NT AUTHORITY\\Authenticated Users")


def _run(args: list[str], timeout: int = 20) -> str:
    """Output of a probe, or "" if it fails or is slow (reported as UNKNOWN)."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                             creationflags=NO_WINDOW)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _ps(command: str, timeout: int = 20) -> str:
    return _run(["powershell", "-NoProfile", "-Command", command], timeout)


def _acl_broad(path: Path) -> list[str]:
    """Principals beyond the owner/SYSTEM/Administrators with access to path."""
    if os.name != "nt" or not path.exists():
        return []
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    return sorted({p for p in BROAD_PRINCIPALS if p in out})


def run(settings: Settings, svc=None) -> list[dict]:
    home = settings.data_dir.parent
    checks: list[dict] = []

    def add(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail})

    # Hub reachable only on loopback.
    add("hub bind address", "PASS" if settings.hub_host in ("127.0.0.1", "::1") else "FAIL",
        f"hub_host={settings.hub_host}")
    if os.name == "nt":
        listen = _ps(f"Get-NetTCPConnection -State Listen -LocalPort {settings.hub_port} "
                     "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalAddress")
        addrs = sorted(set(listen.split()))
        if not addrs:
            add("hub listening sockets", "WARN", "hub not listening (daemon down?)")
        else:
            add("hub listening sockets", "PASS" if set(addrs) <= {"127.0.0.1", "::1"} else "FAIL",
                f"port {settings.hub_port} on {addrs}")
        # netsh: fast and needs no admin (Get-NetFirewallProfile hung > 30 s here).
        fw = _run(["netsh", "advfirewall", "show", "allprofiles", "state"])
        states = [l.split()[-1].upper() for l in fw.splitlines() if l.strip().lower().startswith("state")]
        add("windows firewall",
            "UNKNOWN" if not states else ("PASS" if all(x == "ON" for x in states) else "FAIL"),
            f"profiles: {states}" if states else "could not read firewall state")
        rule = _run(["netsh", "advfirewall", "firewall", "show", "rule",
                     "name=Block context-memory hub inbound"])
        present = "Block context-memory hub inbound" in rule
        add("firewall rule for hub port", "PASS" if present else "WARN",
            "inbound block rule present" if present
            else "no explicit inbound block rule (defence in depth; needs admin to add)")
        enc = _ps("(Get-CimInstance -Namespace root/CIMv2/Security/MicrosoftVolumeEncryption "
                  "-ClassName Win32_EncryptableVolume -Filter \"DriveLetter='C:'\" "
                  "-ErrorAction SilentlyContinue).ProtectionStatus")
        add("disk encryption (C:)", {"1": "PASS", "0": "WARN"}.get(enc, "UNKNOWN"),
            {"1": "protection on", "0": "protection off: data at rest is readable from the disk"}
            .get(enc, "needs administrator rights to read"))

    # File permissions on sensitive locations.
    ssh_key = Path.home() / ".ssh" / "cmh_tunnel_ed25519"
    for label, p in (("data dir", settings.data_dir), ("config dir", home / "config"),
                     ("backup dir", home / "backup"), ("browser profiles", home / "browser"),
                     ("tunnel ssh key", ssh_key)):
        if not p.exists():
            continue
        broad = _acl_broad(p)
        add(f"permissions: {label}", "WARN" if broad else "PASS",
            f"also accessible to {broad}" if broad else "owner/SYSTEM/Administrators only")

    # Leftover device bundles hold live tokens.
    devices_dir = home / "config" / "devices"
    bundles = sorted(p.name for p in devices_dir.glob("*-bundle")) if devices_dir.exists() else []
    add("leftover device bundles", "WARN" if bundles else "PASS",
        f"{bundles} still hold tokens; delete once copied" if bundles else "none")

    # Network behaviour of the model loader.
    add("model loading offline", "PASS" if not settings.allow_model_download else "WARN",
        "HF offline mode; no network on model load" if not settings.allow_model_download
        else "CMH_ALLOW_MODEL_DOWNLOAD is set: model loads contact huggingface.co")

    # Repository guard against committing private strings.
    repo = Path(__file__).resolve().parents[1]
    hook = repo / ".git" / "hooks" / "pre-commit"
    add("pre-commit privacy guard", "PASS" if hook.exists() and "check_private" in hook.read_text()
        else "WARN", "installed" if hook.exists() else "run: python scripts/check_private.py --install")

    if svc is not None:
        chain = svc.sqlite.verify_audit_chain()
        add("audit log chain", "PASS" if chain["ok"] else "FAIL",
            f"{chain.get('rows')} chained rows" if chain["ok"]
            else f"broken at seq {chain.get('broken_at')}: {chain.get('reason')}")
        now = time.time()
        stale = []
        for d in svc.sqlite.conn.execute(
                "SELECT name, last_seen, scopes FROM devices WHERE revoked_at IS NULL").fetchall():
            seen = d["last_seen"]
            if not seen or now - time.mktime(time.strptime(seen[:19], "%Y-%m-%dT%H:%M:%S")) > 30 * 86400:
                stale.append(d["name"])
        add("active devices", "WARN" if stale else "PASS",
            f"unused for 30+ days (revoke?): {stale}" if stale else "all active devices seen recently")

    snaps = sorted((home / "backup").glob("snapshot-*/manifest.json")) if (home / "backup").exists() else []
    if not snaps:
        add("backups", "WARN", "no snapshot yet")
    else:
        m = json.loads(snaps[-1].read_text())
        age_h = (time.time() - snaps[-1].stat().st_mtime) / 3600
        ok = m.get("ok") and age_h < 48
        add("backups", "PASS" if ok else "WARN",
            f"latest {snaps[-1].parent.name}, {age_h:.0f} h old, verified={m.get('ok')}; same disk as data")
    return checks
