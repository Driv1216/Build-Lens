"""Immutable Stage 5 artifact-processing evidence records.

Coordinates are decoded Python/Unicode character coordinates.  They are not
AST byte columns and content is always the exact persisted source slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

ProcessingOutcome = Literal["complete", "partial", "failed"]
ArtifactProcessingOutcome = Literal[
    "processed", "empty", "excluded", "unsupported", "skipped", "failed"
]
Severity = Literal["warning", "error"]


@dataclass(frozen=True, slots=True)
class ChunkSpan:
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    start_column: int
    end_column: int


@dataclass(frozen=True, slots=True)
class ProcessingDiagnostic:
    code: str
    message: str
    severity: Severity
    span: ChunkSpan | None = None


@dataclass(frozen=True, slots=True)
class ProcessorContract:
    processor: str
    version: str
    boundary_version: str
    decoding: str
    span_convention: str = "decoded-unicode-half-open.v1"
    screening_version: str = "secret-screen.v2"

    @property
    def digest_material(self) -> str:
        return "|".join(
            (
                self.processor,
                self.version,
                self.boundary_version,
                self.decoding,
                self.span_convention,
                self.screening_version,
            )
        )


@dataclass(frozen=True, slots=True)
class Chunk:
    id: UUID
    artifact_processing_id: UUID
    stable_key: str
    ordinal: int
    unit_anchor: str
    part_index: int
    kind: str
    content: str
    content_hash: str
    span: ChunkSpan
    symbol_id: UUID | None = None
    symbol_identity_id: UUID | None = None
    heading_path: tuple[str, ...] = ()
    json_path: str | None = None
    locator: str | None = None
    artifact_id: UUID | None = None
    run_id: UUID | None = None
    snapshot_id: UUID | None = None
    freshness_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactProcessing:
    id: UUID
    processing_run_id: UUID
    project_version_id: UUID
    artifact_id: UUID
    locator: str
    outcome: ArtifactProcessingOutcome
    processor_contract: ProcessorContract | None
    input_fingerprint: str | None
    encoding: str | None
    diagnostics: tuple[ProcessingDiagnostic, ...] = ()
    chunks: tuple[Chunk, ...] = ()
    execution_mode: Literal["processed", "reused"] = "processed"
    reuse_source_artifact_processing_id: UUID | None = None
    structural_artifact_analysis_id: UUID | None = None
    result_digest: str | None = None
    dependency_fingerprint: str | None = None
    stored_chunk_count: int | None = None


@dataclass(frozen=True, slots=True)
class ProcessingRun:
    id: UUID
    project_version_id: UUID
    started_at: datetime
    completed_at: datetime
    outcome: ProcessingOutcome
    structural_analysis_id: UUID | None
    processing_contract: dict[str, object]
    limits: dict[str, int]
    coverage: dict[str, int]
    diagnostics: tuple[ProcessingDiagnostic, ...] = ()
    artifact_processings: tuple[ArtifactProcessing, ...] = ()
    revision: int = 0
    captured_active_processing_run_id: UUID | None = None
    captured_latest_revision: int = 0

    quarantined: bool = False
    superseded: bool = False

    @property
    def successful_evidence(self) -> bool:
        return any(value.outcome in {"processed", "empty"} for value in self.artifact_processings)
