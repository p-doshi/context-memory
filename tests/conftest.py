from pathlib import Path

import pytest

from context_memory.config import Settings
from context_memory.memory.embedder import HashEmbedder
from context_memory.memory.service import MemoryService


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    docs = tmp_path / "docs"
    docs.mkdir()
    return Settings(data_dir=tmp_path / "data", embedder="hash", ingest_roots=[docs],
                    chunk_max_tokens=120)


@pytest.fixture
def svc(settings: Settings):
    s = MemoryService(settings, HashEmbedder())
    yield s
    s.close()
