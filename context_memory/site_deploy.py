"""Prepare web/site for static hosting elsewhere (e.g. Vercel).

web/site/vercel.json is a template with API_HOST as a placeholder. The deploy
folder gets the real API host in vercel.json (CSP connect-src) and in
config.json (where site.js sends /api/* calls). Only files under web/site are
copied: no data leaves the machine in this step.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

SITE = Path(__file__).resolve().parents[1] / "web" / "site"
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def write_deploy(out: Path, api_host: str) -> list[str]:
    api_host = api_host.strip().lower().removeprefix("https://").rstrip("/")
    if not _HOST.match(api_host):
        raise ValueError(f"not a host name: {api_host!r} (e.g. api.example.org)")
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SITE, out)
    vj = out / "vercel.json"
    vj.write_text(vj.read_text().replace("API_HOST", api_host))
    (out / "config.json").write_text(json.dumps({"api": f"https://{api_host}"}) + "\n")
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
