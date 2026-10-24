"""Embedding backends.

Qwen3Embedder wraps sentence-transformers. HashEmbedder is a deterministic
bag-of-words feature hasher: no model download, used by tests and as an
offline fallback. Its retrieval quality is far below a learned model.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    def count_tokens(self, text: str) -> int: ...


class HashEmbedder:
    def __init__(self, dim: int = 256):
        self.dim = dim
        self.model_name = f"hash-bow-{dim}"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in _WORD_RE.findall(text.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) == 0 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def count_tokens(self, text: str) -> int:
        # Rough subword estimate: ~1.3 tokens per word-like unit.
        return max(1, math.ceil(len(_WORD_RE.findall(text)) * 1.3 + text.count("\n") * 0.1))


class Qwen3Embedder:
    """Qwen3-Embedding via sentence-transformers.

    Queries use the model's built-in "query" prompt; documents are embedded
    without an instruction, per the model card's usage example.
    """

    def __init__(self, model_name: str, device: str | None = None,
                 batch_size: int = 8, max_seq_tokens: int = 1024,
                 revision: str | None = None, cache_dir: str | None = None,
                 dtype: str = "float16"):
        import torch
        from sentence_transformers import SentenceTransformer

        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        # Always explicit: with no dtype, transformers loads the checkpoint's
        # bfloat16, which is slow on CPU and not native on Turing GPUs.
        model_kwargs = {"dtype": getattr(torch, dtype) if device.startswith("cuda") else torch.float32}
        self.model = SentenceTransformer(
            model_name,
            device=device,
            revision=revision,
            cache_folder=cache_dir,
            model_kwargs=model_kwargs,
            processor_kwargs={"padding_side": "left"},
        )
        self.model.max_seq_length = max_seq_tokens
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self.dim = int(self.model.get_embedding_dimension())

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out = self.model.encode(
            texts, batch_size=self.batch_size, normalize_embeddings=True,
            convert_to_numpy=True, show_progress_bar=False,
        )
        return out.astype("float32").tolist()

    def embed_query(self, text: str) -> list[float]:
        out = self.model.encode(
            [text], prompt_name="query", normalize_embeddings=True,
            convert_to_numpy=True, show_progress_bar=False,
        )
        return out[0].astype("float32").tolist()

    def count_tokens(self, text: str) -> int:
        return len(self.model.tokenizer(text, add_special_tokens=False)["input_ids"])


def make_embedder(settings) -> Embedder:
    if settings.embedder == "hash":
        return HashEmbedder()
    if settings.embedder == "qwen":
        return Qwen3Embedder(
            settings.embedding_model,
            device=settings.embedding_device,
            batch_size=settings.embedding_batch_size,
            max_seq_tokens=settings.embedding_max_seq_tokens,
            revision=settings.embedding_model_revision,
            cache_dir=str(settings.model_dir) if settings.model_dir else None,
            dtype=settings.embedding_dtype,
        )
    raise ValueError(f"unknown embedder {settings.embedder!r}")
