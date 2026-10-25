# Context Memory Harness

A local memory index shared by AI assistants (Claude, ChatGPT, Cursor, local Qwen).
The index stores claims with provenance; source files, code, and experiment records
stay authoritative. Design: [docs/design-v0.2.md](docs/design-v0.2.md), with the
[contextual-integrity addendum](docs/design-v0.3-contextual-integrity.md).

Status: Week 1 done; parts of Weeks 2 (hybrid retrieval, graph) and 5 (chat ingestion) in place.

## What exists

One store (SQLite + LanceDB) for every source and account:

| Source | How |
|---|---|
| Codebases | `git ls-files`; Python split at classes/methods, line ranges kept |
| Markdown docs | heading-aware chunks |
| Claude Code sessions | `<CLAUDE_CONFIG_DIR>/projects/**.jsonl`, per account |
| Claude web + desktop | logged-in browser sync, or the official export via an inbox folder |
| Perplexity | logged-in browser sync (threads + Spaces) |

- `memory_search` (compact hits, ~1.5k tokens) / `memory_get` / `memory_write`, with
  provenance (design §7.2); results from local files are checked against the file at
  query time (`verified` / `stale` / `missing`), chats stay `unverified`
- Automatic session preamble on every configured machine (SessionStart context pack
  plus `CLAUDE.md` guidance); remote machines via SSH tunnels to the hub
- Daily verified local snapshots (7 kept) under `<home>/backup`
- Retrieval modes `vector`, `bm25` (SQLite FTS5) and `hybrid` (reciprocal rank fusion)
- Temporal knowledge graph: projects, documents, accounts, modules, classes, methods,
  imports, file mentions; each edge cites its evidence chunk and expires with it
- Ingress: secret scanner (reject, or redact for chat logs); path allowlist
- Egress: deterministic Disclosure Gate per recipient class; MCP results are treated
  as going to a cloud model (addendum §11)
- Supersession instead of deletion: changed sources retire old chunks and edges
- Always-on daemon with startup reconciliation, heartbeat, GPU temperature
- MCP server (stdio and HTTP hub) and `cmh` CLI
- Hub (127.0.0.1 only) with per-device tokens and scopes (`read`, `write`, `upload`,
  `context`, `public`, `admin`); hash-chained audit log
- Owner console (`cmh console`, admin scope): store overview, project graph projection,
  entity neighbourhoods, audit trail
- Visitor page (`cmh visitor`, public scope): an [AG-UI](https://docs.ag-ui.com) client over
  only the projects listed under `[public]` in `sources.toml`, chats excluded; answers are
  extractive (no language model) and pass the Disclosure Gate as recipient `public`
- `cmh security-check` and `scripts/harden-host.ps1` (admin): inbound block on the hub
  port, outbound block on the harness interpreter

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
| `CMH_EMBEDDING_MODEL_REVISION` | `97b0c614…` | Hugging Face commit the benchmark used; pins the weights |
| `CMH_EMBEDDING_DTYPE` | `float32` | CUDA only; float16 was 4.3x slower on the dev GPU, a GTX 1660 Ti ([results](docs/benchmarks/embedding-results.md)) |
| `CMH_EMBEDDING_BATCH_SIZE` | `8` | |
| `CMH_INGEST_ROOTS` | `[]` | JSON list, e.g. `["C:/research/notes"]` |
| `CMH_CHUNK_MAX_TOKENS` | `800` | |

## Usage

```sh
# what to index lives in <home>/config/sources.toml (see config/sources.example.toml)
uv run cmh daemon --once                        # one indexing pass over every source
uv run cmh status                               # daemon heartbeat

uv run cmh ingest-code path/to/repo --project ocean-downscaling
uv run cmh ingest path/to/notes --project ocean-downscaling
uv run cmh ingest-claude --account claude-work
uv run cmh browser login claude-work            # once per web account, in plain Chrome
uv run cmh browser sync claude-work

uv run cmh search "which physics loss is used" --mode hybrid
uv run cmh search "..." --as cloud_model        # see what the Disclosure Gate lets out
uv run cmh graph neighbors context_memory.memory.service --relation imports
uv run cmh write "Spectral loss replaced MSE on 2026-10-18." --project ocean-downscaling --kind decision
uv run cmh secret set perplexity                # hidden prompt -> OS keychain
uv run cmh stats ; uv run cmh audit -n 10
uv run cmh serve                                # MCP server on stdio
uv run cmh device add laptop2 --bundle          # token + setup bundle for another machine
uv run cmh console                              # owner console in the browser
uv run cmh visitor                              # preview of what a visitor would see
uv run cmh security-check
```

The always-on daemon is a per-user scheduled task:
`powershell -ExecutionPolicy Bypass -File scripts\install-daemon.ps1 [-KeepAwakeOnAC]`.

Register the MCP server with Claude Code:

```sh
claude mcp add context-memory -- uv run --directory C:/JARVIS/context-memory cmh serve
```

## Benchmarks

Each benchmark has a plan committed before it runs; raw results record the commit,
lockfile hash, seed and command.

| Benchmark | Plan | Result |
|---|---|---|
| Embedding throughput (`scripts/bench_embed.py`) | [plan](docs/benchmarks/embedding-plan.md) | [results](docs/benchmarks/embedding-results.md): float32 on the dev GPU, 4.98 chunks/s at 512 tokens |
| Search latency (`scripts/bench_search.py`) | stated in the results | [results](docs/benchmarks/search-latency-results.md): hybrid p50 152 ms local, 219 ms through a tunnel |
| Retrieval quality (`scripts/eval_retrieval.py`) | [plan](docs/benchmarks/retrieval-plan.md) | [results](docs/benchmarks/retrieval-results.md): hybrid recall@5 0.90 vs vector 0.75; default stays vector by the pre-set rule |

## Repository layout

```text
context_memory/   Python package: memory core, ingestion, MCP server, CLI
tests/            unit tests (offline, hash embedder)
scripts/          benchmarks and maintenance scripts
docs/             design document, decision records, benchmark plans and results
web/console/      owner console (static; vendored Cytoscape.js 3.30.2, MIT)
web/visitor/      visitor page, AG-UI client
client/           stdlib-only push/pull client and setup scripts for other machines
```

## Roadmap

Weeks 1 to 6 follow design §26. The visitor page is served by the local hub today. Public
hosting (a separate host that holds only an exported public subset, never the local data
directory) and an optional language-model answerer are open decisions.
