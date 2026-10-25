"""Disclosure Gate: deterministic recipient-aware filtering of outbound text.

Implements the first slice of the contextual-integrity addendum (v0.3): every
text leaving the memory system is classified into attribute tiers and passed
through the rule for its recipient class. The model never decides this.

Detection is pattern-based. It finds emails (including "name [at] host [dot] ca"),
phone numbers (local and international, E.164), street addresses, postal codes,
GPS coordinates, payment cards and IBANs (checksummed), Canadian SINs and US SSNs,
dates of birth next to a cue word, and credentials. It does NOT detect schedule or
relationship details (e.g. "my partner is away Friday") or names; those need a
classifier and are an open gap. Patterns can be evaded by unusual formats; the
public site therefore offers no free-text probe of the gate (see public_node).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from context_memory.ingestion.sensitive_scanner import RULES as CREDENTIAL_RULES

# Ordered from least to most sensitive; used to pick a chunk's overall tier.
TIER_ORDER = [
    "public", "task-minimal", "research-private", "schedule", "relationship",
    "contact", "identity", "location-precise", "financial", "credential",
]
TIER_RANK = {t: i for i, t in enumerate(TIER_ORDER)}

RECIPIENTS = {"owner", "local_model", "cloud_model", "mcp_server", "collaborator", "public"}

_STREET = (r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|Court|Ct|"
           r"Way|Crescent|Cres|Place|Pl|Terrace|Close|Row|Highway|Hwy)")

# A number ends at a non-word character; a trailing sentence period is fine, a
# decimal continuation (".5") is not. (The old "not followed by '.'" rule let every
# phone number at the end of a sentence through.)
_END = r"(?!\w|\.\d)"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"

DETECTORS: list[tuple[str, str, re.Pattern]] = [
    ("contact", "email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # "name [at] host [dot] ca", "name (at) host (dot) ca", "name at host dot ca"
    ("contact", "email", re.compile(
        r"\b[A-Za-z0-9._%+-]+\s*[\[({<]\s*at\s*[\])}>]\s*[A-Za-z0-9-]+(?:\s*[\[({<]?\s*dot\s*[\])}>]?\s*[A-Za-z0-9-]+)+"
        r"|\b[A-Za-z0-9._%+-]+\s+at\s+[A-Za-z0-9-]+(?:\s+dot\s+[A-Za-z0-9-]+)+\b", re.I)),
    ("contact", "phone", re.compile(
        rf"(?<![\w.])(?:\+?1[\s.-]?)?\(?[2-9]\d{{2}}\)?[\s.-]?\d{{3}}[\s.-]\d{{4}}{_END}"
        rf"|(?<![\w.])\+\d{{1,3}}[\s.-]\d{{1,4}}(?:[\s.-]\d{{2,4}}){{2,4}}{_END}"
        rf"|(?<![\w+])\+\d{{10,15}}{_END}")),                          # E.164, no separators
    ("identity", "government_id", re.compile(rf"(?<![\w.])\d{{3}}[ -]\d{{3}}[ -]\d{{3}}{_END}"   # SIN (Luhn-checked)
                                             rf"|(?<![\w.])\d{{3}}-\d{{2}}-\d{{4}}{_END}")),     # SSN
    ("identity", "date_of_birth", re.compile(
        rf"\b(?:born(?:\s+on)?|DOB|D\.O\.B\.?|date\s+of\s+birth|birth\s*date|birthday)\b[\s:,-]*"
        rf"(?:\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}|{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}"
        rf"|\d{{1,2}}\s+{_MONTH}\s+\d{{4}})", re.I)),
    ("location-precise", "street_address", re.compile(
        rf"\b\d{{1,5}}\s+(?:[A-Z][A-Za-z'-]+\s+){{1,4}}{_STREET}\.?\b"
        r"(?:,?\s*(?:Apt|Apartment|Unit|Suite|#)\.?\s*\w+)?")),
    ("location-precise", "postal_code_ca", re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[A-Z][ -]?\d[A-Z]\d\b")),
    ("location-precise", "coordinates", re.compile(
        # 44.6366 N, 63.5917 W  /  44.6366° N -63.5917° W
        r"(?<![\w.])-?\d{1,2}(?:\.\d+)?\s*°?\s*[NS]\b\s*[,;]?\s*-?\d{1,3}(?:\.\d+)?\s*°?\s*[EW]\b"
        # 44°38'12"N 63°35'30"W
        r"|\d{1,3}°\s*\d{1,2}['′]\s*\d{1,2}(?:\.\d+)?[\"″]?\s*[NSEW]"
        # 44.6366, -63.5917  (unlabelled pairs need 4+ decimals, so metrics like 0.750 pass)
        rf"|(?<![\w.])-?\d{{1,2}}\.\d{{4,}}\s*,\s*-?\d{{1,3}}\.\d{{4,}}{_END}")),
    ("financial", "iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")),
    ("financial", "payment_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
] + [("credential", name, pat) for name, pat in CREDENTIAL_RULES]

# Per-recipient rule for each tier: allow or redact. Tiers not listed are allowed.
RECIPIENT_RULES: dict[str, dict[str, str]] = {
    "owner": {"credential": "redact"},
    "local_model": {"credential": "redact"},
    "cloud_model": {"contact": "redact", "identity": "redact", "location-precise": "redact",
                    "financial": "redact", "credential": "redact"},
    "mcp_server": {"contact": "redact", "identity": "redact", "location-precise": "redact",
                   "financial": "redact", "credential": "redact"},
    "collaborator": {"contact": "redact", "identity": "redact", "location-precise": "redact",
                     "financial": "redact", "credential": "redact"},
    "public": {"contact": "redact", "identity": "redact", "location-precise": "redact",
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


def _coords_ok(text: str) -> bool:
    """Unlabelled pairs only: within lat/lon range and not two tiny numbers (0.0003, 0.0001)."""
    if re.search(r"[NSEW°]", text):
        return True
    a, b = (abs(float(x)) for x in re.findall(r"-?\d+(?:\.\d+)?", text)[:2])
    return a <= 90 and b <= 180 and max(a, b) >= 1


def _luhn9_ok(text: str) -> bool:
    nums = [int(c) for c in text if c.isdigit()]
    if re.fullmatch(r"\d{3}-\d{2}-\d{4}", text.strip()):
        return True  # SSN layout (3-2-4): no checksum
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _iban_ok(text: str) -> bool:
    s = text.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    s = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in s)
    return int(digits) % 97 == 1


def detect(text: str) -> list[Attribute]:
    found: list[Attribute] = []
    for tier, kind, pat in DETECTORS:
        for m in pat.finditer(text):
            if kind == "payment_card" and not _luhn_ok(m.group()):
                continue
            if kind == "iban" and not _iban_ok(m.group()):
                continue
            if kind == "government_id" and not _luhn9_ok(m.group()):
                continue
            if kind == "coordinates" and not _coords_ok(m.group()):
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
