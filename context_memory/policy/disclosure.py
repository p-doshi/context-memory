"""Disclosure Gate: deterministic recipient-aware filtering of outbound text.

Implements the first slice of the contextual-integrity addendum (v0.3): every
text leaving the memory system is classified into attribute tiers and passed
through the rule for its recipient class. The model never decides this.

Detection is pattern-based. It finds emails, phone numbers, street addresses,
postal codes, payment cards and credentials. It does NOT detect schedule or
relationship details (e.g. "my partner is away Friday"); those need a
classifier and are an open gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from context_memory.ingestion.sensitive_scanner import RULES as CREDENTIAL_RULES

# Ordered from least to most sensitive; used to pick a chunk's overall tier.
TIER_ORDER = [
    "public", "task-minimal", "research-private", "schedule", "relationship",
    "contact", "location-precise", "financial", "credential",
]
TIER_RANK = {t: i for i, t in enumerate(TIER_ORDER)}

RECIPIENTS = {"owner", "local_model", "cloud_model", "mcp_server", "collaborator", "public"}

_STREET = (r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|Court|Ct|"
           r"Way|Crescent|Cres|Place|Pl|Terrace|Close|Row|Highway|Hwy)")

DETECTORS: list[tuple[str, str, re.Pattern]] = [
    ("contact", "email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("contact", "phone", re.compile(
        r"(?<![\w.])(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w.])"
        r"|(?<![\w.])\+\d{1,3}[\s.-]\d{1,4}(?:[\s.-]\d{2,4}){2,4}(?![\w.])")),
    ("location-precise", "street_address", re.compile(
        rf"\b\d{{1,5}}\s+(?:[A-Z][A-Za-z'-]+\s+){{1,4}}{_STREET}\.?\b"
        r"(?:,?\s*(?:Apt|Apartment|Unit|Suite|#)\.?\s*\w+)?")),
    ("location-precise", "postal_code_ca", re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[A-Z][ -]?\d[A-Z]\d\b")),
    ("financial", "iban", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b")),
    ("financial", "payment_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
] + [("credential", name, pat) for name, pat in CREDENTIAL_RULES]

# Per-recipient rule for each tier: allow or redact. Tiers not listed are allowed.
RECIPIENT_RULES: dict[str, dict[str, str]] = {
    "owner": {"credential": "redact"},
    "local_model": {"credential": "redact"},
    "cloud_model": {"contact": "redact", "location-precise": "redact",
                    "financial": "redact", "credential": "redact"},
    "mcp_server": {"contact": "redact", "location-precise": "redact",
                   "financial": "redact", "credential": "redact"},
    "collaborator": {"contact": "redact", "location-precise": "redact",
                     "financial": "redact", "credential": "redact"},
    "public": {"contact": "redact", "location-precise": "redact",
               "financial": "redact", "credential": "redact",
               "research-private": "block"},
}


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass
class Attribute:
    tier: str
    kind: str
    start: int
    end: int


@dataclass
class GateResult:
    decision: str  # allow | transform | block
    text: str
    violations: list[dict] = field(default_factory=list)


def detect(text: str) -> list[Attribute]:
    found: list[Attribute] = []
    for tier, kind, pat in DETECTORS:
        for m in pat.finditer(text):
            if kind == "payment_card" and not _luhn_ok(m.group()):
                continue
            found.append(Attribute(tier, kind, m.start(), m.end()))
    return found


def highest_tier(attrs: list[Attribute], default: str) -> str:
    tiers = [a.tier for a in attrs] + [default]
    return max(tiers, key=lambda t: TIER_RANK.get(t, 0))


def gate(text: str, recipient: str, chunk_tier: str = "research-private") -> GateResult:
    """Apply the recipient's rules to text. Never returns raw redacted values."""
    if recipient not in RECIPIENTS:
        raise ValueError(f"unknown recipient {recipient!r}")
    rules = RECIPIENT_RULES[recipient]
    if rules.get(chunk_tier) == "block":
        return GateResult("block", "", [{"tier": chunk_tier, "kind": "chunk", "count": 1}])

    to_redact = [a for a in detect(text) if rules.get(a.tier) == "redact"]
    if not to_redact:
        return GateResult("allow", text)

    # Merge overlapping spans, keep the most sensitive label for each.
    to_redact.sort(key=lambda a: (a.start, -a.end))
    merged: list[Attribute] = []
    for a in to_redact:
        if merged and a.start < merged[-1].end:
            last = merged[-1]
            last.end = max(last.end, a.end)
            if TIER_RANK[a.tier] > TIER_RANK[last.tier]:
                last.tier, last.kind = a.tier, a.kind
        else:
            merged.append(Attribute(a.tier, a.kind, a.start, a.end))

    out, pos = [], 0
    counts: dict[tuple[str, str], int] = {}
    for a in merged:
        out.append(text[pos:a.start])
        out.append(f"[WITHHELD:{a.tier}:{a.kind}]")
        pos = a.end
        counts[(a.tier, a.kind)] = counts.get((a.tier, a.kind), 0) + 1
    out.append(text[pos:])
    violations = [{"tier": t, "kind": k, "count": n} for (t, k), n in sorted(counts.items())]
    return GateResult("transform", "".join(out), violations)
