"""Stage 9 bounded evidence and context-construction contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias
from uuid import UUID

from buildlens.domain.embeddings import EmbeddingRun
from buildlens.domain.processing import Chunk, ChunkSpan, ProcessingRun
from buildlens.domain.retrieval import RetrievalHit, RetrievalMode

RankingPolicy: TypeAlias = Literal["local_cross_encoder", "retrieval_order"]
RerankerState: TypeAlias = Literal["completed", "not_requested", "not_needed"]
DecisionDisposition: TypeAlias = Literal["included", "excluded", "observed"]

CONTEXT_POLICY_VERSION = "stage9.context.v1"
CONTEXT_CANDIDATE_DEPTH = 100
RERANK_CANDIDATE_DEPTH = 32
SEED_COUNT = 8
EXPANDED_OBSERVATION_LIMIT = 16
CONTEXT_ITEM_LIMIT = 16
CONTEXT_BYTE_LIMIT = 32_000
RERANK_MAX_QUERY_TOKENS = 128
RERANK_MAX_WINDOWS_PER_CANDIDATE = 16
RERANK_MAX_WINDOWS = 512
RERANK_BATCH_SIZE = 8
CONTEXT_SOURCE_BYTE_LIMIT = 2 * 1024 * 1024
CONTEXT_RESPONSE_BYTE_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    version: str = CONTEXT_POLICY_VERSION
    candidate_depth: int = CONTEXT_CANDIDATE_DEPTH
    rerank_depth: int = RERANK_CANDIDATE_DEPTH
    seed_count: int = SEED_COUNT
    neighbours_per_seed: int = 2
    expanded_observation_limit: int = EXPANDED_OBSERVATION_LIMIT
    item_limit: int = CONTEXT_ITEM_LIMIT
    byte_limit: int = CONTEXT_BYTE_LIMIT
    max_windows_per_candidate: int = RERANK_MAX_WINDOWS_PER_CANDIDATE
    max_scoring_windows: int = RERANK_MAX_WINDOWS
    inference_batch_size: int = RERANK_BATCH_SIZE
    request_deadline_seconds: float = 120
    rerank_deadline_seconds: float = 45
    hydrated_source_byte_limit: int = CONTEXT_SOURCE_BYTE_LIMIT
    response_byte_limit: int = CONTEXT_RESPONSE_BYTE_LIMIT


@dataclass(frozen=True, slots=True)
class RerankerContract:
    provider: str
    provider_version: str
    model_id: str
    model_revision: str
    tokenizer_id: str
    tokenizer_revision: str
    backend: str
    device: str
    precision: str
    input_format: str
    sequence_limit: int
    query_token_limit: int
    window_policy: str
    aggregation: str
    max_windows_per_candidate: int
    batch_size: int
    transformers_version: str
    torch_version: str


@dataclass(frozen=True, slots=True)
class RerankResult:
    chunk_id: UUID
    score: float | None
    window_count: int
    winning_window: int | None
    exclusion_reason: Literal["reranker_input_limit"] | None = None


@dataclass(frozen=True, slots=True)
class ContextMeasurement:
    unit: Literal["utf8_bytes"] = "utf8_bytes"
    policy: str = "canonical-json-evidence.v1"
    exact: bool = True
    tokenizer_id: str | None = None
    tokenizer_revision: str | None = None


@dataclass(frozen=True, slots=True)
class ContextBudget:
    measurement: ContextMeasurement
    limit: int
    used: int
    framing_cost: int
    remaining: int
    per_item_costs: tuple[tuple[UUID, int], ...]
    generation_token_count: None = None


@dataclass(frozen=True, slots=True)
class ContextDecision:
    chunk_id: UUID
    stable_key: str
    locator: str
    stage: str
    disposition: DecisionDisposition
    reason: str
    related_chunk_ids: tuple[UUID, ...] = ()
    retrieval_rank: int | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class ContextEvidenceItem:
    chunk: Chunk
    retrieval: RetrievalHit | None
    retrieved_directly: bool
    selected_as_seed: bool
    expanded_from: tuple[UUID, ...]
    rerank_score: float | None
    rerank_rank: int | None
    rerank_window_count: int | None
    rerank_winning_window: int | None
    context_cost: int


@dataclass(frozen=True, slots=True)
class ContextRetrievalSummary:
    requested_mode: RetrievalMode
    executed_mode: RetrievalMode
    candidate_count: int
    candidate_depth: int
    candidates_truncated: bool
    exact_count: int
    lexical_count: int
    semantic_count: int


@dataclass(frozen=True, slots=True)
class ContextResult:
    project_id: UUID
    snapshot_id: UUID
    query: str
    ranking_policy: RankingPolicy
    policy: ContextPolicy
    processing_run: ProcessingRun
    embedding_run: EmbeddingRun | None
    retrieval: ContextRetrievalSummary
    reranker_state: RerankerState
    reranker_contract: RerankerContract | None
    items: tuple[ContextEvidenceItem, ...]
    decisions: tuple[ContextDecision, ...]
    budget: ContextBudget
    ranking_observation: RankingObservation | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    """Source-free metadata copied from decisions already made by production."""

    chunk_id: UUID
    artifact_id: UUID | None
    artifact_processing_id: UUID
    symbol_id: UUID | None
    symbol_identity_id: UUID | None
    locator: str | None
    stable_key: str
    content_hash: str
    span: ChunkSpan
    retrieval_rank: int
    score_kind: str
    retrieval_score: float | None
    exact_rank: int | None
    lexical_rank: int | None
    lexical_score: float | None
    semantic_rank: int | None
    semantic_score: float | None
    eligible: bool
    exclusions: tuple[str, ...]
    selection_order: int | None
    rerank_rank: int | None
    rerank_score: float | None
    rerank_window_count: int | None
    rerank_winning_window: int | None
    embedding_observation_id: UUID | None


@dataclass(frozen=True, slots=True)
class RankingObservation:
    version: str
    candidate_limit: int
    reranker_admission_limit: int
    candidates: tuple[CandidateObservation, ...]
