"""Stage 14 architecture HTTP contracts (deterministic; no AI text; fail closed on integrity)."""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from buildlens.api.architecture_schemas import (
    ArchitectureHistoryResponse,
    ArchitectureRunResponse,
    ArchitectureStatusResponse,
    ComponentResponse,
    CoverageResponse,
    CycleDetailResponse,
    CyclesResponse,
    DependenciesResponse,
    DiagnosticPageResponse,
    DiagnosticResponse,
    EdgePageResponse,
    EdgeResponse,
    EvidenceLinkResponse,
    EvidenceResponse,
    GapsResponse,
    IntegrityResponse,
    MatrixCellResponse,
    MatrixResponse,
    NeighbourhoodResponse,
    NodeDetailResponse,
    NodeResponse,
    OverviewResponse,
    PathResponse,
    ReachedResponse,
    TreeResponse,
)
from buildlens.api.dependencies import get_architecture_queries, get_architecture_service
from buildlens.application.architecture import ArchitectureService
from buildlens.application.architecture_queries import ArchitectureQueries, Gaps
from buildlens.domain.architecture import (
    ArchitectureComponent,
    ArchitectureDiagnostic,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureRun,
    EvidenceLink,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/snapshots/{snapshot_id}/architecture",
    tags=["architecture"],
)

ClassSetParam = Literal["S0", "S1", "S2", "S3", "S4"]


def run(x: ArchitectureRun) -> ArchitectureRunResponse:
    return ArchitectureRunResponse(
        run_id=x.id,
        snapshot_id=x.snapshot_id,
        revision=x.revision,
        started_at=x.started_at,
        completed_at=x.completed_at,
        outcome=x.outcome,
        catalog_run_id=x.catalog_run_id,
        catalog_digest=x.catalog_digest,
        structural_analysis_id=x.structural_analysis_id,
        structural_revision=x.structural_revision,
        input_authority_digest=x.input_authority_digest,
        graph_digest=x.graph_digest,
        architecture_digest=x.architecture_digest,
        portable_digest=x.portable_digest,
        architecture_contract=x.architecture_contract,
        coverage=x.coverage,
        diagnostics=list(x.diagnostics),
        superseded=x.superseded,
        quarantined=x.quarantined,
    )


def links(values: tuple[EvidenceLink, ...]) -> list[EvidenceLinkResponse]:
    return [EvidenceLinkResponse(source=x.source, digest=x.digest) for x in values]


def node(x: ArchitectureNode) -> NodeResponse:
    return NodeResponse(
        key=x.key,
        kind=x.kind,
        parent_key=x.parent_key,
        name=x.name,
        locator=x.locator,
        language=x.language,
        catalog_entity_key=x.catalog_entity_key,
        coverage_state=x.coverage_state,
        attributes=x.attributes,
        metrics=x.metrics,
        evidence=links(x.evidence),
    )


def edge(x: ArchitectureEdge) -> EdgeResponse:
    return EdgeResponse(
        key=x.key,
        kind=x.kind,
        source_key=x.source_key,
        target_key=x.target_key,
        target_text=x.target_text,
        tier=x.tier,
        resolution_status=x.resolution_status,
        import_class=x.import_class,
        count=x.count,
        attributes=x.attributes,
        conditions=list(x.conditions),
        evidence=links(x.evidence),
    )


def component(x: ArchitectureComponent) -> ComponentResponse:
    return ComponentResponse(
        key=x.key,
        level=x.level,
        class_set=x.class_set,
        scope_key=x.scope_key,
        members=list(x.members),
        witness=list(x.witness),
        attributes=x.attributes,
        evidence=links(x.evidence),
    )


def diagnostic(x: ArchitectureDiagnostic) -> DiagnosticResponse:
    return DiagnosticResponse(
        ordinal=x.ordinal,
        code=x.code,
        category=x.category,
        tier=x.tier,
        subjects=list(x.subjects),
        attributes=x.attributes,
        evidence=links(x.evidence),
    )


def gaps(x: Gaps) -> GapsResponse:
    return GapsResponse(
        complete=x.complete,
        unknown_exits=[edge(item) for item in x.unknown_exits],
        coverage_gaps=list(x.coverage_gaps),
        run_gaps=list(x.run_gaps),
    )


def status(
    service: ArchitectureService, project_id: UUID, snapshot_id: UUID
) -> ArchitectureStatusResponse:
    value = service.status(project_id, snapshot_id)
    return ArchitectureStatusResponse(
        latest_attempt=run(value.latest_attempt) if value.latest_attempt else None,
        active_run=run(value.active_run) if value.active_run else None,
        freshness=value.freshness,
        freshness_reasons=list(value.freshness_reasons),
        contract=value.contract,
        catalog_ready=value.catalog_ready,
    )


@router.post("", response_model=ArchitectureStatusResponse)
def build(
    project_id: UUID,
    snapshot_id: UUID,
    service: ArchitectureService = Depends(get_architecture_service),
) -> ArchitectureStatusResponse:
    service.build(project_id, snapshot_id)
    return status(service, project_id, snapshot_id)


