"""HTTP contracts over the public processing application interface."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from buildlens.api.dependencies import get_processing_service
from buildlens.api.processing_schemas import (
    ArtifactProcessingResponse,
    ChunkPageResponse,
    ChunkResponse,
    ProcessingArtifactPageResponse,
    ProcessingDiagnosticResponse,
    ProcessingHistoryResponse,
    ProcessingRunResponse,
    ProcessingSpanResponse,
    ProcessingStatusResponse,
)
from buildlens.application.processing import ProcessingService
from buildlens.domain.processing import (
    ArtifactProcessing,
    Chunk,
    ChunkSpan,
    ProcessingDiagnostic,
    ProcessingRun,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/snapshots/{snapshot_id}/processing", tags=["processing"]
)


def span(x: ChunkSpan) -> ProcessingSpanResponse:
    return ProcessingSpanResponse(
        start_offset=x.start_offset,
        end_offset=x.end_offset,
        start_line=x.start_line,
        end_line=x.end_line,
        start_column=x.start_column,
        end_column=x.end_column,
    )


def diag(x: ProcessingDiagnostic) -> ProcessingDiagnosticResponse:
    return ProcessingDiagnosticResponse(
        code=x.code, message=x.message, severity=x.severity, span=span(x.span) if x.span else None
    )


def run(x: ProcessingRun) -> ProcessingRunResponse:
    return ProcessingRunResponse(
        run_id=x.id,
        snapshot_id=x.project_version_id,
        revision=x.revision,
        started_at=x.started_at,
        completed_at=x.completed_at,
        outcome=x.outcome,
        structural_analysis_id=x.structural_analysis_id,
        coverage=x.coverage,
        diagnostics=[diag(v) for v in x.diagnostics],
        processing_contract=x.processing_contract,
        quarantined=x.quarantined,
        superseded=x.superseded,
    )


def artifact(x: ArtifactProcessing) -> ArtifactProcessingResponse:
    return ArtifactProcessingResponse(
        id=x.id,
        artifact_id=x.artifact_id,
        locator=x.locator,
        outcome=x.outcome,
        encoding=x.encoding,
        input_fingerprint=x.input_fingerprint,
        execution_mode=x.execution_mode,
        reuse_source_artifact_processing_id=x.reuse_source_artifact_processing_id,
        structural_artifact_analysis_id=x.structural_artifact_analysis_id,
        result_digest=x.result_digest,
        chunk_count=x.stored_chunk_count if x.stored_chunk_count is not None else len(x.chunks),
        diagnostics=[diag(v) for v in x.diagnostics],
    )


def chunk(x: Chunk, content: bool = False) -> ChunkResponse:
    return ChunkResponse(
        id=x.id,
        artifact_processing_id=x.artifact_processing_id,
        stable_key=x.stable_key,
        ordinal=x.ordinal,
        unit_anchor=x.unit_anchor,
        part_index=x.part_index,
        kind=x.kind,
        content_hash=x.content_hash,
        span=span(x.span),
        symbol_id=x.symbol_id,
        symbol_identity_id=x.symbol_identity_id,
        heading_path=list(x.heading_path),
        json_path=x.json_path,
        content=x.content if content else None,
        freshness_reasons=list(x.freshness_reasons),
        locator=x.locator,
        artifact_id=x.artifact_id,
        run_id=x.run_id,
        snapshot_id=x.snapshot_id,
    )


def status(
    service: ProcessingService, project_id: UUID, snapshot_id: UUID
) -> ProcessingStatusResponse:
    value = service.status(project_id, snapshot_id)
    return ProcessingStatusResponse(
        latest_attempt=run(value.latest_attempt) if value.latest_attempt else None,
        active_run=run(value.active_run) if value.active_run else None,
        active_freshness=value.active_freshness,
        freshness_reasons=list(value.freshness_reasons),
    )


@router.post("", response_model=ProcessingStatusResponse)
def process(
    project_id: UUID,
    snapshot_id: UUID,
    service: ProcessingService = Depends(get_processing_service),
) -> ProcessingStatusResponse:
    service.process(project_id, snapshot_id)
    return status(service, project_id, snapshot_id)


@router.get("", response_model=ProcessingStatusResponse)
def get_status(
    project_id: UUID,
    snapshot_id: UUID,
    service: ProcessingService = Depends(get_processing_service),
) -> ProcessingStatusResponse:
    return status(service, project_id, snapshot_id)


@router.get("/runs", response_model=ProcessingHistoryResponse)
def history(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: ProcessingService = Depends(get_processing_service),
) -> ProcessingHistoryResponse:
    values, total = service.history(project_id, snapshot_id, limit, offset)
    return ProcessingHistoryResponse(
        items=[run(x) for x in values], total=total, limit=limit, offset=offset
    )


@router.get("/runs/{run_id}", response_model=ProcessingRunResponse)
def get_run(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    service: ProcessingService = Depends(get_processing_service),
) -> ProcessingRunResponse:
    return run(service.run(project_id, snapshot_id, run_id))


@router.get("/artifacts", response_model=ProcessingArtifactPageResponse)
def artifacts(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: ProcessingService = Depends(get_processing_service),
) -> ProcessingArtifactPageResponse:
    selected, values, total = service.artifact_page(project_id, snapshot_id, run_id, limit, offset)
    authority = service.status(project_id, snapshot_id)
    return ProcessingArtifactPageResponse(
        run_id=selected.id,
        latest_attempt_id=authority.latest_attempt.id if authority.latest_attempt else None,
        active_run_id=authority.active_run.id if authority.active_run else None,
        total=total,
        limit=limit,
        offset=offset,
        items=[artifact(x) for x in values],
    )


@router.get("/chunks", response_model=ChunkPageResponse)
def chunks(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: ProcessingService = Depends(get_processing_service),
) -> ChunkPageResponse:
    selected, values, total = service.chunk_page(project_id, snapshot_id, run_id, limit, offset)
    authority = service.status(project_id, snapshot_id)
    return ChunkPageResponse(
        run_id=selected.id,
        latest_attempt_id=authority.latest_attempt.id if authority.latest_attempt else None,
        active_run_id=authority.active_run.id if authority.active_run else None,
        total=total,
        limit=limit,
        offset=offset,
        freshness_reasons=list(service.freshness(project_id, snapshot_id, selected)),
        items=[chunk(x) for x in values],
    )


@router.get("/chunks/{chunk_id}", response_model=ChunkResponse)
def get_chunk(
    project_id: UUID,
    snapshot_id: UUID,
    chunk_id: UUID,
    run_id: UUID | None = None,
    service: ProcessingService = Depends(get_processing_service),
) -> ChunkResponse:
    return chunk(service.chunk(project_id, snapshot_id, chunk_id, run_id), True)
