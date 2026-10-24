"""Ingress-boundary secret scanner.

Runs before anything is chunked, embedded, or written. A hit rejects the
whole item; findings report the rule and line number, never the match text.
This is a first line of defense with known gaps (no entropy analysis, no
PII classifier); the full privacy pipeline is a later milestone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

RULES: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("openrouter_key", re.compile(r"\bsk-or-v1-[a-f0-9]{32,}")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("url_credentials", re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.I)),
    ("password_assignment", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
        r"\s*[:=]\s*['\"]?(?!\s|['\"]?(?:<|\$\{|\{\{|xxx|\*\*\*|changeme|example|none|null|''|\"\"))"
        r"[^\s'\"]{8,}"
    )),
]

BLOCKED_FILENAMES = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|.*\.(?:pem|key|p12|pfx|kdbx|keystore|jks)"
    r"|\.netrc|\.pgpass|credentials(?:\.json)?|secrets?\.(?:ya?ml|json|toml)|cookies(?:\.sqlite)?)$"
)


@dataclass
class Finding:
    rule: str
    line: int


def scan_text(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in RULES:
            if pattern.search(line):
                findings.append(Finding(name, lineno))
    return findings


def redact_text(text: str) -> str:
    """Replace every rule match with [REDACTED:<rule>]."""
    for name, pattern in RULES:
        text = pattern.sub(f"[REDACTED:{name}]", text)
    return text


def blocked_filename(path: Path) -> bool:
    return bool(BLOCKED_FILENAMES.match(path.name))
