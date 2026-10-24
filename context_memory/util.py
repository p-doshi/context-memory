"""Small shared helpers: ids, hashes, timestamps, validation."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

SOURCES = {"claude", "chatgpt", "perplexity", "cursor", "manual", "git", "file", "web"}
KINDS = {
    "prompt", "response", "decision", "note", "code",
    "paper", "experiment", "config", "ui_spec", "chat",
}
# Context-relative tiers from the contextual-integrity addendum (v0.3, §4).
SENSITIVITIES = {
    "public", "task-minimal", "contact", "location-precise", "schedule",
    "relationship", "financial", "credential", "research-private",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str, n: int = 16) -> str:
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{h[:n]}"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


_SLUG_BAD = re.compile(r"[^a-z0-9._-]+")


def slugify(name: str | None, fallback: str) -> str:
    """A valid project slug from free text ("Transit Time & METR" -> "transit-time-metr")."""
    slug = _SLUG_BAD.sub("-", (name or "").lower()).strip("-.")[:64].rstrip("-.")
    return slug or fallback


def validate_project(project: str) -> str:
    if not PROJECT_RE.match(project):
        raise ValueError(
            f"invalid project {project!r}: use lowercase letters, digits, '.', '_' or '-'"
        )
    return project


def validate_choice(value: str, allowed: set[str], field: str) -> str:
    if value not in allowed:
        raise ValueError(f"invalid {field} {value!r}; allowed: {sorted(allowed)}")
    return value
