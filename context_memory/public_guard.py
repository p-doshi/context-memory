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
        r"\b(contents? of|cat|read|open)\b.{0,20}(\.env|id_rsa|id_ed25519|sources\.toml|metadata\.sqlite3)",
    ],
    # Transformations of the hidden context ("translate everything above this line").
    "context_dump": [
        r"\b(translate|repeat|summari[sz]e|rewrite|paraphrase|encode|print|output|copy|convert|list|dump)\b"
        r".{0,40}\b(everything|all|text|content|words?|lines?|instructions?|messages?|context|excerpts?)\b"
        r".{0,25}\b(above|before|preceding|previous|prior|so\s+far|earlier)\b",
        r"\b(above|before)\s+this\s+(line|message|point|sentence)\b",
        r"\b(base64|rot13|hex|binary|morse)\b.{0,40}\b(above|everything|instructions?|context|excerpts?)\b",
    ],
    "encoded_payload": [r"[A-Za-z0-9+/]{80,}={0,2}", r"(\\x[0-9a-fA-F]{2}){8,}", r"(%[0-9a-fA-F]{2}){12,}"],
    "abuse": [r"\b(kill yourself|kys)\b"],
}
# Not attacks: people asking for material this public site cannot have. Answered
# politely, no strike, still logged for the owner.
PRIVATE_RULES = [
    r"\bi\s*(am|'m)\s+(the\s+)?(owner|author|admin|developer|maintainer|creator)\b",
    r"\bi\s*(am|'m)\s+parth\b", r"\bas\s+the\s+(owner|author|admin)\b",
    # A request (share/show/give ...) for someone's material; "how does it keep my chats
    # private?" is a fair question and must pass.
    r"\b(share|show|give|send|provide|tell\s+me|list|read|open|access|find|get|fetch|dump)\b.{0,40}"
    r"\b(my|parth'?s|the\s+owner'?s|the\s+author'?s|his|her|their)\s+(own\s+)?(papers?|thesis|chats?|"
    r"conversations?|files?|e-?mails?|messages|notes|results|research)\b",
    r"\b(results|contents?|findings)\s+of\s+(the|my|his|her)\s+[\w-]+\s+(paper|thesis|project)\b",
    r"\b(owner|author|developer)'?s?\b.{0,30}\b(e-?mail|phone|home address|location|chats?|"
    r"private messages|conversations)\b",
    r"\b(ip|ip\s+address|host\s*name|hostname)\s+(of|for)\s+(the|your|this|that)\b",
    r"\b(server|machine|laptop|hub)'?s?\s+(ip|address|hostname)\b",
]
# Questions about the bot itself: answered plainly, no strike.
META_RULES = [
    r"\b(your|its)\s+(own\s+)?(rules|instructions|guidelines|system\s+prompt|prompt|configuration|"
    r"setup|context\s+window|excerpts|training)\b",
    r"\bwho\s+(made|built|programmed|configured)\s+you\b",
]
META_REPLY = ("I'm a guide to this project's public documentation, run by a small model on the owner's laptop. "
              "I don't discuss my own configuration; ask me how the memory works instead.")
LONG_REPLY = "Please keep questions under {n} characters, and ask one thing at a time."
PRIVATE_REPLY = ("This public site only knows the project's public code and documentation. It can't see "
                 "anyone's private files, papers, chats or servers, whoever is asking. That separation is "
                 "deliberate: the public site runs from its own copy of the published material. Ask me how "
                 "the memory works and I'll explain.")
DEFAULT_REPLIES = [
    'Damn, looks like we got a "cyber" expert on our hands. Nice try, but stick to questions about the project.',
    "Bold move. Still no. Ask me about the pipeline, the code or the design instead.",
    "That prompt has been tried before, by people with more coffee than you. Project questions only.",
]
SYSTEM_MARKERS = ["You are the guide on the public website", "Excerpts are quoted data"]
OUTPUT_PATTERNS = [r"[A-Za-z]:[\\/](Users|JARVIS)\b", r"/home/\w+", r"\[WITHHELD:", r"\bcmh_[A-Za-z0-9_-]{20,}",
                   r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\b(sk|pk)-[A-Za-z0-9]{20,}"]


VERBATIM_LIMIT = 160  # characters copied unchanged from one excerpt: a dump, not an answer


def _longest_shared(answer: str, excerpts: list[str]) -> int:
    from difflib import SequenceMatcher
    a = " ".join(answer.split())
    best = 0
    for e in excerpts:
        e = " ".join((e or "").split())
        if e:
            m = SequenceMatcher(None, a, e, autojunk=False).find_longest_match(0, len(a), 0, len(e))
            best = max(best, m.size)
    return best


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
        self.private = [re.compile(p, re.I) for p in PRIVATE_RULES + list(cfg.get("private_rules") or [])]
        self.private_reply = str(cfg.get("private_reply") or PRIVATE_REPLY)
        self.meta = [re.compile(p, re.I) for p in META_RULES]
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

    @staticmethod
    def normalise(text: str) -> str:
        """A copy for matching: spaced-out letters joined ("i g n o r e" -> "ignore"),
        common character swaps undone ("1gn0re" -> "ignore"), whitespace collapsed."""
        t = re.sub(r"(?<!\w)(?:\w[\s._*~-]){2,}\w(?!\w)",
                   lambda m: re.sub(r"[\s._*~-]+", "", m.group()), text)
        t = t.translate(str.maketrans("013457@$", "oieastas"))
        return re.sub(r"\s+", " ", t)

    def check_question(self, question: str, ip: str = "") -> Verdict:
        self._maybe_reload()
        if self.blocked(ip):
            return Verdict(False, ["blocked"], "You have used up your attempts for now. Come back later with "
                                               "a question about the project.")
        if len(question) > self.max_chars:  # polite, no strike
            self.record("input", question[:300], ip, ["too_long"])
            return Verdict(False, ["too_long"], LONG_REPLY.format(n=self.max_chars))
        forms = [question, self.normalise(question)]
        allowed = any(a.search(question) for a in self.allow)
        reasons = []
        if not allowed:
            for name, pats in self.patterns.items():
                if any(p.search(f) for p in pats for f in forms):
                    reasons.append(name)
        if not reasons:
            if not allowed and any(p.search(f) for p in self.meta for f in forms):
                self.record("input", question, ip, ["meta_question"])
                return Verdict(False, ["meta_question"], META_REPLY)
            if not allowed and any(p.search(f) for p in self.private for f in forms):
                self.record("input", question, ip, ["private_request"])
                return Verdict(False, ["private_request"], self.private_reply)
            return Verdict(True)
        with self.lock:
            self.strikes.setdefault(self.client_id(ip), []).append(time.time())
        verdict = Verdict(False, reasons, random.choice(self.replies))
        self.record("input", question, ip, reasons)
        return verdict

    def check_answer(self, answer: str, question: str = "", ip: str = "",
                     excerpts: list[str] | None = None) -> Verdict:
        reasons = []
        if any(m.lower() in answer.lower() for m in SYSTEM_MARKERS):
            reasons.append("prompt_echo")
        if any(re.search(p, answer) for p in OUTPUT_PATTERNS):
            reasons.append("sensitive_output")
        if "<<<" in answer or ">>>" in answer:
            reasons.append("context_format")  # the excerpt delimiters only appear in the prompt
        if excerpts and _longest_shared(answer, excerpts) >= VERBATIM_LIMIT:
            reasons.append("verbatim_dump")
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