@router.get("", response_model=ArchitectureStatusResponse)
def get_status(
    project_id: UUID,
    snapshot_id: UUID,
    service: ArchitectureService = Depends(get_architecture_service),
) -> ArchitectureStatusResponse:
    return status(service, project_id, snapshot_id)


@router.get("/runs", response_model=ArchitectureHistoryResponse)
def history(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: ArchitectureService = Depends(get_architecture_service),
) -> ArchitectureHistoryResponse:
    values, total = service.history(project_id, snapshot_id, limit, offset)
    return ArchitectureHistoryResponse(
        items=[run(x) for x in values], total=total, limit=limit, offset=offset
    )


@router.get("/runs/{run_id}", response_model=ArchitectureRunResponse)
def get_run(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    service: ArchitectureService = Depends(get_architecture_service),
) -> ArchitectureRunResponse:
    return run(service.run(project_id, snapshot_id, run_id))


@router.get("/overview", response_model=OverviewResponse)
def overview(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> OverviewResponse:
    x = queries.overview(project_id, snapshot_id, run_id)
    return OverviewResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        workspaces=[node(item) for item in x.workspaces],
        node_counts=x.node_counts,
        edge_counts=x.edge_counts,
        component_counts=x.component_counts,
        diagnostic_counts=x.diagnostic_counts,
        coverage=x.coverage,
    )


@router.get("/tree", response_model=TreeResponse)
def tree(
    project_id: UUID,
    snapshot_id: UUID,
    parent: str | None = Query(None, max_length=2048),
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> TreeResponse:
    x = queries.tree(project_id, snapshot_id, run_id, parent)
    return TreeResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        parent=node(x.parent) if x.parent else None,
        children=[node(item) for item in x.children],
        diagnostic_counts=x.diagnostic_counts,
    )


@router.get("/node", response_model=NodeDetailResponse)
def node_detail(
    project_id: UUID,
    snapshot_id: UUID,
    key: str = Query(..., max_length=2048),
    limit: int = Query(100, ge=1, le=500),
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> NodeDetailResponse:
    x = queries.node(project_id, snapshot_id, run_id, key, limit)
    return NodeDetailResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        node=node(x.node),
        chain=[node(item) for item in x.chain],
        children=[node(item) for item in x.children],
        outgoing=[edge(item) for item in x.outgoing],
        incoming=[edge(item) for item in x.incoming],
        outgoing_total=x.outgoing_total,
        incoming_total=x.incoming_total,
        components=[component(item) for item in x.components],
        diagnostics=[diagnostic(item) for item in x.diagnostics],
    )


@router.get("/edges", response_model=EdgePageResponse)
def edges(
    project_id: UUID,
    snapshot_id: UUID,
    kind: str | None = Query(None, max_length=32),
    source: str | None = Query(None, max_length=2048),
    target: str | None = Query(None, max_length=2048),
    tier: str | None = Query(None, max_length=16),
    resolution: str | None = Query(None, max_length=16),
    import_class: str | None = Query(None, max_length=32),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> EdgePageResponse:
    x = queries.edges(
        project_id,
        snapshot_id,
        run_id,
        kind=kind,
        source=source,
        target=target,
        tier=tier,
        resolution=resolution,
        import_class=import_class,
        limit=limit,
        offset=offset,
    )
    return EdgePageResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        items=[edge(item) for item in x.items],
        total=x.total,
        limit=x.limit,
        offset=x.offset,
    )


@router.get("/dependencies", response_model=DependenciesResponse)
def dependencies(
    project_id: UUID,
    snapshot_id: UUID,
    node_key: str = Query(..., alias="node", max_length=2048),
    direction: Literal["out", "in"] = "out",
    class_set: ClassSetParam | None = None,
    depth: int = Query(32, ge=1, le=32),
    limit: int = Query(500, ge=1, le=500),
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> DependenciesResponse:
    x = queries.dependencies(
        project_id,
        snapshot_id,
        run_id,
        node_key,
        direction=direction,
        class_set=class_set,
        depth=depth,
        limit=limit,
    )
    return DependenciesResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        origin=x.origin,
        origins=list(x.origins),
        direction=x.direction,
        class_set=x.class_set,
        max_depth=x.max_depth,
        search=x.search,
        frontier=x.frontier,
        reached=[
            ReachedResponse(node=item.node, depth=item.depth, path=list(item.path))
            for item in x.reached
        ],
        nodes={key: node(value) for key, value in x.nodes.items()},
        edges={key: edge(value) for key, value in x.edges.items()},
        gaps=gaps(x.gaps),
        by_container=x.by_container,
    )


@router.get("/path", response_model=PathResponse)
def path(
    project_id: UUID,
    snapshot_id: UUID,
    source: str = Query(..., max_length=2048),
    target: str = Query(..., max_length=2048),
    class_set: ClassSetParam | None = None,
    depth: int = Query(32, ge=1, le=32),
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> PathResponse:
    x = queries.path(
        project_id, snapshot_id, run_id, source, target, class_set=class_set, depth=depth
    )
    return PathResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        source=x.source,
        target=x.target,
        class_set=x.class_set,
        status=x.status,
        path=[edge(item) for item in x.path],
        gaps=gaps(x.gaps),
    )


