"""claude.ai Project knowledge exports: text and PDF files imported as notes and papers."""

import io
import zipfile

from context_memory.ingestion.claude_project import import_project_export, is_project_export, title_of
from context_memory.server.daemon import process_project_exports


def _pdf(text: str) -> bytes:
    """A minimal one-page PDF whose page shows `text` (Helvetica)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def test_titles():
    assert title_of("claude_Window_Length_Pilot___Log") == "Window Length Pilot — Log"
    assert title_of("Game_Plan") == "Game Plan"
    assert title_of("ICLR_2027_ECHO.pdf") == "ICLR 2027 ECHO"


def test_import_text_and_pdf(svc, tmp_path):
    z = _zip(tmp_path / "project_export.zip", {
        "Game_Plan": "The problem being solved\n\nPassive acoustic monitoring archives are too large to inspect.",
        "claude_Pilot___Log": "# Pilot — Master Log\n\nEntry 1: window length 5 s beat 2 s on unseen stations.",
        "Paper.pdf": _pdf("Weak vessel detection through temporal deviations"),
        "figure.png": b"\x89PNG",
    })
    assert is_project_export(z)
    res = import_project_export(svc, z, "studies")
    by = {r["path"]: r for r in res}
    assert by["Game_Plan"]["kind"] == "note" and by["Paper.pdf"]["kind"] == "paper"
    assert by["figure.png"]["status"] == "skipped"
    hits = svc.memory_search("vessel detection temporal deviations", project="studies", mode="bm25",
                             recipient="owner")["results"]
    assert hits and hits[0]["title"] == "Paper" and hits[0]["kind"] == "paper"
    doc = svc.sqlite.conn.execute("SELECT title, meta FROM documents WHERE title = 'Game Plan'").fetchone()
    assert doc and '"platform"' not in doc[1]            # reference material, not a dated chat
    again = import_project_export(svc, z, "studies")      # same files: nothing new
    assert {r.get("status") for r in again if r["path"] != "figure.png"} == {"unchanged"}


def test_daemon_inbox_imports_then_moves_and_refuses_account_exports(svc, tmp_path):
    root = tmp_path / "claude-projects"
    (root / "studies").mkdir(parents=True)
    _zip(root / "studies" / "project_export.zip", {"Notes": "Statistical testing plan for the pilot."})
    _zip(root / "studies" / "data.zip", {"conversations.json": "[]"})
    out = process_project_exports(svc, root)
    assert list(out) == ["project-export:studies:project_export.zip"]
    assert len(list((root / "studies" / "done").glob("*project_export.zip"))) == 1
    assert (root / "studies" / "data.zip").exists()       # an account export is left for the account inbox
