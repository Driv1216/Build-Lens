"""Stage 14 architecture HTTP response schemas.

Graph elements carry an evidence ``tier``; responses from authoritative queries are only
produced after every asserted element and its evidence verified (``architecture-integrity.v1``).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class ArchitectureRunResponse(BaseModel):
    run_id: UUID
    snapshot_id: UUID
    revision: int
    started_at: datetime
    completed_at: datetime
    outcome: str
    catalog_run_id: UUID
    catalog_digest: str
    structural_analysis_id: UUID
    structural_revision: int
    input_authority_digest: str
    graph_digest: str | None
    architecture_digest: str | None
    portable_digest: str | None
    architecture_contract: dict[str, Any]
    coverage: dict[str, Any]
    diagnostics: list[str]
    superseded: bool
    quarantined: bool


class ArchitectureStatusResponse(BaseModel):
    latest_attempt: ArchitectureRunResponse | None
    active_run: ArchitectureRunResponse | None
    freshness: str
    freshness_reasons: list[str]
    contract: dict[str, Any]
    catalog_ready: bool


class ArchitectureHistoryResponse(BaseModel):
    items: list[ArchitectureRunResponse]
    total: int
    limit: int
    offset: int


class EvidenceLinkResponse(BaseModel):
    source: str
    digest: str


class NodeResponse(BaseModel):
    key: str
    kind: str
    parent_key: str | None
    name: str
    locator: str | None
    language: str | None
    catalog_entity_key: str | None
    coverage_state: str
    attributes: dict[str, Any]
    metrics: dict[str, Any]
    evidence: list[EvidenceLinkResponse]


class EdgeResponse(BaseModel):
    key: str
    kind: str
    source_key: str
    target_key: str | None
    target_text: str | None
    tier: str
    resolution_status: str | None
    import_class: str | None
    count: int
    attributes: dict[str, Any]
    conditions: list[str]
    evidence: list[EvidenceLinkResponse]


class ComponentResponse(BaseModel):
    key: str
    level: str
    class_set: str
    scope_key: str | None
    members: list[str]
    witness: list[str]
    attributes: dict[str, Any]
    evidence: list[EvidenceLinkResponse]


class DiagnosticResponse(BaseModel):
    ordinal: int
    code: str
    category: str
    tier: str
    subjects: list[str]
    attributes: dict[str, Any]
    evidence: list[EvidenceLinkResponse]


class Scoped(BaseModel):
    run_id: UUID
    freshness_reasons: list[str]
    verification: str = "verified"


class OverviewResponse(Scoped):
    workspaces: list[NodeResponse]
    node_counts: dict[str, int]
    edge_counts: dict[str, dict[str, int]]
    component_counts: dict[str, int]
    diagnostic_counts: dict[str, int]
    coverage: dict[str, Any]


class TreeResponse(Scoped):
    parent: NodeResponse | None
    children: list[NodeResponse]
    diagnostic_counts: dict[str, int]


class NodeDetailResponse(Scoped):
    node: NodeResponse
    chain: list[NodeResponse]
    children: list[NodeResponse]
    outgoing: list[EdgeResponse]
    incoming: list[EdgeResponse]
    outgoing_total: int
    incoming_total: int
    components: list[ComponentResponse]
    diagnostics: list[DiagnosticResponse]


class EdgePageResponse(Scoped):
    items: list[EdgeResponse]
    total: int
    limit: int
    offset: int


class GapsResponse(BaseModel):
    complete: bool
    unknown_exits: list[EdgeResponse]
    coverage_gaps: list[str]
    run_gaps: list[str]


class ReachedResponse(BaseModel):
    node: str
    depth: int
    path: list[str]


class DependenciesResponse(Scoped):
    origin: str
    origins: list[str]
    direction: str
    class_set: str
    max_depth: int
    search: str
    frontier: int
    reached: list[ReachedResponse]
    nodes: dict[str, NodeResponse]
    edges: dict[str, EdgeResponse]
    gaps: GapsResponse
    by_container: dict[str, int]


class PathResponse(Scoped):
    source: str
    target: str
    class_set: str
    status: str
    path: list[EdgeResponse]
    gaps: GapsResponse


class CyclesResponse(Scoped):
    items: list[ComponentResponse]
    edges: dict[str, EdgeResponse]
    total: int


class CycleDetailResponse(Scoped):
    component: ComponentResponse
    witness: list[EdgeResponse]
    internal: list[EdgeResponse]
    internal_total: int
    members: list[NodeResponse]


class MatrixCellResponse(BaseModel):
    source: str
    target: str
    count: int
    edges: int
    classes: dict[str, int]


class MatrixResponse(Scoped):
    parent: NodeResponse
    class_set: str
    children: list[NodeResponse]
    cells: list[MatrixCellResponse]
    cyclic_pairs: list[list[str]]


class NeighbourhoodResponse(Scoped):
    origin: str
    class_set: str
    radius: int
    nodes: list[NodeResponse]
    edges: list[EdgeResponse]
    truncated: bool
    total_nodes: int


class DiagnosticPageResponse(Scoped):
    items: list[DiagnosticResponse]
    total: int
    limit: int
    offset: int
    counts: dict[str, int]


class CoverageResponse(Scoped):
    coverage: dict[str, Any]


class EvidenceResponse(BaseModel):
    run_id: UUID
    digest: str
    kind: str
    source_refs: list[dict[str, Any]]
    derived: dict[str, Any]
    renderer_version: str
    rendered_text: str | None
    verification: str
    label: str = "non-authoritative inspection"


class IntegrityResponse(BaseModel):
    run_id: str
    structure: str
    structural_failures: list[str]
    evidence_checked: int
    evidence_failures: dict[str, str]
    evidence_failure_count: int
    label: str


class ApiViewResponse(Scoped):
    """Milestone B API architecture. Connections are conditional candidates only."""

    endpoints: list[NodeResponse]
    endpoints_total: int
    clients: list[NodeResponse]
    clients_total: int
    destinations: list[NodeResponse]
    routers: list[NodeResponse]
    connections: list[EdgeResponse]
    outcome_counts: dict[str, int]
    coverage: dict[str, Any]
    limit: int
    offset: int


class ViewResponse(Scoped):
    """A Milestone C view: inferred structure is labelled by tier, never intended design."""

    name: str
    nodes: list[NodeResponse]
    nodes_total: int
    edges: list[EdgeResponse]
    edges_total: int
    diagnostics: list[DiagnosticResponse]
    coverage: dict[str, Any]
    limit: int
