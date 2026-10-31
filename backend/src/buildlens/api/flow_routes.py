"""Stage 15 flow evidence HTTP contracts (Milestone A: extraction, status, coverage, inspection).

Every read accepts ``run_id``. Without it, the active run must be fresh (409
``FLOW_EVIDENCE_REQUIRED`` / ``FLOW_EVIDENCE_STALE``); explicit history is labelled with its
``freshness_reasons``. Display texts are returned beside, never inside, analysis facts.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from buildlens.api.dependencies import get_flow_evidence_service
from buildlens.application.flow_evidence import FlowEvidenceService
from buildlens.domain.flow_evidence import ArtifactFlowCoverage, FlowEvidenceRun, FlowScope

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/snapshots/{snapshot_id}/flow",
    tags=["flow"],
)


class FlowEvidenceRunResponse(BaseModel):
    run_id: UUID
    snapshot_id: UUID
    revision: int
    started_at: datetime
    completed_at: datetime
    outcome: str
    structural_analysis_id: UUID
    structural_revision: int
    input_authority_digest: str
    evidence_digest: str | None
    contract: dict[str, Any]
    coverage: dict[str, Any]
    diagnostics: list[str]
    failure_code: str | None
    superseded: bool


class FlowEvidenceStatusResponse(BaseModel):
    latest_attempt: FlowEvidenceRunResponse | None
    active_run: FlowEvidenceRunResponse | None
    freshness: str
    freshness_reasons: list[str]
    contract: dict[str, Any]
    structure_available: bool


class FlowEvidenceHistoryResponse(BaseModel):
    items: list[FlowEvidenceRunResponse]
    total: int
    limit: int
    offset: int


class Scoped(BaseModel):
    run_id: UUID
    freshness_reasons: list[str]


class ArtifactCoverageResponse(BaseModel):
    artifact_analysis_id: UUID
    artifact_id: UUID
    locator: str
    language: str | None
    state: str
    extractor: str | None
    execution_mode: str
    reuse_source_run_id: UUID | None
    evidence_digest: str | None
    scope_count: int
    callsite_count: int
    diagnostics: list[dict[str, Any]]


class CoveragePageResponse(Scoped):
    items: list[ArtifactCoverageResponse]
    total: int
    limit: int
    offset: int


class ScopeResponse(BaseModel):
    scope_key: str
    parent_scope_key: str | None
    owner_symbol_id: UUID | None
    kind: str
    callable_kind: str
    name: str | None
    span: list[int | None]
    offsets: list[int | None]
    truncated: str | None
    scope_digest: str
    cfg: dict[str, Any]
    summary: dict[str, Any]
    callsites: list[dict[str, Any]]
    jsx: list[dict[str, Any]]


class DisplayTextResponse(BaseModel):
    text_key: str
    text: str | None
    flags: list[str]


class ArtifactFlowResponse(Scoped):
    artifact_analysis_id: UUID
    scopes: list[ScopeResponse]
    display_texts: list[DisplayTextResponse]


class FrameworkFactResponse(BaseModel):
    ecosystem: str
    package: str
    version: str | None
    constraint: str | None
    source_kind: str
    locator: str
    scope: str


class FrameworkFactsResponse(Scoped):
    items: list[FrameworkFactResponse]


def run(x: FlowEvidenceRun) -> FlowEvidenceRunResponse:
    return FlowEvidenceRunResponse(
        run_id=x.id,
        snapshot_id=x.project_version_id,
        revision=x.revision,
        started_at=x.started_at,
        completed_at=x.completed_at,
        outcome=x.outcome,
        structural_analysis_id=x.structural_analysis_id,
        structural_revision=x.structural_revision,
        input_authority_digest=x.input_authority_digest,
        evidence_digest=x.evidence_digest,
        contract=x.contract,
        coverage=x.coverage,
        diagnostics=list(x.diagnostics),
        failure_code=x.failure_code,
        superseded=x.superseded,
    )


def status(
    service: FlowEvidenceService, project_id: UUID, snapshot_id: UUID
) -> FlowEvidenceStatusResponse:
    value = service.status(project_id, snapshot_id)
    return FlowEvidenceStatusResponse(
        latest_attempt=run(value.latest) if value.latest else None,
        active_run=run(value.active) if value.active else None,
        freshness=value.freshness,
        freshness_reasons=list(value.freshness_reasons),
        contract=value.contract,
        structure_available=value.structure_available,
    )


def coverage_row(c: ArtifactFlowCoverage) -> ArtifactCoverageResponse:
    return ArtifactCoverageResponse(
        artifact_analysis_id=c.artifact_analysis_id,
        artifact_id=c.artifact_id,
        locator=c.locator,
        language=c.language,
        state=c.state,
        extractor=c.extractor,
        execution_mode=c.execution_mode,
        reuse_source_run_id=c.reuse_source_run_id,
        evidence_digest=c.evidence_digest,
        scope_count=c.scope_count,
        callsite_count=c.callsite_count,
        diagnostics=list(c.diagnostics),
    )


def scope_row(s: FlowScope) -> ScopeResponse:
    portable = s.portable()
    return ScopeResponse(
        scope_key=s.key,
        parent_scope_key=s.parent_key,
        owner_symbol_id=s.owner_symbol_id,
        kind=s.kind,
        callable_kind=s.callable_kind,
        name=s.name,
        span=portable["span"],
        offsets=portable["offsets"],
        truncated=s.truncated,
        scope_digest=s.digest,
        cfg=s.cfg,
        summary=s.summary,
        callsites=portable["callsites"],
        jsx=portable["jsx"],
    )


@router.post("/evidence", response_model=FlowEvidenceStatusResponse)
def extract(
    project_id: UUID,
    snapshot_id: UUID,
    service: FlowEvidenceService = Depends(get_flow_evidence_service),
) -> FlowEvidenceStatusResponse:
    service.extract(project_id, snapshot_id)
    return status(service, project_id, snapshot_id)


@router.get("/evidence", response_model=FlowEvidenceStatusResponse)
def get_status(
    project_id: UUID,
    snapshot_id: UUID,
    service: FlowEvidenceService = Depends(get_flow_evidence_service),
) -> FlowEvidenceStatusResponse:
    return status(service, project_id, snapshot_id)


@router.get("/evidence/runs", response_model=FlowEvidenceHistoryResponse)
def history(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: FlowEvidenceService = Depends(get_flow_evidence_service),
) -> FlowEvidenceHistoryResponse:
    values, total = service.history(project_id, snapshot_id, limit, offset)
    return FlowEvidenceHistoryResponse(
        items=[run(x) for x in values], total=total, limit=limit, offset=offset
    )


@router.get("/evidence/runs/{run_id}", response_model=FlowEvidenceRunResponse)
def get_run(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    service: FlowEvidenceService = Depends(get_flow_evidence_service),
) -> FlowEvidenceRunResponse:
    return run(service.run(project_id, snapshot_id, run_id))


@router.get("/evidence/coverage", response_model=CoveragePageResponse)
def coverage(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    state: str | None = Query(None, max_length=16),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: FlowEvidenceService = Depends(get_flow_evidence_service),
) -> CoveragePageResponse:
    value, reasons, rows, total = service.coverage(
        project_id, snapshot_id, run_id, state, limit, offset
    )
    return CoveragePageResponse(
        run_id=value.id,
        freshness_reasons=list(reasons),
        items=[coverage_row(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/evidence/artifacts/{artifact_analysis_id}", response_model=ArtifactFlowResponse)
def artifact(
    project_id: UUID,
    snapshot_id: UUID,
    artifact_analysis_id: UUID,
    run_id: UUID | None = None,
    service: FlowEvidenceService = Depends(get_flow_evidence_service),
) -> ArtifactFlowResponse:
    value, reasons, scopes, texts = service.artifact(
        project_id, snapshot_id, run_id, artifact_analysis_id
    )
    return ArtifactFlowResponse(
        run_id=value.id,
        freshness_reasons=list(reasons),
        artifact_analysis_id=artifact_analysis_id,
        scopes=[scope_row(s) for s in scopes],
        display_texts=[
            DisplayTextResponse(text_key=t.key, text=t.text, flags=list(t.flags)) for t in texts
        ],
    )


@router.get("/evidence/framework-facts", response_model=FrameworkFactsResponse)
def framework_facts(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    service: FlowEvidenceService = Depends(get_flow_evidence_service),
) -> FrameworkFactsResponse:
    value, reasons, facts = service.framework_facts(project_id, snapshot_id, run_id)
    return FrameworkFactsResponse(
        run_id=value.id,
        freshness_reasons=list(reasons),
        items=[
            FrameworkFactResponse(
                ecosystem=f.ecosystem,
                package=f.package,
                version=f.version,
                constraint=f.constraint,
                source_kind=f.source_kind,
                locator=f.locator,
                scope=f.scope,
            )
            for f in facts
        ],
    )
