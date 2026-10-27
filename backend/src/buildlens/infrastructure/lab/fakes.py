"""Deterministic, model-free providers for offline laboratory runs and tests.

These are NOT quality baselines. They exist so every pipeline's mechanics (ingestion,
storage, retrieval wiring, tracing, packing, scoring) can run with zero downloads.
"""

from __future__ import annotations

import hashlib
import math
import re

from buildlens.application.embeddings import ModelCacheStatus
from buildlens.domain.context import RerankerContract, RerankResult
from buildlens.domain.embeddings import EmbeddingContract, EmbeddingVector
from buildlens.domain.processing import Chunk

HASH_DIMENSIONS = 384
_TOKEN = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def hash_vector(text: str, dimensions: int = HASH_DIMENSIONS) -> tuple[float, ...]:
    """Signed feature hashing of identifier sub-tokens, L2-normalized (never all-zero)."""
    values = [0.0] * dimensions
    for token in _tokens(text) or ["<empty>"]:
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        values[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    if norm == 1.0 and not any(values):
        values[0] = 1.0
    return tuple(v / norm for v in values)


class HashEmbeddingProvider:
    """EmbeddingProvider-compatible deterministic hashing embedder (no model)."""

    contract = EmbeddingContract(
        "buildlens.lab.hash-embedding",
        "1.0.0",
        "buildlens/feature-hash",
        "0" * 40,
        HASH_DIMENSIONS,
        "python",
        "cpu",
        "float32",
        "empty",
        "empty",
        "chunk-content.v1",
        "query-text.v1",
        4096,
        "none.v1",
        "feature-hash.v1",
        "l2.v1",
        "n/a",
        "n/a",
        "n/a",
    )

    def __init__(self) -> None:
        self.document_calls = 0
        self.document_texts = 0
        self.query_calls = 0

    def status(self) -> ModelCacheStatus:
        return ModelCacheStatus(True, "deterministic-hash")

    def embed_documents(self, texts: list[str]) -> list[EmbeddingVector]:
        self.document_calls += 1
        self.document_texts += len(texts)
        return [EmbeddingVector(_float32(hash_vector(t)), 1) for t in texts]

    def embed_query(self, text: str) -> EmbeddingVector:
        self.query_calls += 1
        return EmbeddingVector(_float32(hash_vector(text)), 1)

    def close(self) -> None:
        pass


def _float32(values: tuple[float, ...]) -> tuple[float, ...]:
    import struct

    packed = struct.pack(f"<{len(values)}f", *values)
    return struct.unpack(f"<{len(values)}f", packed)


class OverlapReranker:
    """Reranker-compatible lexical-overlap scorer (deterministic, model-free)."""

    contract = RerankerContract(
        "buildlens.lab.overlap-reranker",
        "1.0.0",
        "buildlens/token-overlap",
        "0" * 40,
        "buildlens/token-overlap",
        "0" * 40,
        "python",
        "cpu",
        "float32",
        "pair.v1",
        512,
        128,
        "none.v1",
        "overlap.v1",
        16,
        8,
        "n/a",
        "n/a",
    )

    def __init__(self) -> None:
        self.calls = 0

    def status(self) -> ModelCacheStatus:
        return ModelCacheStatus(True, "deterministic-overlap")

    def score(
        self, query: str, candidates: list[Chunk], timeout_seconds: float
    ) -> list[RerankResult]:
        self.calls += 1
        terms = set(_tokens(query))
        results = []
        for chunk in candidates:
            tokens = _tokens(chunk.content)
            overlap = sum(1 for t in tokens if t in terms)
            score = overlap / math.sqrt(len(tokens) + 1)
            results.append(RerankResult(chunk.id, float(score), 1, 0))
        return results

    def close(self) -> None:
        pass
