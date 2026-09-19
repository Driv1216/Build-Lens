from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from buildlens.api.dependencies import get_structural_analysis_service
from buildlens.api.snapshot_routes import _detail
from buildlens.api.structural_schemas import (
    ArtifactAnalysisPageResponse,
    ArtifactAnalysisResponse,
    DecoratorResponse,
    DiagnosticResponse,
    ExpressionResponse,
    ImportPageResponse,
    ImportReferenceResponse,
    ParameterResponse,
    SpanResponse,
    StructuralAnalysisResponse,
    SymbolDetailResponse,
    SymbolPageResponse,
    SymbolSummaryResponse,
)
from buildlens.application.projects import ProjectNotFoundError
from buildlens.application.snapshots import SnapshotNotFoundError
from buildlens.application.structural import (
    AnalysisBusyError,
    AnalysisConflictError,
    AnalysisUnavailableError,
    StructuralAnalysisService,
)
from buildlens.domain.structural import (
    ArtifactAnalysis,
    ImportReference,
    ObservedExpression,
    ParseDiagnostic,
    SourceSpan,
    StructuralAnalysis,
    Symbol,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/snapshots/{snapshot_id}/structure", tags=["structure"]
)


def _span(value: SourceSpan | None) -> SpanResponse | None:
    return (
        None
        if value is None
        else SpanResponse(
            start_line=value.start_line,
            end_line=value.end_line,
            start_column=value.start_column,
            end_column=value.end_column,
        )
    )


def _expression(value: ObservedExpression | None) -> ExpressionResponse | None:
    return (
        None
        if value is None
        else ExpressionResponse(
            text=value.text,
            span=_span(value.span),
            omitted=value.omitted,
            truncated=value.truncated,
        )
    )


def _diagnostic(value: ParseDiagnostic) -> DiagnosticResponse:
    return DiagnosticResponse(
        code=value.code, message=value.message, severity=value.severity, span=_span(value.span)
    )


def _analysis(value: StructuralAnalysis) -> StructuralAnalysisResponse:
    return StructuralAnalysisResponse(
        analysis_id=value.id,
        snapshot_id=value.project_version_id,
        started_at=value.started_at,
        completed_at=value.completed_at,
        outcome=value.outcome,
        parser_version=value.parser_version,
        runtime_version=value.runtime_version,
        policy_version=value.policy_version,
        limits=value.limits,
        summary=value.summary,
        warnings=[_diagnostic(item) for item in value.warnings],
        revision=value.revision,
        quarantined=value.quarantined,
        superseded=value.superseded,
        captured_predecessor_analysis_id=value.captured_predecessor_analysis_id,
        producer_contract=value.producer_contract,
        reuse_count=value.summary.get("reused", 0),
        reparse_count=value.summary.get("reparsed", 0),
    )


def _artifact(value: ArtifactAnalysis) -> ArtifactAnalysisResponse:
    return ArtifactAnalysisResponse(
        id=value.id,
        artifact_id=value.artifact_id,
        locator=value.locator,
        outcome=value.outcome,
        read_started_at=value.read_started_at,
        read_completed_at=value.read_completed_at,
        encoding=value.encoding,
        line_count=value.line_count,
        is_package_initializer=value.is_package_initializer,
        symbol_count=value.stored_symbol_count
        if value.stored_symbol_count is not None
        else len(value.symbols),
        import_count=value.stored_import_count
        if value.stored_import_count is not None
        else len(value.imports),
        diagnostics=[_diagnostic(item) for item in value.diagnostics],
        input_fingerprint=value.input_fingerprint,
        snapshot_verified=value.snapshot_verified,
        execution_mode=value.execution_mode,
        reuse_source_artifact_analysis_id=value.reuse_source_artifact_analysis_id,
    )


