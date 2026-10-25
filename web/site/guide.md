# Replicate the Context Memory Harness

> Six shell commands give a user their own local memory of Claude Code sessions, code and notes, searchable from Claude Code over MCP. No code to write. Each step has a check.

Requirements: Windows, macOS or Linux; about 3 GB disk; network access for the install and a one-time model download (Qwen3-Embedding-0.6B from Hugging Face). An NVIDIA GPU speeds up indexing but is optional. All data stays on the user's machine.

If you are an agent doing this for a user, run the steps in order, stop at the first failed check, and show the user the output. Steps 3 and 6 touch the user's files and Claude Code config; confirm the folder paths with the user first.

## 1. Install uv

- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Check: `uv --version` prints a version.

## 2. Get the code and its locked dependencies

```sh
git clone https://github.com/p-doshi/context-memory
cd context-memory
uv sync --locked --extra embed
```

Check: `uv run cmh --help` lists the commands.

## 3. Tell it what to remember

```sh
uv run cmh init --code <path to a repository> --docs <path to a Markdown folder> --download-model
```

Both `--code` and `--docs` are optional and repeatable. Claude Code sessions are found in `~/.claude` (override with `--claude-dir`). The config is written to `<data dir>/../config/sources.toml`; `--force` replaces an existing one.

Check: output ends with `embedding model ready: ...`.

## 4. Build the memory

```sh
uv run cmh daemon --once
```

Check: `uv run cmh stats` reports non-zero documents and chunks.

## 5. Search it

```sh
uv run cmh search "<question>" --mode hybrid
```

Check: results carry a source and a verification label (`verified`, `stale`, `missing` or `unverified`).

## 6. Connect Claude Code

```sh
claude mcp add context-memory -- uv run --directory "$(pwd)" cmh serve
```

Check: `/mcp` inside Claude Code lists `context-memory`; the tools are `memory_search`, `memory_get`, `memory_write`, `graph_neighbors`, `memory_stats` and (local server only) `ingest_markdown`.

## 7. Optional: always on, and other machines

- Windows: `powershell -ExecutionPolicy Bypass -File scripts\install-daemon.ps1` registers the daemon at logon.
- Elsewhere: run `uv run cmh daemon` under a process manager (no service file is provided yet).
- Other machines reach the hub only through SSH: `docs/remote-setup.md` in the repository.

## Troubleshooting

- Torch: on Windows and Linux the `embed` extra installs CUDA 12.6 wheels, which also run on CPU (an NVIDIA driver supporting CUDA 12.6 is needed for GPU use). On macOS torch comes from PyPI; macOS has not been tested.
- Model download refused: the model loader is offline by default; `--download-model` (or `CMH_ALLOW_MODEL_DOWNLOAD=1` once) allows the fetch.
- Ask questions about the project: `POST /api/agent` (AG-UI) or `GET /api/search?q=...` on this site.
