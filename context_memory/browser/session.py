"""Logged-in browser sessions for web accounts (claude.ai, perplexity.ai).

Each account gets its own persistent Microsoft Edge profile under
<home>/browser/<label>. The owner logs in once in a visible window
(`cmh browser login <label>`); later syncs reuse the profile's cookies
headlessly. The harness never sees or stores a password.

These sites' internal endpoints are undocumented and can change without
notice; automated use may also conflict with their terms of service. The
owner accepted that risk on 2026-10-24.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOGIN_URLS = {
    "claude": "https://claude.ai/login",
    "perplexity": "https://www.perplexity.ai/",
}


def profile_dir(home: Path, label: str) -> Path:
    return home / "browser" / label


@contextmanager
def browser_context(home: Path, label: str, headless: bool = True) -> Iterator:
    from playwright.sync_api import sync_playwright

    profile = profile_dir(home, label)
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile), channel="msedge", headless=headless,
            viewport={"width": 1280, "height": 900},
        )
        try:
            yield ctx
        finally:
            ctx.close()


def login(home: Path, label: str, provider: str) -> None:
    """Open a visible window at the provider's login page; return when it is closed."""
    with browser_context(home, label, headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URLS[provider])
        print(f"Log in to {provider} as '{label}' in the browser window, then close it.")
        while ctx.pages:
            time.sleep(1)


def probe(home: Path, label: str, url: str, seconds: int = 20, headless: bool = False) -> Path:
    """Record which JSON endpoints a page calls, keeping only URLs, status codes
    and key names (never values), to design a parser without copying data."""
    seen: list[dict] = []

    def shape(obj, depth=0):
        if depth > 3:
            return "..."
        if isinstance(obj, dict):
            return {k: shape(v, depth + 1) for k, v in list(obj.items())[:40]}
        if isinstance(obj, list):
            return [shape(obj[0], depth + 1)] if obj else []
        return type(obj).__name__

    def on_response(resp):
        if "json" not in (resp.headers.get("content-type") or ""):
            return
        try:
            body = resp.json()
        except Exception:
            return
        seen.append({"url": resp.url.split("?")[0], "method": resp.request.method,
                     "status": resp.status, "shape": shape(body)})

    with browser_context(home, label, headless=headless) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_response)
        page.goto(url)
        page.wait_for_timeout(seconds * 1000)
    out = home / "browser" / f"probe-{label}.json"
    out.write_text(json.dumps(seen, indent=2))
    return out