def _symbol(record: Symbol) -> SymbolSummaryResponse:
    return SymbolSummaryResponse(
        id=record.id,
        artifact_analysis_id=record.artifact_analysis_id,
        locator=record.locator,
        parent_id=record.parent_id,
        kind=record.kind,
        name=record.name,
        qualified_name=record.qualified_name,
        is_async=record.is_async,
        declaration_span=SpanResponse(
            start_line=record.declaration_span.start_line,
            end_line=record.declaration_span.end_line,
            start_column=record.declaration_span.start_column,
            end_column=record.declaration_span.end_column,
        ),
        full_start_line=record.full_start_line,
        stable_identity_id=record.stable_identity_id,
        continuity_reason=record.continuity_reason,
        state_fingerprint=record.state_fingerprint,
    )


def _import(record: ImportReference) -> ImportReferenceResponse:
    return ImportReferenceResponse(
        id=record.id,
        artifact_analysis_id=record.artifact_analysis_id,
        locator=record.locator,
        owner_symbol_id=record.owner_symbol_id,
        form=record.form,
        from_module=record.from_module,
        name=record.name,
        alias=record.alias,
        level=record.level,
        statement_ordinal=record.statement_ordinal,
        item_ordinal=record.item_ordinal,
        span=SpanResponse(
            start_line=record.span.start_line,
            end_line=record.span.end_line,
            start_column=record.span.start_column,
            end_column=record.span.end_column,
        ),
        resolution_status="unresolved",
        resolution_reason="not_attempted",
    )


def _raise(error: Exception) -> None:
    if isinstance(error, ProjectNotFoundError):
        raise HTTPException(404, _detail("PROJECT_NOT_FOUND", "Project was not found.")) from error
    if isinstance(error, SnapshotNotFoundError):
        raise HTTPException(
            404, _detail("SNAPSHOT_NOT_FOUND", "Repository snapshot was not found.")
        ) from error
    if isinstance(error, AnalysisBusyError):
        raise HTTPException(409, _detail("ANALYSIS_BUSY", str(error))) from error
    if isinstance(error, AnalysisConflictError):
        raise HTTPException(409, _detail("ANALYSIS_CONFLICT", str(error))) from error
    if isinstance(error, ValueError):
        raise HTTPException(422, _detail("ANALYSIS_PREREQUISITE_INVALID", str(error))) from error
    if isinstance(error, AnalysisUnavailableError):
        raise HTTPException(503, _detail("ANALYSIS_UNAVAILABLE", str(error))) from error
    raise error


def _require_snapshot(
    service: StructuralAnalysisService, project_id: UUID, snapshot_id: UUID
) -> None:
    service.require_snapshot(project_id, snapshot_id)


@router.post("", response_model=StructuralAnalysisResponse)
def analyse(
    project_id: UUID,
    snapshot_id: UUID,
    service: StructuralAnalysisService = Depends(get_structural_analysis_service),
) -> StructuralAnalysisResponse:
    try:
        return _analysis(service.analyse(project_id, snapshot_id))
    except Exception as error:
        _raise(error)
        raise


@router.get("", response_model=StructuralAnalysisResponse)
def latest(
    project_id: UUID,
    snapshot_id: UUID,
    service: StructuralAnalysisService = Depends(get_structural_analysis_service),
) -> StructuralAnalysisResponse:
    try:
        _require_snapshot(service, project_id, snapshot_id)
        value = service.latest_attempt(project_id, snapshot_id)
        if value is None:
            raise HTTPException(
                409, _detail("ANALYSIS_NOT_FOUND", "No structural analysis has been committed.")
            )
        return _analysis(value)
    except HTTPException:
        raise
    except Exception as error:
        _raise(error)
        raise


@router.get("/artifacts", response_model=ArtifactAnalysisPageResponse)
def artifacts(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    outcome: str | None = None,
    run_id: UUID | None = None,
    service: StructuralAnalysisService = Depends(get_structural_analysis_service),
) -> ArtifactAnalysisPageResponse:
    _require_snapshot(service, project_id, snapshot_id)
    items, total, analysis_id = service.artifact_page(
        project_id, snapshot_id, limit, offset, outcome, run_id
    )
    return ArtifactAnalysisPageResponse(
        analysis_id=analysis_id,
        total=total,
        limit=limit,
        offset=offset,
        items=[_artifact(item) for item in items],
    )


