"""Immutable Stage 7 embedding evidence and semantic-search records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal, TypeAlias
from uuid import UUID

from buildlens.domain.processing import Chunk

EmbeddingValues: TypeAlias = tuple[float, ...]
EmbeddingOutcome: TypeAlias = Literal["complete", "failed"]
EmbeddingExecutionMode: TypeAlias = Literal["generated", "reused"]


@dataclass(frozen=True, slots=True)
class EmbeddingContract:
    provider: str
    provider_version: str
    model_id: str
    model_revision: str
    dimensions: int
    backend: str
    device: str
    precision: str
    document_prompt: str
    query_prompt: str
    document_format: str
    query_format: str
    token_limit: int
    window_policy: str
    pooling: str
    normalization: str
    sentence_transformers_version: str
    transformers_version: str
    torch_version: str

    @property
    def digest(self) -> str:
        material = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    values: EmbeddingValues
    window_count: int


@dataclass(frozen=True, slots=True)
class ChunkEmbedding:
    id: UUID
    embedding_run_id: UUID
    project_id: UUID
    snapshot_id: UUID
    processing_run_id: UUID
    artifact_processing_id: UUID
    chunk_id: UUID
    stable_chunk_key: str
    input_hash: str
    logical_embedding_key: str
    contract_digest: str
    dimensions: int
    window_count: int
    execution_mode: EmbeddingExecutionMode
    reuse_source_embedding_id: UUID | None = None
    values: EmbeddingValues | None = None

    @property
    def embedding_key(self) -> str:
        return self.logical_embedding_key


@dataclass(frozen=True, slots=True)
class EmbeddingRun:
    id: UUID
    project_id: UUID
    snapshot_id: UUID
    processing_run_id: UUID
    processing_revision: int
    processing_contract: dict[str, object]
    contract: EmbeddingContract
    revision: int
    started_at: datetime
    completed_at: datetime
    outcome: EmbeddingOutcome
    superseded: bool
    quarantined: bool
    captured_active_embedding_run_id: UUID | None
    captured_latest_revision: int
    membership_digest: str
    limits: dict[str, object]
    coverage: dict[str, int]
    diagnostics: tuple[str, ...]
    chunk_count: int = 0

    @property
    def created_at(self) -> datetime:
        return self.completed_at


@dataclass(frozen=True, slots=True)
class SemanticSearchFilters:
    locator_prefix: str | None = None
    language: str | None = None
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticSearchHit:
    score: float
    embedding: ChunkEmbedding
    chunk: Chunk

    @property
    def embedded(self) -> ChunkEmbedding:
        return self.embedding


def embedding_key(contract_digest: str, stable_chunk_key: str, input_hash: str) -> str:
    value = "|".join((contract_digest, stable_chunk_key, input_hash))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def membership_digest(chunks: list[ChunkEmbedding]) -> str:
    material = [[item.stable_chunk_key, str(item.chunk_id), item.input_hash] for item in chunks]
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
