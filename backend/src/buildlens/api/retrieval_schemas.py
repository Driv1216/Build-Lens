from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from buildlens.api.embedding_schemas import EmbeddingContractResponse, EmbeddingRunResponse
from buildlens.api.processing_schemas import ProcessingSpanResponse


class RetrievalFiltersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator_prefix: str | None = Field(default=None, max_length=2048)
    language: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=32)


class RetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["exact", "lexical", "semantic", "hybrid"]
    query: str
    top_k: int = Field(default=10, ge=1, le=50)
    filters: RetrievalFiltersRequest | None = None


class RetrievalPolicyResponse(BaseModel):
    version: str
    candidate_depth: int
    rrf_constant: int
    exact_precedence: bool


class ComponentStatusResponse(BaseModel):
    state: Literal["not_requested", "completed"]
    candidate_count: int
    candidate_count_is_bounded: bool = True
    truncated: bool


class ExactMatchResponse(BaseModel):
    field: Literal["symbol_name", "symbol_qualified_name", "locator"]
    value: str


class LexicalProvenanceResponse(BaseModel):
    rank: int
    score: float
    field_scores: dict[Literal["content", "symbol", "path", "metadata"], float]


class SemanticProvenanceResponse(BaseModel):
    rank: int
    cosine_similarity: float
    embedding_id: UUID
    embedding_run_id: UUID
    logical_embedding_key: str
    input_hash: str
    contract_digest: str
    window_count: int
    execution_mode: Literal["generated", "reused"]
    reuse_source_embedding_id: UUID | None


class HybridProvenanceResponse(BaseModel):
    lexical_contribution: float
    semantic_contribution: float


class RetrievalHitResponse(BaseModel):
    rank: int
    score_kind: Literal["exact", "lexical", "cosine", "rrf"]
    score: float | None
    exact_rank: int | None
    exact_matches: list[ExactMatchResponse]
    lexical: LexicalProvenanceResponse | None
    semantic: SemanticProvenanceResponse | None
    hybrid: HybridProvenanceResponse | None
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


class RetrievalComponentsResponse(BaseModel):
    exact: ComponentStatusResponse
    lexical: ComponentStatusResponse
    semantic: ComponentStatusResponse


class RetrievalResponse(BaseModel):
    requested_mode: Literal["exact", "lexical", "semantic", "hybrid"]
    executed_mode: Literal["exact", "lexical", "semantic", "hybrid"]
    policy: RetrievalPolicyResponse
    project_id: UUID
    snapshot_id: UUID
    processing_run_id: UUID
    processing_revision: int
    processing_coverage: dict[str, int]
    embedding_run: EmbeddingRunResponse | None
    embedding_contract: EmbeddingContractResponse | None
    components: RetrievalComponentsResponse
    candidate_depth: int
    candidates_truncated: bool
    returned_count: int
    hits: list[RetrievalHitResponse]
