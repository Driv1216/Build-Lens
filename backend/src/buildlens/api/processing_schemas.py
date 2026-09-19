from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ProcessingSpanResponse(BaseModel):
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    start_column: int
    end_column: int


class ProcessingDiagnosticResponse(BaseModel):
    code: str
    message: str
    severity: Literal["warning", "error"]
    span: ProcessingSpanResponse | None = None


class ChunkResponse(BaseModel):
    freshness_reasons: list[str] = Field(default_factory=list)
    id: UUID
    artifact_processing_id: UUID
    stable_key: str
    ordinal: int
    unit_anchor: str
    part_index: int
    kind: str
    content_hash: str
    span: ProcessingSpanResponse
    symbol_id: UUID | None = None
    symbol_identity_id: UUID | None = None
    heading_path: list[str] = []
    json_path: str | None = None
    content: str | None = None
    locator: str | None = None
    artifact_id: UUID | None = None
    run_id: UUID | None = None
    snapshot_id: UUID | None = None


class ArtifactProcessingResponse(BaseModel):
    id: UUID
    artifact_id: UUID
    locator: str
    outcome: Literal["processed", "empty", "excluded", "unsupported", "skipped", "failed"]
    encoding: str | None
    input_fingerprint: str | None
    execution_mode: Literal["processed", "reused"]
    reuse_source_artifact_processing_id: UUID | None
    structural_artifact_analysis_id: UUID | None
    result_digest: str | None
    chunk_count: int
    diagnostics: list[ProcessingDiagnosticResponse]


class ProcessingRunResponse(BaseModel):
    run_id: UUID
    snapshot_id: UUID
    revision: int
    started_at: datetime
    completed_at: datetime
    outcome: Literal["complete", "partial", "failed"]
    structural_analysis_id: UUID | None
    coverage: dict[str, int]
    diagnostics: list[ProcessingDiagnosticResponse]
    processing_contract: dict[str, object]
    quarantined: bool = False
    superseded: bool = False


class ProcessingStatusResponse(BaseModel):
    latest_attempt: ProcessingRunResponse | None
    active_run: ProcessingRunResponse | None
    active_freshness: Literal["fresh", "stale", "none"]
    freshness_reasons: list[str] = Field(default_factory=list)


class ProcessingPage(BaseModel):
    run_id: UUID
    latest_attempt_id: UUID | None
    active_run_id: UUID | None
    total: int
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)


class ProcessingArtifactPageResponse(ProcessingPage):
    items: list[ArtifactProcessingResponse]


class ChunkPageResponse(ProcessingPage):
    freshness_reasons: list[str] = Field(default_factory=list)
    items: list[ChunkResponse]


class ProcessingHistoryResponse(BaseModel):
    items: list[ProcessingRunResponse]
    total: int
    limit: int
    offset: int
