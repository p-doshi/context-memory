"""Prepare web/site for static hosting elsewhere (e.g. Vercel), and publish it.

web/site/vercel.json is a template with API_HOST as a placeholder. The deploy
folder gets the real API host in vercel.json (CSP connect-src) and in
config.json (where site.js sends /api/* calls). Only files under web/site are
copied: no memory content leaves the machine in this step.

publish() keeps a clone of a site repository (e.g. a private GitHub repo that
Vercel deploys from), refreshes it with the deploy files and pushes when
anything changed, so a new tunnel address reaches the hosted site without a
Vercel token.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from context_memory.util import NO_WINDOW

SITE = Path(__file__).resolve().parents[1] / "web" / "site"
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def write_deploy(out: Path, api_host: str) -> list[str]:
    api_host = api_host.strip().lower().removeprefix("https://").rstrip("/")
    if not _HOST.match(api_host):
        raise ValueError(f"not a host name: {api_host!r} (e.g. api.example.org)")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.iterdir():  # keep a repository's .git
        if old.name != ".git":
            shutil.rmtree(old) if old.is_dir() else old.unlink()
    shutil.copytree(SITE, out, dirs_exist_ok=True)
    fonts = SITE.parent / "fonts"  # self-hosted IBM Plex, with its licence
    if fonts.is_dir():
        shutil.copytree(fonts, out / "fonts", dirs_exist_ok=True)
    vj = out / "vercel.json"
    vj.write_text(vj.read_text().replace("API_HOST", api_host))
    (out / "config.json").write_text(json.dumps({"api": f"https://{api_host}"}) + "\n")
    # Agent-facing documents link the API; on a static host it lives at api_host.
    for name in ("llms.txt", "overview.md", "guide.md"):
        f = out / name
        if f.exists():
            text = f.read_text(encoding="utf-8")
            for prefix in ("](", "`GET ", "`POST "):
                text = text.replace(prefix + "/api/", f"{prefix}https://{api_host}/api/")
            f.write_text(text, encoding="utf-8")
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(out).parts)


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120,
                         creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout


def publish(home: Path, api_host: str, repo_url: str) -> dict:
    """Refresh the site repository clone at <home>/public/site-repo and push."""
    clone = Path(home) / "public" / "site-repo"
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        _git(clone.parent, "clone", "--quiet", repo_url, clone.name)
    else:
        try:
            _git(clone, "pull", "--quiet", "--ff-only")
        except RuntimeError as e:  # a fresh, still empty repository has nothing to pull
            if "no tracking information" not in str(e) and "couldn't find remote ref" not in str(e):
                raise
    files = write_deploy(clone, api_host)
    _git(clone, "add", "-A")
    if not _git(clone, "status", "--porcelain").strip():
        return {"pushed": False, "files": len(files)}
    # The owner's own git identity: Vercel may skip deploys of private-repo commits
    # whose author is not on the account.
    _git(clone, "commit", "--quiet", "-m", f"site update (api {api_host})")
    _git(clone, "push", "--quiet", "origin", "HEAD")
    return {"pushed": True, "files": len(files)}
