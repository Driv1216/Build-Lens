"""Stage 15 flow HTTP contracts: evidence (A), the flow model (B) and FlowTraces (C).

Every read accepts ``run_id``. Without it, the active run must be fresh (409
``FLOW_EVIDENCE_REQUIRED`` / ``FLOW_EVIDENCE_STALE``); explicit history is labelled with its
``freshness_reasons``. Display texts are returned beside, never inside, analysis facts.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from buildlens.api.dependencies import (
    get_flow_evidence_service,
    get_flow_explanation_service,
    get_flow_trace_service,
)
from buildlens.application.flow_evidence import FlowEvidenceService
from buildlens.application.flow_explanation import FlowExplanationService
from buildlens.application.flow_trace_service import FlowTraceService
from buildlens.domain.flow_evidence import ArtifactFlowCoverage, FlowEvidenceRun, FlowScope
from buildlens.domain.flow_explanation import ExplanationRun
from buildlens.domain.flow_trace import MAX_DEPTH, MAX_HOPS, TraceQuery

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


# ----------------------------------------------------------------------------- Milestone B

from buildlens.api.dependencies import get_flow_service  # noqa: E402
from buildlens.application.flow import FlowService  # noqa: E402
from buildlens.domain.flow import FlowEdge, FlowNode, FlowRun  # noqa: E402


class FlowRunResponse(BaseModel):
    run_id: UUID
    snapshot_id: UUID
    revision: int
    started_at: datetime
    completed_at: datetime
    outcome: str
    flow_evidence_run_id: UUID
    catalog_run_id: UUID
    architecture_run_id: UUID
    structural_analysis_id: UUID
    input_authority_digest: str
    graph_digest: str | None
    portable_digest: str | None
    contract: dict[str, Any]
    coverage: dict[str, Any]
    diagnostics: list[str]
    failure_code: str | None
    superseded: bool


class FlowStatusResponse(BaseModel):
    latest_attempt: FlowRunResponse | None
    active_run: FlowRunResponse | None
    freshness: str
    freshness_reasons: list[str]
    contract: dict[str, Any]
    inputs_ready: bool


class FlowHistoryResponse(BaseModel):
    items: list[FlowRunResponse]
    total: int
    limit: int
    offset: int


class FlowNodeResponse(BaseModel):
    key: str
    kind: str
    label: str
    locator: str | None
    attributes: dict[str, Any]
    evidence: list[str]


class FlowEdgeResponse(BaseModel):
    key: str
    relation: str
    family: str
    source_key: str
    target_key: str | None
    tier: str
    rule: str
    conditions: list[str]
    attributes: dict[str, Any]
    evidence: list[str]


class FlowNodeDetailResponse(Scoped):
    verification: str = "verified"
    node: FlowNodeResponse
    outgoing: list[FlowEdgeResponse]
    incoming: list[FlowEdgeResponse]
    outgoing_total: int
    incoming_total: int


class FlowEdgePageResponse(Scoped):
    verification: str = "verified"
    items: list[FlowEdgeResponse]
    total: int
    limit: int
    offset: int


def flow_run(x: FlowRun) -> FlowRunResponse:
    return FlowRunResponse(
        run_id=x.id,
        snapshot_id=x.project_version_id,
        revision=x.revision,
        started_at=x.started_at,
        completed_at=x.completed_at,
        outcome=x.outcome,
        flow_evidence_run_id=x.flow_evidence_run_id,
        catalog_run_id=x.catalog_run_id,
        architecture_run_id=x.architecture_run_id,
        structural_analysis_id=x.structural_analysis_id,
        input_authority_digest=x.input_authority_digest,
        graph_digest=x.graph_digest,
        portable_digest=x.portable_digest,
        contract=x.contract,
        coverage=x.coverage,
        diagnostics=list(x.diagnostics),
        failure_code=x.failure_code,
        superseded=x.superseded,
    )


def flow_node(n: FlowNode) -> FlowNodeResponse:
    return FlowNodeResponse(
        key=n.key,
        kind=n.kind,
        label=n.label,
        locator=n.locator,
        attributes=n.attributes,
        evidence=list(n.evidence),
    )


def flow_edge(e: FlowEdge) -> FlowEdgeResponse:
    return FlowEdgeResponse(
        key=e.key,
        relation=e.relation,
        family=e.family,
        source_key=e.source_key,
        target_key=e.target_key,
        tier=e.tier,
        rule=e.rule,
        conditions=list(e.conditions),
        attributes=e.attributes,
        evidence=list(e.evidence),
    )


def flow_status(service: FlowService, project_id: UUID, snapshot_id: UUID) -> FlowStatusResponse:
    value = service.status(project_id, snapshot_id)
    return FlowStatusResponse(
        latest_attempt=flow_run(value.latest) if value.latest else None,
        active_run=flow_run(value.active) if value.active else None,
        freshness=value.freshness,
        freshness_reasons=list(value.freshness_reasons),
        contract=value.contract,
        inputs_ready=value.inputs_ready,
    )


@router.post("", response_model=FlowStatusResponse)
def build_model(
    project_id: UUID, snapshot_id: UUID, service: FlowService = Depends(get_flow_service)
) -> FlowStatusResponse:
    service.build(project_id, snapshot_id)
    return flow_status(service, project_id, snapshot_id)


@router.get("", response_model=FlowStatusResponse)
def model_status(
    project_id: UUID, snapshot_id: UUID, service: FlowService = Depends(get_flow_service)
) -> FlowStatusResponse:
    return flow_status(service, project_id, snapshot_id)


@router.get("/runs", response_model=FlowHistoryResponse)
def model_history(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: FlowService = Depends(get_flow_service),
) -> FlowHistoryResponse:
    values, total = service.history(project_id, snapshot_id, limit, offset)
    return FlowHistoryResponse(
        items=[flow_run(x) for x in values], total=total, limit=limit, offset=offset
    )


@router.get("/runs/{run_id}", response_model=FlowRunResponse)
def model_run(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    service: FlowService = Depends(get_flow_service),
) -> FlowRunResponse:
    return flow_run(service.run(project_id, snapshot_id, run_id))


@router.get("/node", response_model=FlowNodeDetailResponse)
def model_node(
    project_id: UUID,
    snapshot_id: UUID,
    key: str = Query(..., min_length=1, max_length=2048),
    run_id: UUID | None = None,
    service: FlowService = Depends(get_flow_service),
) -> FlowNodeDetailResponse:
    value = service.node(project_id, snapshot_id, run_id, key)
    return FlowNodeDetailResponse(
        run_id=value["run"].id,
        freshness_reasons=list(value["freshness_reasons"]),
        node=flow_node(value["node"]),
        outgoing=[flow_edge(e) for e in value["outgoing"]],
        incoming=[flow_edge(e) for e in value["incoming"]],
        outgoing_total=value["outgoing_total"],
        incoming_total=value["incoming_total"],
    )


@router.get("/edges", response_model=FlowEdgePageResponse)
def model_edges(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    source: str | None = Query(None, max_length=2048),
    target: str | None = Query(None, max_length=2048),
    relation: str | None = Query(None, max_length=32),
    tier: str | None = Query(None, max_length=16),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: FlowService = Depends(get_flow_service),
) -> FlowEdgePageResponse:
    value = service.edges(
        project_id,
        snapshot_id,
        run_id,
        source=source,
        target=target,
        relation=relation,
        tier=tier,
        limit=limit,
        offset=offset,
    )
    return FlowEdgePageResponse(
        run_id=value["run"].id,
        freshness_reasons=list(value["freshness_reasons"]),
        items=[flow_edge(e) for e in value["edges"]],
        total=value["total"],
        limit=limit,
        offset=offset,
    )


@router.get("/diagnostics")
def model_diagnostics(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    service: FlowService = Depends(get_flow_service),
) -> dict[str, Any]:
    value = service.diagnostics(project_id, snapshot_id, run_id)
    return {
        "run_id": value["run"].id,
        "freshness_reasons": list(value["freshness_reasons"]),
        "items": [
            {"code": d.code, "subject": d.subject, "attributes": d.attributes}
            for d in value["diagnostics"]
        ],
    }


@router.get("/integrity")
def model_integrity(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    service: FlowService = Depends(get_flow_service),
) -> dict[str, Any]:
    value = service.integrity(project_id, snapshot_id, run_id)
    run = value.pop("run")
    return {"run_id": run.id, **value}


# ----------------------------------------------------------------------------- traces (Milestone C)


class TracePinRequest(BaseModel):
    model_config = {"extra": "forbid"}

    entry: str = Field(..., min_length=1, max_length=2048)
    direction: Literal["forward", "backward"] = "forward"
    max_depth: int = Field(MAX_DEPTH, ge=1, le=64)
    max_hops: int = Field(MAX_HOPS, ge=1, le=10_000)
    run_id: UUID | None = None


@router.get("/entries")
def trace_entries(
    project_id: UUID,
    snapshot_id: UUID,
    q: str | None = Query(None, min_length=1, max_length=500),
    kind: str | None = Query(None, max_length=32),
    mode: Literal["exact", "lexical", "semantic", "hybrid"] = "lexical",
    limit: int = Query(50, ge=1, le=200),
    run_id: UUID | None = None,
    service: FlowTraceService = Depends(get_flow_trace_service),
) -> dict[str, Any]:
    return service.entries(project_id, snapshot_id, run_id, kind, q, mode, limit)


@router.get("/trace")
def flow_trace(
    project_id: UUID,
    snapshot_id: UUID,
    entry: str = Query(..., min_length=1, max_length=2048),
    direction: Literal["forward", "backward"] = "forward",
    max_depth: int = Query(MAX_DEPTH, ge=1, le=64),
    max_hops: int = Query(MAX_HOPS, ge=1, le=10_000),
    run_id: UUID | None = None,
    service: FlowTraceService = Depends(get_flow_trace_service),
) -> dict[str, Any]:
    query = TraceQuery(entry, direction, max_depth, max_hops)
    return service.trace(project_id, snapshot_id, run_id, query)


@router.get("/path")
def flow_path(
    project_id: UUID,
    snapshot_id: UUID,
    source: str = Query(..., min_length=1, max_length=2048),
    target: str = Query(..., min_length=1, max_length=2048),
    run_id: UUID | None = None,
    service: FlowTraceService = Depends(get_flow_trace_service),
) -> dict[str, Any]:
    return service.path(project_id, snapshot_id, run_id, source, target)


@router.post("/traces")
def pin_trace(
    project_id: UUID,
    snapshot_id: UUID,
    body: TracePinRequest,
    service: FlowTraceService = Depends(get_flow_trace_service),
) -> dict[str, Any]:
    query = TraceQuery(body.entry, body.direction, body.max_depth, body.max_hops)
    return service.pin(project_id, snapshot_id, body.run_id, query)


@router.get("/traces/{trace_digest}")
def pinned_trace(
    project_id: UUID,
    snapshot_id: UUID,
    trace_digest: str = Path(..., pattern="^[0-9a-f]{64}$"),
    service: FlowTraceService = Depends(get_flow_trace_service),
) -> dict[str, Any]:
    return service.pinned(project_id, snapshot_id, trace_digest)


# ----------------------------------------------------------------------------- explanations (E)


class ExplanationPlanRequest(BaseModel):
    model_config = {"extra": "forbid"}

    trace_digest: str = Field(..., pattern="^[0-9a-f]{64}$")


class ExplanationRunRequest(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: UUID
    plan_digest: str = Field(..., pattern="^[0-9a-f]{64}$")
    confirm: Literal[True]


def explanation_run(run: ExplanationRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "plan_id": run.plan_id,
        "trace_digest": run.trace_digest,
        "package_digest": run.package_digest,
        "state": run.state,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "outcome": run.outcome,
        "returned_model": run.returned_model,
        "rejection_codes": list(run.rejection_codes),
        "unknowns": list(run.unknowns),
        "requests_sent": run.requests_sent,
        "claims": [
            {
                "ordinal": c.ordinal,
                "level": c.level,
                "text": c.text,
                "steps": list(c.steps),
                "citations": list(c.citations),
                "claim_digest": c.claim_digest,
            }
            for c in run.claims
        ],
        "label": "AI-generated explanation, validated against the pinned trace; never evidence",
    }


@router.get("/explanations/config")
def explanation_config(
    project_id: UUID,
    snapshot_id: UUID,
    service: FlowExplanationService = Depends(get_flow_explanation_service),
) -> dict[str, Any]:
    return service.configuration()


@router.post("/explanations/plan")
def plan_explanation(
    project_id: UUID,
    snapshot_id: UUID,
    body: ExplanationPlanRequest,
    service: FlowExplanationService = Depends(get_flow_explanation_service),
) -> dict[str, Any]:
    return service.plan(project_id, snapshot_id, body.trace_digest)


@router.post("/explanations")
def run_explanation(
    project_id: UUID,
    snapshot_id: UUID,
    body: ExplanationRunRequest,
    service: FlowExplanationService = Depends(get_flow_explanation_service),
) -> dict[str, Any]:
    return explanation_run(
        service.execute(project_id, snapshot_id, body.plan_id, body.plan_digest, body.confirm)
    )


@router.get("/explanations")
def list_explanations(
    project_id: UUID,
    snapshot_id: UUID,
    trace_digest: str = Query(..., pattern="^[0-9a-f]{64}$"),
    service: FlowExplanationService = Depends(get_flow_explanation_service),
) -> dict[str, Any]:
    runs = service.runs(project_id, snapshot_id, trace_digest)
    return {"items": [explanation_run(run) for run in runs], "total": len(runs)}


@router.get("/explanations/{run_id}")
def get_explanation(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    service: FlowExplanationService = Depends(get_flow_explanation_service),
) -> dict[str, Any]:
    return explanation_run(service.run(project_id, snapshot_id, run_id))
