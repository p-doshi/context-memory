"""Runtime settings, read from CMH_* environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    # Keep databases out of synced folders (OneDrive/Dropbox): file sync
    # takes locks on SQLite/Lance files mid-write and can corrupt them.
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "context-memory" / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CMH_", env_nested_delimiter="__")

    data_dir: Path = Field(default_factory=_default_data_dir)

    # "qwen" loads Qwen3-Embedding via sentence-transformers; "hash" is a
    # deterministic, dependency-free embedder for tests and offline use.
    embedder: str = "qwen"
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    # Hugging Face commit to load; pinning it keeps embeddings reproducible.
    embedding_model_revision: str | None = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
    # Model download cache. None = the Hugging Face default (~/.cache/huggingface).
    model_dir: Path | None = None
    embedding_device: str | None = None  # None = cuda if available, else cpu
    # cuda only; cpu always runs float32. float32 is the default because float16
    # GEMM is 6.5x slower on the dev GPU (docs/benchmarks/embedding-results.md).
    embedding_dtype: str = "float32"
    # False = Hugging Face offline mode: no network calls when loading the model
    # (it must already be in model_dir). Set CMH_ALLOW_MODEL_DOWNLOAD=1 once to
    # fetch a model.
    allow_model_download: bool = False
    embedding_batch_size: int = 8
    embedding_max_seq_tokens: int = 1024

    chunk_max_tokens: int = 800

    # File ingestion is refused outside these roots (least privilege).
    ingest_roots: list[Path] = Field(default_factory=list)
    max_file_bytes: int = 2_000_000

    # vector | bm25 | hybrid. Stays "vector" until the retrieval evaluation
    # (docs/benchmarks/retrieval-plan.md) says otherwise.
    search_mode: str = "vector"
    default_search_k: int = 8
    max_search_k: int = 50

    # Contextual integrity (addendum v0.3). Results sent over MCP go to a cloud
    # model, so MCP calls use this recipient class at the Disclosure Gate.
    mcp_recipient: str = "cloud_model"
    # Projects never sent to cloud models (local_models_only, addendum §11).
    cloud_denied_projects: list[str] = Field(default_factory=list)

    # Hub: memory served over HTTP to Claude Code on other machines. It binds
    # 127.0.0.1; other machines reach it through SSH tunnels (docs/remote-setup.md).
    hub_enabled: bool = False
    hub_host: str = "127.0.0.1"
    hub_port: int = 8765
    # SSH host aliases the daemon keeps a reverse tunnel to (see server/tunnels.py),
    # e.g. ["server5.example.org"]. JSON list in CMH_HUB_TUNNELS.
    hub_tunnels: list[str] = Field(default_factory=list)

    # Public node (server/public_node.py): visitor site from the exported public
    # store, on loopback; cloudflared carries outside traffic to it.
    public_enabled: bool = False
    public_port: int = 8787
    public_export_interval_seconds: int = 6 * 3600
    # Origins (e.g. the Vercel site) allowed to call the public API from browsers.
    public_allowed_origins: list[str] = Field(default_factory=list)
    # Regex for origins too numerous to list (e.g. a Vercel project's preview URLs).
    public_allowed_origin_regex: str | None = None
    # Git remote of the static site repository Vercel deploys from; the daemon pushes
    # the site there with the current tunnel address (see site_deploy.publish).
    public_site_repo: str | None = None
    # Local answer model: llama.cpp llama-server on loopback. Empty URL = excerpts only.
    public_llm_url: str = "http://127.0.0.1:8090"
    llm_server_exe: Path | None = None
    llm_model_path: Path | None = None
    llm_gpu_layers: int = 99
    # 3072 leaves GPU headroom for the embedder on a 6 GB card; prompts stay under ~1.5k tokens.
    llm_context_tokens: int = 3072
    # cloudflared binary and its config (named tunnel -> 127.0.0.1:public_port).
    cloudflared_exe: Path | None = None
    cloudflared_config: Path | None = None

    # Daemon: TOML file listing what to index (see config/sources.example.toml).
    sources_file: Path | None = None
    daemon_interval_seconds: int = 300
    # A Claude Code session is indexed only after this many seconds without
    # writes, so live sessions are not re-embedded on every daemon pass.
    session_idle_seconds: int = 600
    # claude.ai / Perplexity syncs run at most this often per account.
    browser_sync_interval_seconds: int = 6 * 3600
    # Verified local snapshots under <home>/backup (see server/backup.py).
    backup_interval_seconds: int = 24 * 3600
    backup_keep: int = 7

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "metadata.sqlite3"

    @property
    def lance_path(self) -> Path:
        return self.data_dir / "lance"

    @property
    def resolved_sources_file(self) -> Path:
        return self.sources_file or (self.data_dir.parent / "config" / "sources.toml")


def get_settings(**overrides) -> Settings:
    return Settings(**overrides)
