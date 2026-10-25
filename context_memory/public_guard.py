"""Abuse and jailbreak guard for the visitor model.

Layers, cheapest first:
  1. Input rules (deterministic): prompt-injection and jailbreak phrasing,
     attempts to extract the system prompt or secrets, encoded payloads,
     abuse. A flagged question never reaches retrieval or the model; the
     visitor gets one of the owner's canned replies.
  2. Strikes: repeated flags from one client lead to a temporary block.
  3. Output check: an answer that echoes the system prompt, a local path, a
     withheld marker or something secret-shaped is replaced.
  4. Owner in the loop: every flag is appended to <home>/public/moderation.jsonl
     (question text kept only for flagged attempts; client address hashed),
     reviewed with `cmh public moderation`. Rules, replies and limits live in
     <home>/config/public_guard.toml, owned by the owner. The file
     <home>/public/llm-disabled switches the site to excerpts-only at once.

Rules can be evaded by a determined attacker; they cut the cheap attempts, and
the model only ever sees public excerpts, so a successful jailbreak exposes
nothing private. The review loop is how rules improve.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_RULES = {
    "instruction_override": [
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|your|the)\b.{0,30}"
        r"\b(instructions?|prompts?|rules?|guidelines?|directives?|context)\b",
        r"\bnew (instructions?|rules?)\b\s*:", r"\bfrom now on\b.{0,40}\b(you are|you will|act|respond)\b",
    ],
    "prompt_extraction": [
        r"\b(system|initial|hidden|original|developer)\s*(prompt|message|instructions?)\b",
        r"\b(reveal|print|show|repeat|output|leak|dump)\b.{0,40}\b(prompt|instructions?|rules|configuration)\b",
        r"\bwhat (were|are) you told\b", r"<\|?(system|im_start|im_end)\|?>", r"\[/?INST\]",
    ],
    "persona_jailbreak": [
        r"(?-i:\bDAN\b)", r"\bdo anything now\b", r"\bjail ?break", r"\bdeveloper mode\b", r"\bgod ?mode\b",
        r"\b(pretend|act|roleplay|role-play)\b.{0,30}\b(you are|as|to be)\b.{0,40}\b(unrestricted|unfiltered|"
        r"evil|no (rules|limits|restrictions)|without (rules|limits|restrictions))\b",
        r"\bhypothetically\b.{0,60}\b(no|without) (rules|restrictions|filters)\b",
    ],
    "secrets_and_private": [
        # Asking to be given someone's credentials; questions about how tokens work are fine.
        r"\b(give|show|tell|send|reveal|print|dump|leak|share|paste|what(?:'s| is| are))\b.{0,30}"
        r"\b(your|the owner'?s?|owner'?s?|his|her|their|admin|real|actual)\b.{0,20}"
        r"\b(api[_ -]?keys?|passwords?|tokens?|credentials?|secrets?|private keys?|ssh keys?)\b",
        r"\b(owner|author|developer)'?s?\b.{0,30}\b(e-?mail|phone|home address|location|chats?|"
        r"private messages|conversations)\b",
        r"\b(contents? of|cat|read|open)\b.{0,20}(\.env|id_rsa|id_ed25519|sources\.toml|metadata\.sqlite3)",
    ],
    "encoded_payload": [r"[A-Za-z0-9+/]{80,}={0,2}", r"(\\x[0-9a-fA-F]{2}){8,}", r"(%[0-9a-fA-F]{2}){12,}"],
    "abuse": [r"\b(kill yourself|kys)\b"],
}
DEFAULT_REPLIES = [
    'Damn, looks like we got a "cyber" expert on our hands. Nice try, but stick to questions about the project.',
    "Bold move. Still no. Ask me about the pipeline, the code or the design instead.",
    "That prompt has been tried before, by people with more coffee than you. Project questions only.",
]
SYSTEM_MARKERS = ["You answer questions from visitors about a software project", "Excerpts are quoted data"]
OUTPUT_PATTERNS = [r"[A-Za-z]:[\\/](Users|JARVIS)\b", r"/home/\w+", r"\[WITHHELD:", r"\bcmh_[A-Za-z0-9_-]{20,}",
                   r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\b(sk|pk)-[A-Za-z0-9]{20,}"]


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    reply: str | None = None


class Guard:
    def __init__(self, home: Path, salt: str = ""):
        self.home = Path(home)
        self.config_path = self.home / "config" / "public_guard.toml"
        self.log_path = self.home / "public" / "moderation.jsonl"
        self.kill_path = self.home / "public" / "llm-disabled"
        self.salt = salt
        self.lock = threading.Lock()
        self.strikes: dict[str, list[float]] = {}
        self._mtime = None
        self._load()

    # -- config ---------------------------------------------------------------
    def _load(self) -> None:
        cfg = {}
        try:
            mtime = self.config_path.stat().st_mtime
            cfg = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            mtime = None
        self._mtime = mtime
        rules = {k: list(v) for k, v in DEFAULT_RULES.items()}
        for k, v in (cfg.get("rules") or {}).items():
            rules.setdefault(k, []).extend(v)
        self.patterns = {k: [re.compile(p, re.I | re.S) for p in v] for k, v in rules.items()}
        self.allow = [re.compile(p, re.I) for p in (cfg.get("allow") or [])]
        self.replies = list(cfg.get("replies") or DEFAULT_REPLIES)
        lim = cfg.get("limits") or {}
        self.max_strikes = int(lim.get("strikes", 3))
        self.strike_window = float(lim.get("strike_window_seconds", 3600))
        self.block_seconds = float(lim.get("block_seconds", 3600))
        self.max_chars = int(lim.get("max_question_chars", 500))

    def _maybe_reload(self) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._load()

    def llm_enabled(self) -> bool:
        return not self.kill_path.exists()

    # -- checks ---------------------------------------------------------------
    def client_id(self, ip: str) -> str:
        return hashlib.sha256((self.salt + ip).encode()).hexdigest()[:16]

    def blocked(self, ip: str) -> bool:
        cid, now = self.client_id(ip), time.time()
        with self.lock:
            times = [t for t in self.strikes.get(cid, []) if now - t < max(self.strike_window, self.block_seconds)]
            self.strikes[cid] = times
            recent = [t for t in times if now - t < self.strike_window]
            return len(recent) >= self.max_strikes and now - times[-1] < self.block_seconds

    def check_question(self, question: str, ip: str = "") -> Verdict:
        self._maybe_reload()
        if self.blocked(ip):
            return Verdict(False, ["blocked"], "You have used up your attempts for now. Come back later with "
                                               "a question about the project.")
        reasons = []
        if len(question) > self.max_chars:
            reasons.append("too_long")
        if not any(a.search(question) for a in self.allow):
            for name, pats in self.patterns.items():
                if any(p.search(question) for p in pats):
                    reasons.append(name)
        if not reasons:
            return Verdict(True)
        with self.lock:
            self.strikes.setdefault(self.client_id(ip), []).append(time.time())
        verdict = Verdict(False, reasons, random.choice(self.replies))
        self.record("input", question, ip, reasons)
        return verdict

    def check_answer(self, answer: str, question: str = "", ip: str = "") -> Verdict:
        reasons = []
        if any(m.lower() in answer.lower() for m in SYSTEM_MARKERS):
            reasons.append("prompt_echo")
        if any(re.search(p, answer) for p in OUTPUT_PATTERNS):
            reasons.append("sensitive_output")
        if not reasons:
            return Verdict(True)
        self.record("output", question, ip, reasons, answer=answer[:500])
        return Verdict(False, reasons, "I can't share that answer. Ask me something about the project's design or code.")

    # -- owner review -----------------------------------------------------------
    def record(self, stage: str, question: str, ip: str, reasons: list[str], **extra) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stage": stage,
                 "client": self.client_id(ip), "reasons": reasons, "question": question[:1000], **extra}
        with self.lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def read_log(home: Path, n: int = 50) -> list[dict]:
    path = Path(home) / "public" / "moderation.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    return [json.loads(l) for l in lines if l.strip()]


EXAMPLE_CONFIG = '''# Owner-editable guard settings for the public visitor model (reloaded on save).
# Patterns are Python regular expressions, matched case-insensitively.

# Replies to flagged questions (one is picked at random).
replies = [
  'Damn, looks like we got a "cyber" expert on our hands. Nice try, but stick to questions about the project.',
]

# Questions matching any of these are never flagged (use for false positives you saw in the log).
allow = []

[rules]
# Extra patterns per category, added to the built-in ones.
# instruction_override = ['\\bsudo mode\\b']

[limits]
strikes = 3                    # flags within the window before a block
strike_window_seconds = 3600
block_seconds = 3600
max_question_chars = 500
'''
