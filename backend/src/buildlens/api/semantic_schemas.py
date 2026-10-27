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
    interpretation_state: Literal["current", "stale", "absent"] | None = None


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


class PlanRequest(BaseModel):
    entity_keys: list[str] = Field(default_factory=list, max_length=200)
    locator_prefix: str | None = Field(None, max_length=1024)
    kinds: list[Literal["file", "symbol", "component", "hook", "api_endpoint", "db_table"]] = Field(
        default_factory=list
    )
    stale_only: bool = False
    mode: Literal["generate", "reuse_only"] = "generate"
    accept_profiles: list[str] | Literal["any"] | None = None


class LocatorResponse(BaseModel):
    artifact_identity_id: str | None = None
    locator: str
    role: str
    bytes: int


class PlanItemResponse(BaseModel):
    ordinal: int
    entity_key: str
    disposition: Literal["generate", "reuse", "ineligible"]
    reason: str | None
    context_coverage: str
    package_digest: str | None
    input_key: str | None
    request_digest: str | None
    request_bytes: int
    contributing_locators: list[LocatorResponse]
    reuse_interpretation_id: UUID | None
    reuse_profile_digest: str | None
    reuse_provider: str | None
    reuse_returned_model: str | None


class PlanResponse(BaseModel):
    plan_id: UUID
    snapshot_id: UUID
    plan_nonce: str
    plan_digest: str
    issued_at: datetime
    expires_at: datetime
    mode: Literal["generate", "reuse_only"]
    accept_profiles: list[str] | Literal["any"] | None
    selection: dict[str, object]
    provider_config: dict[str, object]
    provider_config_digest: str
    generator_profile_digest: str
    sharing_class: Literal["cloud", "loopback"]
    retention_note: str
    catalog_run_id: UUID
    structural_analysis_id: UUID
    processing_run_id: UUID
    index_run_id: UUID | None
    caps: dict[str, object]
    totals: dict[str, object]
    contributing_locators: list[dict[str, object]]
    items: list[PlanItemResponse]
    run_id: UUID | None


class ExecuteRequest(BaseModel):
    plan_id: UUID
    plan_digest: str = Field(pattern="^[0-9a-f]{64}$")
    confirm: bool


class AttemptResponse(BaseModel):
    attempt_id: UUID
    entity_key: str
    attempt_no: int
    request_digest: str
    started_at: datetime
    outcome: str | None
    returned_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    rejection_codes: list[str]
    late_receipts: int


class GenerationRunResponse(BaseModel):
    run_id: UUID
    snapshot_id: UUID
    plan_id: UUID
    plan_digest: str
    state: Literal["executing", "complete", "partial", "failed", "superseded", "abandoned"]
    catalog_run_id: UUID
    captured_index_run_id: UUID | None
    caps: dict[str, object]
    attempts_started: int
    started_at: datetime
    completed_at: datetime | None
    deadline_at: datetime
    totals: dict[str, object]
    diagnostics: list[str]
    attempts: list[AttemptResponse] = Field(default_factory=list)


class GenerationRunHistoryResponse(BaseModel):
    items: list[GenerationRunResponse]
    total: int
    limit: int
    offset: int


class CitationResponse(BaseModel):
    evidence_id: str
    kind: Literal["chunk", "fact"]
    basis: str
    locator: str | None
    start_line: int | None
    end_line: int | None
    stable_key: str | None
    fact_digest: str | None
    start: int
    end: int
    verification: str | None
    quote: str | None


class ClaimResponse(BaseModel):
    ordinal: int
    category: str
    # Withheld (null) when any citation fails read-time verification.
    text: str | None
    status: Literal["cited", "conflicts_with_fact"]
    basis: Literal["code", "documentation", "facts"]
    basis_label: Literal["cited", "per_documentation", "from_declarations_only"]
    assertion: dict[str, object] | None
    assertion_outcome: str | None
    scope_warning: bool
    citations: list[CitationResponse]


class InterpretationResponse(BaseModel):
    interpretation_id: UUID
    entity_key: str
    snapshot_id: UUID
    run_id: UUID
    attempt_id: UUID
    input_key: str
    generator_profile_digest: str
    provider: str
    requested_model: str
    returned_model: str | None
    prompt_version: str
    schema_version: str
    validator_version: str
    status: str
    created_at: datetime
    unknowns: list[str]
    context_coverage: str | None
    claims: list[ClaimResponse]
    integrity_errors: list[str]


class EntryResponse(BaseModel):
    entity_key: str
    state: Literal["current", "stale", "absent"]
    stale_reasons: list[str]
    mode: Literal["generated", "reused", "retained"] | None
    reused_from_profile: str | None
    index_run_id: UUID | None
    interpretation: InterpretationResponse | None


class EntryHistoryResponse(BaseModel):
    entity_key: str
    items: list[EntryResponse]