@router.get("/cycles", response_model=CyclesResponse)
def cycles(
    project_id: UUID,
    snapshot_id: UUID,
    level: Literal["module", "container"] | None = None,
    class_set: ClassSetParam | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> CyclesResponse:
    x = queries.cycles(
        project_id,
        snapshot_id,
        run_id,
        level=level,
        class_set=class_set,
        limit=limit,
        offset=offset,
    )
    return CyclesResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        items=[component(item) for item in x.items],
        edges={key: edge(value) for key, value in x.edges.items()},
        total=x.total,
    )


@router.get("/cycle", response_model=CycleDetailResponse)
def cycle(
    project_id: UUID,
    snapshot_id: UUID,
    key: str = Query(..., max_length=256),
    limit: int = Query(200, ge=1, le=500),
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> CycleDetailResponse:
    x = queries.cycle(project_id, snapshot_id, run_id, key, limit)
    return CycleDetailResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        component=component(x.component),
        witness=[edge(item) for item in x.witness],
        internal=[edge(item) for item in x.internal],
        internal_total=x.internal_total,
        members=[node(item) for item in x.members],
    )


@router.get("/matrix", response_model=MatrixResponse)
def matrix(
    project_id: UUID,
    snapshot_id: UUID,
    parent: str = Query(..., max_length=2048),
    class_set: ClassSetParam | None = None,
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> MatrixResponse:
    x = queries.matrix(project_id, snapshot_id, run_id, parent, class_set=class_set)
    return MatrixResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        parent=node(x.parent),
        class_set=x.class_set,
        children=[node(item) for item in x.children],
        cells=[
            MatrixCellResponse(
                source=cell.source,
                target=cell.target,
                count=cell.count,
                edges=cell.edges,
                classes=cell.classes,
            )
            for cell in x.cells
        ],
        cyclic_pairs=[list(pair) for pair in x.cyclic_pairs],
    )


@router.get("/neighbourhood", response_model=NeighbourhoodResponse)
def neighbourhood(
    project_id: UUID,
    snapshot_id: UUID,
    node_key: str = Query(..., alias="node", max_length=2048),
    class_set: ClassSetParam | None = None,
    radius: int = Query(1, ge=1, le=2),
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> NeighbourhoodResponse:
    x = queries.neighbourhood(
        project_id, snapshot_id, run_id, node_key, class_set=class_set, radius=radius
    )
    return NeighbourhoodResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        origin=x.origin,
        class_set=x.class_set,
        radius=x.radius,
        nodes=[node(item) for item in x.nodes],
        edges=[edge(item) for item in x.edges],
        truncated=x.truncated,
        total_nodes=x.total_nodes,
    )


@router.get("/diagnostics", response_model=DiagnosticPageResponse)
def diagnostics(
    project_id: UUID,
    snapshot_id: UUID,
    code: str | None = Query(None, max_length=64),
    category: str | None = Query(None, max_length=32),
    subject: str | None = Query(None, max_length=2048),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> DiagnosticPageResponse:
    x = queries.diagnostics(
        project_id,
        snapshot_id,
        run_id,
        code=code,
        category=category,
        subject=subject,
        limit=limit,
        offset=offset,
    )
    return DiagnosticPageResponse(
        run_id=x.run.id,
        freshness_reasons=list(x.freshness_reasons),
        items=[diagnostic(item) for item in x.items],
        total=x.total,
        limit=x.limit,
        offset=x.offset,
        counts=x.counts,
    )


@router.get("/coverage", response_model=CoverageResponse)
def coverage(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    queries: ArchitectureQueries = Depends(get_architecture_queries),
) -> CoverageResponse:
    value, reasons, data = queries.coverage(project_id, snapshot_id, run_id)
    return CoverageResponse(run_id=value.id, freshness_reasons=list(reasons), coverage=data)


@router.get("/evidence/{digest}", response_model=EvidenceResponse)
def evidence(
    project_id: UUID,
    snapshot_id: UUID,
    digest: str,
    run_id: UUID | None = None,
    service: ArchitectureService = Depends(get_architecture_service),
) -> EvidenceResponse:
    value, item = service.evidence(project_id, snapshot_id, digest, run_id)
    verified = item.verification == "verified"
    return EvidenceResponse(
        run_id=value.id,
        digest=item.digest,
        kind=item.kind,
        source_refs=list(item.source_refs),
        derived=item.derived,
        renderer_version=item.renderer_version,
        rendered_text=item.rendered_text if verified else None,
        verification=item.verification or "EVIDENCE_NOT_CHECKED",
    )


@router.get("/integrity", response_model=IntegrityResponse)
def integrity(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    service: ArchitectureService = Depends(get_architecture_service),
) -> IntegrityResponse:
    report: dict[str, Any] = service.integrity(project_id, snapshot_id, run_id)
    return IntegrityResponse(**report)
