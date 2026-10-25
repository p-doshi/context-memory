"""`cmh init`: write a first sources.toml so a new user edits nothing by hand.

Finds the Claude Code config directory, takes the code and documentation
folders from the command line, and optionally fetches the pinned embedding
model once (the only step that uses the network).
"""

from __future__ import annotations

from pathlib import Path

from context_memory.util import slugify


def _toml_path(p: Path) -> str:
    return p.resolve().as_posix().replace('"', '\\"')


def render_sources(code: list[Path], docs: list[Path], claude_dir: Path | None) -> str:
    out = ["# Written by `cmh init`. Edit freely; see config/sources.example.toml for all options.", ""]
    if claude_dir:
        out += ["[[accounts]]", 'label = "claude"', 'provider = "claude"',
                f'claude_config_dir = "{_toml_path(claude_dir)}"', ""]
    for p in code:
        out += ["[[codebase]]", f'path = "{_toml_path(p)}"', f'project = "{slugify(p.resolve().name, "code")}"', ""]
    for p in docs:
        out += ["[[docs]]", f'path = "{_toml_path(p)}"', f'project = "{slugify(p.resolve().name, "docs")}"', ""]
    return "\n".join(out)


def init(settings, code: list[str], docs: list[str], claude_dir: str | None, download_model: bool,
         force: bool = False) -> list[str]:
    """Returns the lines to print."""
    target = settings.resolved_sources_file
    if target.exists() and not force:
        raise ValueError(f"{target} exists; pass --force to replace it")
    code_p = [Path(c).expanduser() for c in code]
    docs_p = [Path(d).expanduser() for d in docs]
    for p in code_p + docs_p:
        if not p.is_dir():
            raise ValueError(f"not a folder: {p}")
    cdir = Path(claude_dir).expanduser() if claude_dir else Path.home() / ".claude"
    cdir = cdir if (cdir / "projects").is_dir() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_sources(code_p, docs_p, cdir), encoding="utf-8")
    lines = [f"wrote {target}",
             f"  Claude Code sessions: {cdir or 'none found (pass --claude-dir)'}",
             f"  code folders: {len(code_p)}, documentation folders: {len(docs_p)}",
             f"  data will live in {settings.data_dir}"]
    if download_model:
        from huggingface_hub import snapshot_download
        path = snapshot_download(settings.embedding_model, revision=settings.embedding_model_revision,
                                 cache_dir=str(settings.model_dir) if settings.model_dir else None)
        lines.append(f"embedding model ready: {path}")
    return lines
