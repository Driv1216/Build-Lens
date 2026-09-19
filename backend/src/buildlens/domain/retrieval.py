"""Stage 8 lexical, exact, semantic, and hybrid retrieval contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias
from uuid import UUID

from buildlens.domain.embeddings import ChunkEmbedding, EmbeddingRun
from buildlens.domain.processing import Chunk, ProcessingRun

RetrievalMode: TypeAlias = Literal["exact", "lexical", "semantic", "hybrid"]
ScoreKind: TypeAlias = Literal["exact", "lexical", "cosine", "rrf"]
ComponentState: TypeAlias = Literal["not_requested", "completed"]
ExactField: TypeAlias = Literal["symbol_name", "symbol_qualified_name", "locator"]
LexicalField: TypeAlias = Literal["content", "symbol", "path", "metadata"]

RETRIEVAL_POLICY_VERSION = "stage8.retrieval.v1"
CANDIDATE_DEPTH = 100
RRF_CONSTANT = 60


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    locator_prefix: str | None = None
    language: str | None = None
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class ExactMatch:
    field: ExactField
    value: str


@dataclass(frozen=True, slots=True)
class ExactCandidate:
    chunk_id: UUID
    stable_key: str
    matches: tuple[ExactMatch, ...]


@dataclass(frozen=True, slots=True)
class LexicalCandidate:
    chunk_id: UUID
    stable_key: str
    score: float
    field_scores: dict[LexicalField, float]
    exact_matches: tuple[ExactMatch, ...]


@dataclass(frozen=True, slots=True)
class SemanticCandidate:
    chunk_id: UUID
    stable_key: str
    score: float
    embedding: ChunkEmbedding


@dataclass(frozen=True, slots=True)
class CandidatePage:
    items: tuple[ExactCandidate | LexicalCandidate | SemanticCandidate, ...]
    truncated: bool

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    state: ComponentState
    candidate_count: int = 0
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class RankedComponent:
    rank: int
    score: float | None
    field_scores: dict[LexicalField, float] = field(default_factory=dict)
    embedding: ChunkEmbedding | None = None


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    rank: int
    score_kind: ScoreKind
    score: float | None
    chunk: Chunk
    exact_matches: tuple[ExactMatch, ...] = ()
    exact_rank: int | None = None
    lexical: RankedComponent | None = None
    semantic: RankedComponent | None = None
    lexical_rrf_contribution: float = 0.0
    semantic_rrf_contribution: float = 0.0


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    requested_mode: RetrievalMode
    executed_mode: RetrievalMode
    project_id: UUID
    snapshot_id: UUID
    processing_run: ProcessingRun
    embedding_run: EmbeddingRun | None
    exact: ComponentStatus
    lexical: ComponentStatus
    semantic: ComponentStatus
    returned_count: int
    hits: tuple[RetrievalHit, ...]
