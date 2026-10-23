from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from buildlens.api.embedding_schemas import EmbeddingContractResponse, EmbeddingRunResponse
from buildlens.api.processing_schemas import ProcessingSpanResponse
from buildlens.api.retrieval_schemas import RetrievalFiltersRequest, RetrievalHitResponse


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    retrieval_mode: Literal["exact", "lexical", "semantic", "hybrid"]
    ranking_policy: Literal["local_cross_encoder", "retrieval_order"]
    filters: RetrievalFiltersRequest | None = None


class RerankerContractResponse(BaseModel):
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


class RerankerStatusResponse(BaseModel):
    available: bool
    path: str
    reason: str | None
    runtime_verified: bool = False
    acquisition_command: str = "buildlens reranking download-model"
    contract: RerankerContractResponse


class ContextPolicyResponse(BaseModel):
    version: str
    candidate_depth: int
    rerank_depth: int
    seed_count: int
    neighbours_per_seed: int
    expanded_observation_limit: int
    item_limit: int
    byte_limit: int
    max_windows_per_candidate: int
    max_scoring_windows: int
    inference_batch_size: int
    request_deadline_seconds: float
    rerank_deadline_seconds: float
    hydrated_source_byte_limit: int
    response_byte_limit: int


class ContextRetrievalResponse(BaseModel):
    requested_mode: Literal["exact", "lexical", "semantic", "hybrid"]
    executed_mode: Literal["exact", "lexical", "semantic", "hybrid"]
    candidate_count: int
    candidate_depth: int
    candidates_truncated: bool
    exact_count: int
    lexical_count: int
    semantic_count: int


class ContextDecisionResponse(BaseModel):
    chunk_id: UUID
    stable_key: str
    locator: str
    stage: str
    disposition: Literal["included", "excluded", "observed"]
    reason: str
    related_chunk_ids: list[UUID]
    retrieval_rank: int | None
    rerank_rank: int | None
    rerank_score: float | None


class ContextEvidenceResponse(BaseModel):
    artifact_processing_id: UUID
    artifact_id: UUID
    locator: str
    chunk_id: UUID
    stable_key: str
    content_hash: str
    kind: str
    ordinal: int
    unit_anchor: str
    part_index: int
    span: ProcessingSpanResponse
    symbol_id: UUID | None
    symbol_identity_id: UUID | None
    heading_path: list[str] = Field(default_factory=list)
    json_path: str | None
    content: str
    retrieval: RetrievalHitResponse | None
    retrieved_directly: bool
    selected_as_seed: bool
    expanded_from: list[UUID]
    rerank_score: float | None
    rerank_rank: int | None
    rerank_window_count: int | None
    rerank_winning_window: int | None
    context_cost: int


class ContextMeasurementResponse(BaseModel):
    unit: Literal["utf8_bytes"]
    policy: str
    exact: bool
    tokenizer_id: str | None
    tokenizer_revision: str | None


class ContextBudgetResponse(BaseModel):
    measurement: ContextMeasurementResponse
    limit: int
    used: int
    framing_cost: int
    remaining: int
    per_item_costs: dict[UUID, int]
    generation_token_count: None


class ContextResponse(BaseModel):
    project_id: UUID
    snapshot_id: UUID
    query: str
    ranking_policy: Literal["local_cross_encoder", "retrieval_order"]
    policy: ContextPolicyResponse
    processing_run_id: UUID
    processing_revision: int
    processing_contract: dict[str, object]
    embedding_run_id: UUID | None
    embedding_run: EmbeddingRunResponse | None
    embedding_contract: EmbeddingContractResponse | None
    retrieval: ContextRetrievalResponse
    reranker_state: Literal["completed", "not_requested", "not_needed"]
    reranker_contract: RerankerContractResponse | None
    item_count: int
    items: list[ContextEvidenceResponse]
    decisions: list[ContextDecisionResponse]
    budget: ContextBudgetResponse
