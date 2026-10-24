"""Builds the MemoryService from settings. Shared by the CLI and MCP server."""

from __future__ import annotations

from functools import lru_cache

from context_memory.config import Settings, get_settings
from context_memory.memory.embedder import make_embedder
from context_memory.memory.service import MemoryService


def build_service(settings: Settings | None = None) -> MemoryService:
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return MemoryService(settings, make_embedder(settings))


@lru_cache(maxsize=1)
def default_service() -> MemoryService:
    return build_service()
