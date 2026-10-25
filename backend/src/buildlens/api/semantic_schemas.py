from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CatalogRunResponse(BaseModel):
    run_id: UUID
    snapshot_id: UUID
    revision: int
    started_at: datetime
    completed_at: datetime
    outcome: Literal["complete", "partial", "failed"]
    structural_analysis_id: UUID
    structural_revision: int
    processing_run_id: UUID
    processing_revision: int
    input_authority_digest: str
    catalog_digest: str | None
    portable_digest: str | None
    catalog_contract: dict[str, object]
    coverage: dict[str, object]
    diagnostics: list[str]
    superseded: bool
    quarantined: bool


class CatalogStatusResponse(BaseModel):
    latest_attempt: CatalogRunResponse | None
    active_run: CatalogRunResponse | None
    freshness: Literal["fresh", "stale", "none"]
    freshness_reasons: list[str] = Field(default_factory=list)
    contract: dict[str, object]


class CatalogHistoryResponse(BaseModel):
    items: list[CatalogRunResponse]
    total: int
    limit: int
    offset: int


class EntitySummaryResponse(BaseModel):
    entity_key: str
    kind: Literal["file", "symbol", "component", "hook", "api_endpoint", "db_table"]
    language: str | None
    locator: str
    name: str
    qualified_name: str | None
    symbol_kind: str | None
    start_line: int | None
    end_line: int | None
    subject_entity_key: str | None
    stored_coverage: Literal["complete", "incomplete", "unavailable", "coordinate_mismatch"]
    coverage_reason: str | None
    context_coverage: Literal["full", "outline", "not_eligible"]
    context_reason: str | None
    package_digest: str | None
    package_bytes: int
    limitations: list[str]


class EntityPageResponse(BaseModel):
    run_id: UUID
    freshness_reasons: list[str] = Field(default_factory=list)
    total: int
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)
    items: list[EntitySummaryResponse]


class FactResponse(BaseModel):
    fact_digest: str
    fact_kind: str
    subject_entity_key: str
    # "parsed" for syntax facts; recognizer matches carry their labelled strength.
    basis: Literal["parsed", "pattern_with_local_evidence", "pattern_only"]
    rendered_text: str
    renderer_version: str
    source_refs: list[dict[str, object]]
    derived: dict[str, object]
    verification: str | None


class RelationResponse(BaseModel):
    kind: str
    source_entity_key: str
    target_entity_key: str | None
    target_text: str | None
    resolution_status: str | None
    fact_digest: str


class EvidenceChunkResponse(BaseModel):
    chunk_id: UUID
    stable_key: str
    content_hash: str
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    symbol_identity_id: UUID | None
    content: str | None
    verified: bool | None


class OmittedRegionResponse(BaseModel):
    entity_key: str
    start_line: int | None
    end_line: int | None


class EntityDetailResponse(BaseModel):
    run_id: UUID
    freshness_reasons: list[str] = Field(default_factory=list)
    entity: EntitySummaryResponse
    gaps: dict[str, object]
    package_chunks: list[str]
    omitted: list[OmittedRegionResponse]
    evidence: list[EvidenceChunkResponse]
    facts: list[FactResponse]
    neighbor_facts: list[FactResponse]
    outgoing: list[RelationResponse]
    incoming: list[RelationResponse]
    integrity_errors: list[str]
    related: dict[str, str] = Field(default_factory=dict)
