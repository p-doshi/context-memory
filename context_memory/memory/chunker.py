"""Heading-aware Markdown chunker.

Splits a document into sections at ATX headings (ignoring headings inside
fenced code), then packs each section's blocks into chunks of at most
`max_tokens`. Fenced code blocks are kept whole unless a single block alone
exceeds the limit, in which case it is split on line boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


@dataclass
class Section:
    path: list[str]
    level: int
    body: str


@dataclass
class Chunk:
    ordinal: int
    section_path: str
    text: str
    tokens: int


def split_sections(markdown: str) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    fence: str | None = None
    level = 0

    def flush() -> None:
        body = "\n".join(buf).strip("\n")
        if body.strip():
            sections.append(Section([t for _, t in stack], level, body))
        buf.clear()

    for line in markdown.splitlines():
        m_fence = FENCE_RE.match(line)
        if m_fence:
            marker = m_fence.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            buf.append(line)
            continue
        m = HEADING_RE.match(line) if fence is None else None
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            continue
        buf.append(line)
    flush()
    return sections


def _blocks(body: str) -> list[str]:
    """Paragraph blocks; a fenced code block (with any blank lines) is one block."""
    blocks: list[str] = []
    cur: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        m = FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                if cur:
                    blocks.append("\n".join(cur))
                    cur = []
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
                cur.append(line)
                blocks.append("\n".join(cur))
                cur = []
                continue
        if fence is None and not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def _split_oversized(block: str, max_tokens: int, count: Callable[[str], int]) -> list[str]:
    pieces: list[str] = []
    cur: list[str] = []
    for line in block.splitlines():
        if count(line) > max_tokens:
            # A single enormous line: fall back to word windows.
            words = line.split()
            step = max(1, len(words) * max_tokens // max(count(line), 1))
            if cur:
                pieces.append("\n".join(cur))
                cur = []
            pieces.extend(" ".join(words[i:i + step]) for i in range(0, len(words), step))
            continue
        if cur and count("\n".join(cur + [line])) > max_tokens:
            pieces.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        pieces.append("\n".join(cur))
    return pieces


def chunk_markdown(markdown: str, count_tokens: Callable[[str], int],
                   max_tokens: int = 800) -> list[Chunk]:
    chunks: list[Chunk] = []
    for sec in split_sections(markdown):
        section_path = " > ".join(sec.path)
        cur: list[str] = []
        cur_tokens = 0

        def emit() -> None:
            nonlocal cur, cur_tokens
            if cur:
                text = "\n\n".join(cur)
                chunks.append(Chunk(len(chunks), section_path, text, count_tokens(text)))
            cur, cur_tokens = [], 0

        for block in _blocks(sec.body):
            t = count_tokens(block)
            if t > max_tokens:
                emit()
                for piece in _split_oversized(block, max_tokens, count_tokens):
                    cur = [piece]
                    emit()
                continue
            if cur and cur_tokens + t > max_tokens:
                emit()
            cur.append(block)
            cur_tokens += t
        emit()
    return chunks


def embedding_text(title: str | None, section_path: str, text: str) -> str:
    """Text actually embedded: title and heading path give each chunk context."""
    head = " > ".join(p for p in (title, section_path) if p)
    return f"{head}\n\n{text}" if head else text
