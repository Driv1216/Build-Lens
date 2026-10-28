"""Stage 14 architecture model: contracts, immutable records and integrity digests.

An architecture run is a deterministic projection of one Stage 13 catalog run plus the
structural rows that catalog cites. It never reads repository files and contains no AI text.
Every element carries evidence links (catalog ``F#`` facts or architecture evidence rows) and
an element digest over all of its semantic fields (``architecture-integrity.v1``), so read
paths can detect any change to an edge, its evidence links or its derived records.

Three epistemic axes are kept apart: the evidence tier of graph elements (observed, derived,
inferred, unknown), owner-declared intent, and conformance outcomes. This module holds only
the first axis.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, get_args
from uuid import UUID

from buildlens.domain.extractors import LITERAL_EXTRACTORS, extractor_contract_digest
from buildlens.domain.source import digest

ARCHITECTURE_VERSION = "architecture-model.v2"
IMPORT_CLASSES_VERSION = "import-classes.v2"
INTEGRITY_VERSION = "architecture-integrity.v1"
SCC_VERSION = "scc.tarjan-iterative.v1"
CONTAINMENT_VERSION = "containment.v1"
AGGREGATION_VERSION = "aggregation.v1"
DIRECTION_VERSION = "direction-diagnostics.v1"
EVIDENCE_RENDERER = "architecture-evidence.v1"
# Milestone B API projection contracts (application/architecture_api.py).
ROUTER_BINDING_VERSION = "router-binding.v1"
HTTP_SINKS_VERSION = "http-sinks.v1"
CLIENT_WRAPPER_VERSION = "client-wrapper.v1"
CLIENT_FOLDING_VERSION = "client-folding.v1"
# v3 (Milestone D): typed converters accept only their characters (v2 accepted any segment).
API_MATCHING_VERSION = "api-matching.v3"
# Milestone C view contracts (application/architecture_views.py).
VIEW_CONTRACTS = {
    "manifests": "manifest-facts.v1",
    "packages": "package-matching.v1",
    "database": "database-view.v1",
    "frontend_routes": "frontend-routes.v1",
    "layers": "layer-candidates.v1",
    "slices": "functional-slices.v1",
    "absence_gating": "absence-gating.v1",
    "stdlib_lists": "stdlib-lists.v1",
}

# Placeholder limits recorded in the contract (fail closed: a truncated graph would make
# cycle and reachability claims false).
MAX_NODES = 50_000
MAX_EDGES = 250_000
STATEMENT_REFS_PER_EDGE = 64
QUERY_MAX_DEPTH = 32
QUERY_MAX_ROWS = 500
NEIGHBOURHOOD_MAX_NODES = 80
VERIFICATION_BUDGET = 25_000

NodeKind = Literal[
    "workspace",
    "container",
    "module",
    "external_package",
    "router",
    "api_endpoint",
    "client_call",
    "external_destination",
    "db_table",
    "frontend_route",
    "layer_candidate",
    "functional_slice",
]
EdgeKind = Literal[
    "imports",
    "reexports",
    "depends_on",
    "defines_router",
    "mounts_router",
    "defines_endpoint",
    "issues_request",
    "calls_endpoint",
    "targets_destination",
    "maps_table",
    "migrates_table",
    "references_table",
    "uses_model",
    "declares_dependency",
    "routes_to",
    "member_of",
]
Tier = Literal["observed", "derived", "inferred", "unknown"]
ImportClass = Literal[
    "unconditional",
    "presumed_unconditional",
    "conditional",
    "elision_dependent",
    "type_level",
    "deferred",
]
ClassSet = Literal["S0", "S1", "S2", "S3", "S4"]
ComponentLevel = Literal["module", "container"]
CoverageState = Literal[
    "analysed",
    "partial",
    "not_current",
    "structure_failed",
    "not_analysed",
    "not_code",
    "derived",
]
EvidenceSource = Literal["catalog", "architecture"]
EvidenceKind = Literal["import_statements", "inventory_artifact", "derivation", "literal_rows"]
ArchitectureOutcome = Literal["complete", "partial", "failed"]
Resolution = Literal["resolved", "ambiguous", "not_found", "external", "not_attempted"]

NODE_KINDS: tuple[str, ...] = get_args(NodeKind)
EDGE_KINDS: tuple[str, ...] = get_args(EdgeKind)
TIERS: tuple[str, ...] = get_args(Tier)
IMPORT_CLASSES: tuple[str, ...] = get_args(ImportClass)
CLASS_SET_NAMES: tuple[str, ...] = get_args(ClassSet)
COVERAGE_STATES: tuple[str, ...] = get_args(CoverageState)
EVIDENCE_KINDS: tuple[str, ...] = get_args(EvidenceKind)
RESOLUTIONS: tuple[str, ...] = get_args(Resolution)

# Class sets describe the static import relation only, never runtime behaviour (C-2).
CLASS_SETS: dict[str, frozenset[str]] = {
    "S0": frozenset({"unconditional"}),
    "S1": frozenset({"unconditional", "presumed_unconditional"}),
    "S2": frozenset(
        {"unconditional", "presumed_unconditional", "conditional", "elision_dependent"}
    ),
    "S3": frozenset(
        {
            "unconditional",
            "presumed_unconditional",
            "conditional",
            "elision_dependent",
            "type_level",
        }
    ),
    "S4": frozenset(IMPORT_CLASSES),
}
DEFAULT_CLASS_SET = "S2"
MODULE_EDGE_KINDS = frozenset({"imports", "reexports"})

Row = dict[str, Any]


def architecture_contract() -> dict[str, object]:
    """Everything an architecture run depends on besides its persisted inputs."""
    return {
        "architecture": ARCHITECTURE_VERSION,
        "import_classes": IMPORT_CLASSES_VERSION,
        "integrity": INTEGRITY_VERSION,
        "scc": SCC_VERSION,
        "containment": CONTAINMENT_VERSION,
        "aggregation": AGGREGATION_VERSION,
        "direction_diagnostics": DIRECTION_VERSION,
        "evidence_renderer": EVIDENCE_RENDERER,
        "literals": {name: extractor_contract_digest(name) for name in sorted(LITERAL_EXTRACTORS)},
        "api": {
            "router_binding": ROUTER_BINDING_VERSION,
            "http_sinks": HTTP_SINKS_VERSION,
            "client_wrapper": CLIENT_WRAPPER_VERSION,
            "client_folding": CLIENT_FOLDING_VERSION,
            "api_matching": API_MATCHING_VERSION,
        },
        "views": dict(VIEW_CONTRACTS),
        "limits": {
            "max_nodes": MAX_NODES,
            "max_edges": MAX_EDGES,
            "statement_refs_per_edge": STATEMENT_REFS_PER_EDGE,
            "query_max_depth": QUERY_MAX_DEPTH,
            "query_max_rows": QUERY_MAX_ROWS,
            "neighbourhood_max_nodes": NEIGHBOURHOOD_MAX_NODES,
            "verification_budget": VERIFICATION_BUDGET,
        },
    }


# --------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class EvidenceLink:
    source: EvidenceSource
    digest: str


@dataclass(frozen=True, slots=True)
class ArchitectureEvidence:
    """Evidence the catalog lacks: cited structural rows or a derivation record.

    ``source_refs`` entries carry ``row_digest``, a digest of the cited row's semantic fields
    without database ids, so verification detects any row change and the portable projection
    stays database-independent.
    """

    digest: str
    kind: EvidenceKind
    source_refs: tuple[Row, ...]
    derived: Row
    renderer_version: str
    rendered_text: str
    verification: str | None = None


@dataclass(frozen=True, slots=True)
class ArchitectureNode:
    key: str
    kind: NodeKind
    parent_key: str | None
    name: str
    locator: str | None
    language: str | None
    catalog_entity_key: str | None
    artifact_identity_id: UUID | None
    coverage_state: CoverageState
    attributes: Row = field(default_factory=dict)
    metrics: Row = field(default_factory=dict)
    evidence: tuple[EvidenceLink, ...] = ()
    ordinal: int = 0


@dataclass(frozen=True, slots=True)
class ArchitectureEdge:
    ordinal: int
    key: str
    kind: EdgeKind
    source_key: str
    target_key: str | None
    target_text: str | None
    tier: Tier
    resolution_status: Resolution | None
    import_class: ImportClass | None
    count: int
    attributes: Row = field(default_factory=dict)
    conditions: tuple[str, ...] = ()
    evidence: tuple[EvidenceLink, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchitectureComponent:
    """A strongly connected component of the static import relation (or of an aggregate)."""

    key: str
    ordinal: int
    level: ComponentLevel
    class_set: ClassSet
    scope_key: str | None
    members: tuple[str, ...]
    witness: tuple[str, ...]
    attributes: Row = field(default_factory=dict)
    evidence: tuple[EvidenceLink, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchitectureDiagnostic:
    ordinal: int
    code: str
    category: str
    tier: Tier
    subjects: tuple[str, ...]
    attributes: Row = field(default_factory=dict)
    evidence: tuple[EvidenceLink, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchitectureRun:
    id: UUID
    project_id: UUID
    snapshot_id: UUID
    revision: int
    started_at: datetime
    completed_at: datetime
    outcome: ArchitectureOutcome
    catalog_run_id: UUID
    catalog_digest: str
    structural_analysis_id: UUID
    structural_revision: int
    architecture_contract: dict[str, object]
    input_authority_digest: str
    graph_digest: str | None
    architecture_digest: str | None
    portable_digest: str | None
    coverage: dict[str, object]
    diagnostics: tuple[str, ...] = ()
    superseded: bool = False
    quarantined: bool = False
    captured_active_architecture_run_id: UUID | None = None
    captured_latest_revision: int = 0


@dataclass(frozen=True, slots=True)
class ArchitectureBuild:
    nodes: tuple[ArchitectureNode, ...]
    edges: tuple[ArchitectureEdge, ...]
    components: tuple[ArchitectureComponent, ...]
    diagnostics: tuple[ArchitectureDiagnostic, ...]
    evidence: tuple[ArchitectureEvidence, ...]
    graph_digest: str
    architecture_digest: str
    portable_digest: str
    coverage: dict[str, object]
    outcome: ArchitectureOutcome


# --------------------------------------------------------------------------- digests


def evidence_digest(
    renderer_version: str, rendered_text: str, refs: Sequence[Row], derived: Row
) -> str:
    return digest(
        {
            "renderer": renderer_version,
            "text": rendered_text,
            "refs": list(refs),
            "derived": derived,
        }
    )


def evidence_set_digest(links: Iterable[EvidenceLink]) -> str:
    return digest(sorted([link.source, link.digest] for link in links))


def node_digest(node: ArchitectureNode) -> str:
    return digest(
        {
            "kind": node.kind,
            "key": node.key,
            "parent": node.parent_key,
            "name": node.name,
            "locator": node.locator,
            "language": node.language,
            "catalog_entity": node.catalog_entity_key,
            "artifact_identity": str(node.artifact_identity_id)
            if node.artifact_identity_id
            else None,
            "coverage": node.coverage_state,
            "attributes": node.attributes,
            "metrics": node.metrics,
            "evidence": evidence_set_digest(node.evidence),
        }
    )


def edge_digest(edge: ArchitectureEdge) -> str:
    return digest(
        {
            "kind": edge.kind,
            "key": edge.key,
            "source": edge.source_key,
            "target": edge.target_key,
            "text": edge.target_text,
            "tier": edge.tier,
            "resolution": edge.resolution_status,
            "class": edge.import_class,
            "count": edge.count,
            "attributes": edge.attributes,
            "conditions": list(edge.conditions),
            "evidence": evidence_set_digest(edge.evidence),
        }
    )


def component_digest(component: ArchitectureComponent) -> str:
    return digest(
        {
            "key": component.key,
            "level": component.level,
            "class_set": component.class_set,
            "scope": component.scope_key,
            "members": sorted(component.members),
            "witness": list(component.witness),
            "attributes": component.attributes,
            "evidence": evidence_set_digest(component.evidence),
        }
    )


def diagnostic_digest(diagnostic: ArchitectureDiagnostic) -> str:
    return digest(
        {
            "code": diagnostic.code,
            "category": diagnostic.category,
            "tier": diagnostic.tier,
            "subjects": sorted(diagnostic.subjects),
            "attributes": diagnostic.attributes,
            "evidence": evidence_set_digest(diagnostic.evidence),
        }
    )


def graph_digest_of(
    node_digests: Sequence[str],
    edge_digests: Sequence[str],
    component_digests: Sequence[str],
    diagnostic_digests: Sequence[str],
    coverage: Row,
) -> str:
    """Run-level integrity digest over every element digest (persisted order) and coverage."""
    return digest(
        {
            "integrity": INTEGRITY_VERSION,
            "nodes": list(node_digests),
            "edges": list(edge_digests),
            "components": list(component_digests),
            "diagnostics": list(diagnostic_digests),
            "coverage": coverage,
        }
    )


def class_set_of(classes: Iterable[str]) -> str:
    """The smallest class set containing every given import class."""
    values = set(classes)
    for name in CLASS_SET_NAMES:
        if values <= CLASS_SETS[name]:
            return name
    raise ValueError(f"Unknown import classes {sorted(values)!r}")