@router.get("/symbols", response_model=SymbolPageResponse)
def symbols(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    artifact_id: UUID | None = None,
    kind: str | None = None,
    name: str | None = None,
    run_id: UUID | None = None,
    service: StructuralAnalysisService = Depends(get_structural_analysis_service),
) -> SymbolPageResponse:
    _require_snapshot(service, project_id, snapshot_id)
    items, total, analysis_id = service.symbol_page(
        project_id, snapshot_id, limit, offset, artifact_id, kind, name, run_id
    )
    return SymbolPageResponse(
        analysis_id=analysis_id,
        total=total,
        limit=limit,
        offset=offset,
        items=[_symbol(item) for item in items],
    )


@router.get("/symbols/{symbol_id}", response_model=SymbolDetailResponse)
def symbol(
    project_id: UUID,
    snapshot_id: UUID,
    symbol_id: UUID,
    run_id: UUID | None = None,
    service: StructuralAnalysisService = Depends(get_structural_analysis_service),
) -> SymbolDetailResponse:
    _require_snapshot(service, project_id, snapshot_id)
    record = service.symbol(project_id, snapshot_id, symbol_id, run_id)
    if record is None:
        raise HTTPException(404, _detail("SYMBOL_NOT_FOUND", "Symbol was not found."))
    summary = _symbol(record)
    signature = record.signature
    return SymbolDetailResponse(
        **summary.model_dump(),
        parameters=[
            ParameterResponse(
                name=x.name,
                kind=x.kind,
                has_default=x.has_default,
                default=_expression(x.default),
                annotation=_expression(x.annotation),
                span=_span(x.span),
            )
            for x in signature.parameters
        ]
        if signature
        else None,
        return_annotation=_expression(signature.return_annotation) if signature else None,
        decorators=[
            DecoratorResponse(
                ordinal=x.ordinal,
                form=x.form,
                expression=cast(ExpressionResponse, _expression(x.expression)),
                span=_span(x.span),
            )
            for x in record.decorators
        ],
        type_parameters=[cast(ExpressionResponse, _expression(x)) for x in record.type_parameters],
    )


@router.get("/imports", response_model=ImportPageResponse)
def imports(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    artifact_id: UUID | None = None,
    owner_id: UUID | None = None,
    run_id: UUID | None = None,
    service: StructuralAnalysisService = Depends(get_structural_analysis_service),
) -> ImportPageResponse:
    _require_snapshot(service, project_id, snapshot_id)
    items, total, analysis_id = service.import_page(
        project_id, snapshot_id, limit, offset, artifact_id, owner_id, run_id
    )
    return ImportPageResponse(
        analysis_id=analysis_id,
        total=total,
        limit=limit,
        offset=offset,
        items=[_import(item) for item in items],
    )


@router.get("/runs")
def history(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: StructuralAnalysisService = Depends(get_structural_analysis_service),
) -> dict[str, object]:
    items, total = service.history(project_id, snapshot_id, limit, offset)
    return {
        "items": [_analysis(x) for x in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/runs/{run_id}", response_model=StructuralAnalysisResponse)
def historical_run(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    service: StructuralAnalysisService = Depends(get_structural_analysis_service),
) -> StructuralAnalysisResponse:
    value = service.run(project_id, snapshot_id, run_id)
    if value is None:
        raise HTTPException(404, _detail("ANALYSIS_NOT_FOUND", "Structural run was not found."))
    return _analysis(value)


@router.get("/status")
def status(
    project_id: UUID,
    snapshot_id: UUID,
    service: StructuralAnalysisService = Depends(get_structural_analysis_service),
) -> dict[str, object]:
    latest = service.latest_attempt(project_id, snapshot_id)
    active = service.latest_analysis(project_id, snapshot_id)
    return {
        "latest_attempt": _analysis(latest) if latest else None,
        "active_run": _analysis(active) if active else None,
    }
