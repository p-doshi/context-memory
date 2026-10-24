# Context Memory Harness

A local memory index shared by AI assistants (Claude, ChatGPT, Cursor, local Qwen).
The index stores claims with provenance; source files, code, and experiment records
stay authoritative. Design: [docs/design-v0.2.md](docs/design-v0.2.md).

Status: Week 1 of the roadmap (memory core).

## What exists

- `memory_write` / `memory_search` over SQLite (provenance, append-only audit log) and LanceDB (vectors)
- Heading-aware Markdown chunker; file and directory ingestion behind a path allowlist
- Secret scanner at the ingress boundary: rejected content is never chunked, embedded, or stored
- Re-ingesting a changed file retires old chunks (`valid_to` set) instead of deleting them
- MCP server over stdio and a `cmh` CLI
- Search results carry the provenance fields of design §7.2 and are marked `unverified`;
  the verification service is Week 3

## Reproducible setup

Requires [uv](https://docs.astral.sh/uv/) 0.9 or newer. Python 3.12 and every dependency
are pinned by `.python-version` and `uv.lock`.

```sh
git clone <repo> context-memory && cd context-memory
uv sync --locked --group dev               # core + tests, no GPU stack
uv sync --locked --group dev --extra embed # adds torch (CUDA 12.6), sentence-transformers
uv run pytest
```

The `embed` extra installs CUDA 12.6 torch wheels on Windows and Linux, which need an
NVIDIA driver supporting CUDA 12.6. Without a GPU the same wheels run on CPU.

## Configuration

Environment variables with the `CMH_` prefix:

| Variable | Default | Notes |
|---|---|---|
| `CMH_DATA_DIR` | `%LOCALAPPDATA%\context-memory\data` | keep out of OneDrive/Dropbox ([ADR 0001](docs/decisions/0001-storage-layout.md)) |
| `CMH_MODEL_DIR` | Hugging Face default cache | |
| `CMH_EMBEDDER` | `qwen` | `hash` = deterministic offline embedder, used by tests |
| `CMH_EMBEDDING_MODEL` | `Qwen/Qwen3-Embedding-0.6B` | |
| `CMH_EMBEDDING_MODEL_REVISION` | unset | Hugging Face commit SHA; set it to pin weights |
| `CMH_EMBEDDING_DTYPE` | `float32` | CUDA only; float16 is slower on GTX 16-series ([results](docs/benchmarks/embedding-results.md)) |
| `CMH_EMBEDDING_BATCH_SIZE` | `8` | |
| `CMH_INGEST_ROOTS` | `[]` | JSON list, e.g. `["C:/research/notes"]` |
| `CMH_CHUNK_MAX_TOKENS` | `800` | |

## Usage

```sh
uv run cmh ingest path/to/notes --project ocean-downscaling
uv run cmh search "which physics loss is used" --project ocean-downscaling
uv run cmh write "Spectral loss replaced MSE on 2026-10-18." --project ocean-downscaling --kind decision
uv run cmh stats
uv run cmh audit -n 10
uv run cmh serve   # MCP server on stdio
```

Register the MCP server with Claude Code:

```sh
claude mcp add context-memory -- uv run --directory C:/JARVIS/context-memory cmh serve
```

## Benchmarks

`scripts/bench_embed.py` measures embedding throughput, query latency and VRAM.
The plan is fixed in advance in [docs/benchmarks/embedding-plan.md](docs/benchmarks/embedding-plan.md);
raw results, with the commit, lockfile hash, and command that produced them, are in
`docs/benchmarks/results/`.

## Repository layout

```text
context_memory/   Python package: memory core, ingestion, MCP server, CLI
tests/            unit tests (offline, hash embedder)
scripts/          benchmarks and maintenance scripts
docs/             design document, decision records, benchmark plans and results
web/              (planned) public website and agentic-UI framework
```

## Roadmap

Weeks 1 to 6 follow design §26. A later stage adds a public website and an agentic-UI
framework for external visitors. That will be a separate `web/` package with its own
lockfile. It talks to the memory core only over an authenticated API, and it never reads
the local data directory.
