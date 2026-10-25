"""claude.ai project knowledge exports: the documents attached to a Project
(a zip of text files and PDFs, without conversations).

Text files become notes, PDFs become papers, all under one memory project. They
are reference material, not dated work, so they carry no platform and appear
under "Files & notes" in the console. Each file's text is read inside the zip;
nothing is extracted to disk.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path

from context_memory.util import stable_id

log = logging.getLogger("cmh.ingest.claude_project")
MAX_ENTRY_BYTES = 50_000_000   # per file, uncompressed (zip-bomb guard)
TEXT_SUFFIXES = {"", ".md", ".txt", ".markdown", ".csv", ".tex", ".json", ".py"}


def is_project_export(zip_path: Path) -> bool:
    """A project export has documents but none of an account export's conversation files."""
    with zipfile.ZipFile(zip_path) as z:
        names = [n.lower() for n in z.namelist()]
    return bool(names) and not any(n.endswith("conversations.json") for n in names)


def title_of(name: str) -> str:
    stem = Path(name).stem if Path(name).suffix.lower() in TEXT_SUFFIXES | {".pdf"} else Path(name).name
    stem = re.sub(r"^claude_", "", stem)                 # claude.ai prefixes files it created
    stem = stem.replace("___", " — ").replace("_", " ")
    return re.sub(r"\s+", " ", stem).strip()


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"## Page {i}\n\n{text}")
    return "\n\n".join(pages)


def import_project_export(svc, zip_path: Path, project: str, actor: str = "cli") -> list[dict]:
    zip_path = Path(zip_path)
    out = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if info.is_dir() or name.startswith("__MACOSX/") or Path(name).name.startswith("."):
                continue
            suffix = Path(name).suffix.lower()
            if info.file_size > MAX_ENTRY_BYTES:
                out.append({"path": name, "status": "skipped", "reason": "too large"})
                continue
            if suffix == ".pdf":
                kind, text = "paper", pdf_text(z.read(info))
            elif suffix in TEXT_SUFFIXES:
                kind, text = "note", z.read(info).decode("utf-8", "replace")
            else:
                out.append({"path": name, "status": "skipped", "reason": f"unsupported type {suffix}"})
                continue
            title = title_of(name)
            if not text.strip():
                out.append({"path": name, "status": "skipped", "reason": "no extractable text"})
                continue
            if not text.lstrip().startswith("#"):
                text = f"# {title}\n\n{text}"   # a heading gives every passage the document's name
            res = svc.ingest_text(
                text=text, project=project, source="claude", kind=kind,
                path=f"claude-project://{project}/{name}", title=title,
                doc_id=stable_id("d", "claude-project", project, name),
                tags=["claude.ai-project", f"project-export:{zip_path.name}"],
                meta={"origin": "claude.ai project knowledge", "file": name}, actor=actor)
            out.append({"path": name, "kind": kind, "chars": len(text), **res})
    return out
