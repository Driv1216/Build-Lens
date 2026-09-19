"""Immutable records for an observed Python structure aggregate.

The records deliberately describe only what was observed while an explicit
analysis ran.  They contain neither source files nor snapshot-verification
claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

AnalysisOutcome = Literal["complete", "partial", "failed"]
ArtifactOutcome = Literal["parsed", "failed", "skipped"]
SymbolKind = Literal["function", "class", "method"]
Severity = Literal["warning", "error"]
ParameterKind = Literal[
    "positional_only",
    "positional_or_keyword",
    "variadic_positional",
    "keyword_only",
    "variadic_keyword",
]


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start_line: int | None
    end_line: int | None
    start_column: int | None = None
    end_column: int | None = None


@dataclass(frozen=True, slots=True)
class ObservedExpression:
    """A narrow reconstructed expression, never a whole source excerpt."""

    text: str | None
    span: SourceSpan | None
    omitted: bool = False
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    kind: ParameterKind
    has_default: bool
    default: ObservedExpression | None
    annotation: ObservedExpression | None
    span: SourceSpan | None


@dataclass(frozen=True, slots=True)
class Signature:
    parameters: tuple[Parameter, ...]
    return_annotation: ObservedExpression | None


@dataclass(frozen=True, slots=True)
class DecoratorObservation:
    ordinal: int
    form: str
    expression: ObservedExpression
    span: SourceSpan | None


@dataclass(frozen=True, slots=True)
class ParseDiagnostic:
    code: str
    message: str
    severity: Severity
    span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class Symbol:
    id: UUID
    artifact_analysis_id: UUID
    parent_id: UUID | None
    ordinal: int
    kind: SymbolKind
    name: str
    qualified_name: str
    is_async: bool
    declaration_span: SourceSpan
    full_start_line: int | None
    signature: Signature | None
    decorators: tuple[DecoratorObservation, ...] = ()
    type_parameters: tuple[ObservedExpression, ...] = ()
    stable_identity_id: UUID | None = None
    continuity_reason: str | None = None
    state_fingerprint: str | None = None
    locator: str | None = None


@dataclass(frozen=True, slots=True)
class ImportReference:
    id: UUID
    artifact_analysis_id: UUID
    owner_symbol_id: UUID | None
    statement_ordinal: int
    item_ordinal: int
    form: Literal["import", "from"]
    from_module: str | None
    name: str
    alias: str | None
    level: int
    span: SourceSpan
    resolution_status: Literal["unresolved"] = "unresolved"
    resolution_reason: Literal["not_attempted"] = "not_attempted"
    locator: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactAnalysis:
    id: UUID
    structural_analysis_id: UUID
    project_version_id: UUID
    artifact_id: UUID
    locator: str
    outcome: ArtifactOutcome
    read_started_at: datetime | None
    read_completed_at: datetime | None
    encoding: str | None
    line_count: int | None
    is_package_initializer: bool | None
    diagnostics: tuple[ParseDiagnostic, ...] = ()
    symbols: tuple[Symbol, ...] = ()
    imports: tuple[ImportReference, ...] = ()
    input_fingerprint: str | None = None
    snapshot_verified: bool = False
    execution_mode: Literal["parsed", "reused"] = "parsed"
    baseline_artifact_analysis_id: UUID | None = None
    reuse_source_artifact_analysis_id: UUID | None = None
    bytes_consumed: int = 0
    stored_symbol_count: int | None = None
    stored_import_count: int | None = None


@dataclass(frozen=True, slots=True)
class StructuralAnalysis:
    id: UUID
    project_version_id: UUID
    started_at: datetime
    completed_at: datetime
    outcome: AnalysisOutcome
    parser_version: str
    runtime_version: str
    policy_version: str
    limits: dict[str, int]
    warnings: tuple[ParseDiagnostic, ...] = ()
    artifact_analyses: tuple[ArtifactAnalysis, ...] = ()
    revision: int = 0
    captured_predecessor_analysis_id: UUID | None = None
    producer_contract: dict[str, str] | None = None

    quarantined: bool = False
    superseded: bool = False
    captured_active_analysis_id: UUID | None = None
    captured_latest_revision: int = 0
    summary_data: dict[str, int] | None = None

    @property
    def summary(self) -> dict[str, int]:
        if self.summary_data is not None:
            return self.summary_data
        values = {
            "modules": 0,
            "symbols": 0,
            "imports": 0,
            "failed": 0,
            "skipped": 0,
            "reused": 0,
            "reparsed": 0,
        }
        for artifact in self.artifact_analyses:
            if artifact.outcome == "parsed":
                values["modules"] += 1
                values["reused" if artifact.execution_mode == "reused" else "reparsed"] += 1
                values["symbols"] += len(artifact.symbols)
                values["imports"] += len(artifact.imports)
            elif artifact.outcome == "failed":
                values["failed"] += 1
            else:
                values["skipped"] += 1
        return values
