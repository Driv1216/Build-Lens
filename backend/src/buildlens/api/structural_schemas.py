from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SpanResponse(BaseModel):
    start_line: int | None
    end_line: int | None
    start_column: int | None
    end_column: int | None


class ExpressionResponse(BaseModel):
    text: str | None
    span: SpanResponse | None
    omitted: bool
    truncated: bool


class DiagnosticResponse(BaseModel):
    code: str
    message: str
    severity: Literal["warning", "error"]
    span: SpanResponse | None


class StructuralAnalysisResponse(BaseModel):
    analysis_id: UUID
    snapshot_id: UUID
    started_at: datetime
    completed_at: datetime
    outcome: Literal["complete", "partial", "failed"]
    parser_version: str
    runtime_version: str
    policy_version: str
    limits: dict[str, int]
    summary: dict[str, int]
    warnings: list[DiagnosticResponse]
    revision: int = 0
    quarantined: bool = False
    superseded: bool = False
    captured_predecessor_analysis_id: UUID | None = None
    producer_contract: dict[str, str] | None = None
    reuse_count: int = 0
    reparse_count: int = 0
    observation_note: str = (
        "Verified artifacts are tied to the named snapshot's bytes; unverified evidence is not "
        "authoritative."
    )


class ExtractionSummaryResponse(BaseModel):
    """One additive extractor pass; ``contract_current`` is per-language authority (M4)."""

    extractor: str
    outcome: Literal["extracted", "partial", "failed", "skipped"]
    execution_mode: Literal["extracted", "reused"]
    contract_digest: str
    contract_current: bool
    diagnostics: list[DiagnosticResponse] = []


class ArtifactAnalysisResponse(BaseModel):
    id: UUID
    artifact_id: UUID
    locator: str
    outcome: Literal["parsed", "failed", "skipped"]
    read_started_at: datetime | None
    read_completed_at: datetime | None
    encoding: str | None
    line_count: int | None
    is_package_initializer: bool | None
    symbol_count: int
    import_count: int
    diagnostics: list[DiagnosticResponse]
    input_fingerprint: str | None = None
    snapshot_verified: bool = False
    execution_mode: Literal["parsed", "reused"] = "parsed"
    reuse_source_artifact_analysis_id: UUID | None = None
    language: str | None = None
    declaration_extraction_id: UUID | None = None
    extractions: list[ExtractionSummaryResponse] = []


class SymbolSummaryResponse(BaseModel):
    locator: str | None = None
    id: UUID
    artifact_analysis_id: UUID
    parent_id: UUID | None
    kind: Literal["function", "class", "method", "interface", "type_alias", "enum", "namespace"]
    name: str
    qualified_name: str
    is_async: bool
    declaration_span: SpanResponse
    full_start_line: int | None
    stable_identity_id: UUID | None = None
    continuity_reason: str | None = None
    state_fingerprint: str | None = None
    declaration_form: str | None = None
    declaration_space: Literal["value", "type", "namespace"] | None = None
    name_source: str | None = None
    exported: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    additional_spans: list[tuple[int, int]] = []


class ParameterResponse(BaseModel):
    name: str
    kind: str
    has_default: bool
    default: ExpressionResponse | None
    annotation: ExpressionResponse | None
    span: SpanResponse | None


class DecoratorResponse(BaseModel):
    ordinal: int
    form: str
    expression: ExpressionResponse
    span: SpanResponse | None


class SymbolDetailResponse(SymbolSummaryResponse):
    parameters: list[ParameterResponse] | None
    return_annotation: ExpressionResponse | None
    decorators: list[DecoratorResponse]
    type_parameters: list[ExpressionResponse]


class ImportReferenceResponse(BaseModel):
    locator: str | None = None
    id: UUID
    artifact_analysis_id: UUID
    owner_symbol_id: UUID | None
    form: Literal["import", "from", "es_import", "es_import_type", "dynamic_import", "require"]
    from_module: str | None
    name: str
    alias: str | None
    level: int
    statement_ordinal: int
    item_ordinal: int
    span: SpanResponse
    resolution_status: Literal["unresolved"]
    resolution_reason: Literal["not_attempted"]


class StructuralPage(BaseModel):
    analysis_id: UUID | None
    total: int
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)


class ArtifactAnalysisPageResponse(StructuralPage):
    items: list[ArtifactAnalysisResponse]


class SymbolPageResponse(StructuralPage):
    items: list[SymbolSummaryResponse]


class ImportPageResponse(StructuralPage):
    items: list[ImportReferenceResponse]
