from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from buildlens.api.processing_schemas import ProcessingSpanResponse


class ModelCacheResponse(BaseModel):
    available: bool
    path: str
    reason: str | None = None


class EmbeddingContractResponse(BaseModel):
    digest: str
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


class EmbeddingRunResponse(BaseModel):
    run_id: UUID
    project_id: UUID
    snapshot_id: UUID
    processing_run_id: UUID
    processing_revision: int
    revision: int
    contract: EmbeddingContractResponse
    membership_digest: str
    coverage: dict[str, int]
    limits: dict[str, object]
    diagnostics: list[str]
    chunk_count: int
    started_at: datetime
    completed_at: datetime
    outcome: Literal["complete", "failed"]
    superseded: bool
    quarantined: bool


class EmbeddingStatusResponse(BaseModel):
    model: ModelCacheResponse
    contract: EmbeddingContractResponse
    prepared: bool
    search_available: bool
    active_processing_run_id: UUID | None
    freshness_reasons: list[str] = Field(default_factory=list)
    latest_attempt: EmbeddingRunResponse | None
    active_run: EmbeddingRunResponse | None


class PrepareEmbeddingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SemanticSearchFiltersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator_prefix: str | None = Field(default=None, max_length=2048)
    language: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=32)


class SemanticSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    top_k: int = Field(default=10, ge=1, le=50)
    filters: SemanticSearchFiltersRequest | None = None


class SemanticSearchHitResponse(BaseModel):
    score: float
    embedding_id: UUID
    embedding_run_id: UUID
    logical_embedding_key: str
    embedding_key: str
    input_hash: str
    contract_digest: str
    window_count: int
    execution_mode: Literal["generated", "reused"]
    reuse_source_embedding_id: UUID | None
    project_id: UUID
    snapshot_id: UUID
    processing_run_id: UUID
    processing_revision: int
    artifact_processing_id: UUID
    artifact_id: UUID
    locator: str
    chunk_id: UUID
    stable_key: str
    content_hash: str
    kind: str
    ordinal: int
    span: ProcessingSpanResponse
    symbol_id: UUID | None = None
    symbol_identity_id: UUID | None = None
    heading_path: list[str] = Field(default_factory=list)
    json_path: str | None = None
    content: str


class SemanticSearchResponse(BaseModel):
    contract: EmbeddingContractResponse
    embedding_run: EmbeddingRunResponse
    hits: list[SemanticSearchHitResponse]


class EmbeddingHistoryResponse(BaseModel):
    items: list[EmbeddingRunResponse]
    total: int
    limit: int
    offset: int


class ChunkEmbeddingResponse(BaseModel):
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
    execution_mode: Literal["generated", "reused"]
    reuse_source_embedding_id: UUID | None


class ChunkEmbeddingPageResponse(BaseModel):
    run_id: UUID
    total: int
    limit: int
    offset: int
    items: list[ChunkEmbeddingResponse]
