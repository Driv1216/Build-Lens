"""Stage 14 relationship queries over a verified architecture run.

Every query computes its answer from the structurally verified graph (I-C1/I-C2), names
every element it asserts or aggregates in an ``Assertion``, and only returns after
``ArchitectureService.verify_assertion`` re-read those rows and verified their evidence
(I-C3). Negative answers carry an explicit search status and the gaps that prevent a
complete claim; a bounded search never reports absence.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, cast
from uuid import UUID

from buildlens.application.architecture import (
    ArchitectureQueryError,
    ArchitectureService,
    Assertion,
    LoadedGraph,
)
from buildlens.application.architecture_builder import aggregate_shapes, ancestors, parents_of
from buildlens.domain.architecture import (
    CLASS_SET_NAMES,
    CLASS_SETS,
    DEFAULT_CLASS_SET,
    MODULE_EDGE_KINDS,
    NEIGHBOURHOOD_MAX_NODES,
    QUERY_MAX_DEPTH,
    QUERY_MAX_ROWS,
    ArchitectureComponent,
    ArchitectureDiagnostic,
    ArchitectureEdge,
    ArchitectureNode,
    ArchitectureRun,
)
from buildlens.domain.architecture_graph import breadth_first, shortest_path

QUERY_DEADLINE_SECONDS = 5.0
# Milestone C views: node kinds, edge kinds, diagnostic categories and coverage sections.
VIEWS: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str], tuple[str, ...]]] = {
    "integrations": (
        frozenset({"external_package", "external_destination"}),
        frozenset({"declares_dependency", "targets_destination"}),
        frozenset({"integration"}),
        ("integrations",),
    ),
    "database": (
        frozenset({"db_table"}),
        frozenset({"maps_table", "migrates_table", "references_table", "uses_model"}),
        frozenset({"database"}),
        ("database",),
    ),
    "frontend": (
        frozenset({"frontend_route"}),
        frozenset({"routes_to"}),
        frozenset(),
        ("frontend_routes",),
    ),
    "subsystems": (
        frozenset({"layer_candidate", "functional_slice"}),
        frozenset({"member_of"}),
        frozenset({"direction"}),
        ("layers", "slices"),
    ),
}
Direction = Literal["out", "in"]


@dataclass(frozen=True, slots=True)
class Scoped:
    run: ArchitectureRun
    freshness_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Overview(Scoped):
    workspaces: tuple[ArchitectureNode, ...]
    node_counts: dict[str, int]
    edge_counts: dict[str, dict[str, int]]
    component_counts: dict[str, int]
    diagnostic_counts: dict[str, int]
    coverage: dict[str, object]


@dataclass(frozen=True, slots=True)
class Tree(Scoped):
    parent: ArchitectureNode | None
    children: tuple[ArchitectureNode, ...]
    diagnostic_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class NodeDetail(Scoped):
    node: ArchitectureNode
    chain: tuple[ArchitectureNode, ...]
    children: tuple[ArchitectureNode, ...]
    outgoing: tuple[ArchitectureEdge, ...]
    incoming: tuple[ArchitectureEdge, ...]
    outgoing_total: int
    incoming_total: int
    components: tuple[ArchitectureComponent, ...]
    diagnostics: tuple[ArchitectureDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class EdgePage(Scoped):
    items: tuple[ArchitectureEdge, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class Reached:
    node: str
    depth: int
    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Gaps:
    """Why a negative answer is not a complete claim."""

    unknown_exits: tuple[ArchitectureEdge, ...]
    coverage_gaps: tuple[str, ...]
    run_gaps: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not (self.unknown_exits or self.coverage_gaps or self.run_gaps)


@dataclass(frozen=True, slots=True)
class Dependencies(Scoped):
    origin: str
    origins: tuple[str, ...]
    direction: Direction
    class_set: str
    max_depth: int
    search: str
    frontier: int
    reached: tuple[Reached, ...]
    edges: dict[str, ArchitectureEdge]
    nodes: dict[str, ArchitectureNode]
    gaps: Gaps
    by_container: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PathResult(Scoped):
    source: str
    target: str
    class_set: str
    status: str
    path: tuple[ArchitectureEdge, ...]
    gaps: Gaps


@dataclass(frozen=True, slots=True)
class Cycles(Scoped):
    items: tuple[ArchitectureComponent, ...]
    edges: dict[str, ArchitectureEdge]
    total: int


@dataclass(frozen=True, slots=True)
class CycleDetail(Scoped):
    component: ArchitectureComponent
    witness: tuple[ArchitectureEdge, ...]
    internal: tuple[ArchitectureEdge, ...]
    internal_total: int
    members: tuple[ArchitectureNode, ...]


@dataclass(frozen=True, slots=True)
class MatrixCell:
    source: str
    target: str
    count: int
    edges: int
    classes: dict[str, int]


@dataclass(frozen=True, slots=True)
class Matrix(Scoped):
    parent: ArchitectureNode
    class_set: str
    children: tuple[ArchitectureNode, ...]
    cells: tuple[MatrixCell, ...]
    cyclic_pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Neighbourhood(Scoped):
    origin: str
    class_set: str
    radius: int
    nodes: tuple[ArchitectureNode, ...]
    edges: tuple[ArchitectureEdge, ...]
    truncated: bool
    total_nodes: int


@dataclass(frozen=True, slots=True)
class ApiView(Scoped):
    """Milestone B API architecture: endpoints, request sites and conditional connections."""

    endpoints: tuple[ArchitectureNode, ...]
    endpoints_total: int
    clients: tuple[ArchitectureNode, ...]
    clients_total: int
    destinations: tuple[ArchitectureNode, ...]
    routers: tuple[ArchitectureNode, ...]
    connections: tuple[ArchitectureEdge, ...]
    outcome_counts: dict[str, int]
    coverage: dict[str, object]
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ViewResult(Scoped):
    name: str
    nodes: tuple[ArchitectureNode, ...]
    nodes_total: int
    edges: tuple[ArchitectureEdge, ...]
    edges_total: int
    diagnostics: tuple[ArchitectureDiagnostic, ...]
    coverage: dict[str, object]
    limit: int


@dataclass(frozen=True, slots=True)
class DiagnosticPage(Scoped):
    items: tuple[ArchitectureDiagnostic, ...]
    total: int
    limit: int
    offset: int
    counts: dict[str, int]


def _class_set(value: str | None) -> str:
    name = value or DEFAULT_CLASS_SET
    if name not in CLASS_SET_NAMES:
        raise ArchitectureQueryError(f"Unknown class set {name!r}.")
    return name


def _bounded(value: int, upper: int, name: str) -> int:
    if value < 1 or value > upper:
        raise ArchitectureQueryError(f"{name} must be between 1 and {upper}.")
    return value


class ArchitectureQueries:
    def __init__(self, service: ArchitectureService) -> None:
        self._service = service

    # ----------------------------------------------------------------- helpers

    def _open(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None
    ) -> tuple[ArchitectureRun, tuple[str, ...], LoadedGraph, float]:
        run, reasons, loaded = self._service.graph(project_id, snapshot_id, run_id)
        return run, reasons, loaded, time.monotonic() + QUERY_DEADLINE_SECONDS

    def _verify(
        self, run: ArchitectureRun, loaded: LoadedGraph, assertion: Assertion, deadline: float
    ) -> None:
        self._service.verify_assertion(run, loaded, assertion, deadline)

    @staticmethod
    def _node(loaded: LoadedGraph, key: str) -> ArchitectureNode:
        node = loaded.nodes.get(key)
        if node is None:
            raise ArchitectureQueryError(f"Unknown node {key!r}.")
        return node

    @staticmethod
    def _descendant_modules(loaded: LoadedGraph, key: str) -> list[str]:
        values: list[str] = []
        stack = [key]
        while stack:
            current = stack.pop()
            if loaded.nodes[current].kind == "module":
                values.append(current)
            stack.extend(loaded.children.get(current, []))
        return sorted(values)

    @staticmethod
    def _gaps(loaded: LoadedGraph, run: ArchitectureRun, visited: Iterable[str]) -> Gaps:
        exits: list[ArchitectureEdge] = []
        coverage: list[str] = []
        visited = set(visited)
        for edge in loaded.edges.values():
            if (
                edge.kind in MODULE_EDGE_KINDS
                and edge.tier == "unknown"
                and edge.source_key in visited
            ):
                exits.append(edge)
        for key in sorted(visited):
            node = loaded.nodes.get(key)
            if node is not None and node.kind == "module" and node.coverage_state != "analysed":
                coverage.append(key)
        run_gaps = []
        opaque = run.coverage.get("opaque_dynamic_imports")
        if opaque == "not_observable":
            run_gaps.append("OPAQUE_DYNAMIC_IMPORTS_NOT_OBSERVABLE")
        elif opaque == "present":
            run_gaps.append("OPAQUE_DYNAMIC_IMPORTS_PRESENT")
        if run.coverage.get("python_guard_context") == "unknown":
            run_gaps.append("PYTHON_GUARD_CONTEXT_UNKNOWN")
        exits.sort(key=lambda edge: edge.key)
        return Gaps(
            tuple(exits[:QUERY_MAX_ROWS]), tuple(coverage[:QUERY_MAX_ROWS]), tuple(run_gaps)
        )

    # ----------------------------------------------------------------- overview and tree

    def overview(self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None) -> Overview:
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        nodes = list(loaded.nodes.values())
        edges = list(loaded.edges.values())
        components = list(loaded.components.values())
        diagnostics = list(loaded.stored.diagnostics)
        self._verify(
            run,
            loaded,
            Assertion(
                frozenset(loaded.nodes),
                frozenset(loaded.edges),
                frozenset(loaded.components),
                frozenset(item.ordinal for item in diagnostics),
            ),
            deadline,
        )
        edge_counts: dict[str, Counter[str]] = defaultdict(Counter)
        for edge in edges:
            edge_counts[edge.kind][str(edge.import_class)] += 1
            if edge.resolution_status:
                edge_counts[f"{edge.kind}:resolution"][edge.resolution_status] += 1
        return Overview(
            run,
            reasons,
            tuple(node for node in nodes if node.kind == "workspace"),
            dict(sorted(Counter(node.kind for node in nodes).items())),
            {key: dict(sorted(value.items())) for key, value in sorted(edge_counts.items())},
            dict(sorted(Counter(f"{c.level}:{c.class_set}" for c in components).items())),
            dict(sorted(Counter(item.code for item in diagnostics).items())),
            dict(run.coverage),
        )

    def api(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        *,
        outcome: str | None = None,
        locator: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> ApiView:
        """Endpoints (all, up to the row limit) and a page of request sites with their
        branches; every returned node and connection is verified (I-C3)."""
        limit = _bounded(limit, QUERY_MAX_ROWS, "limit")
        if offset < 0:
            raise ArchitectureQueryError("offset must not be negative.")
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        nodes = sorted(loaded.nodes.values(), key=lambda node: node.key)
        endpoints = [node for node in nodes if node.kind == "api_endpoint"]
        clients = [
            node
            for node in nodes
            if node.kind == "client_call"
            and (locator is None or node.locator == locator)
            and (
                outcome is None
                or any(b.get("outcome") == outcome for b in node.attributes.get("branches", []))
            )
        ]
        page = clients[offset : offset + limit]
        shown = {node.key for node in page}
        connections = [
            edge
            for edge in sorted(loaded.edges.values(), key=lambda edge: edge.key)
            if edge.kind in {"calls_endpoint", "targets_destination"} and edge.source_key in shown
        ]
        destinations = [node for node in nodes if node.kind == "external_destination"]
        routers = [node for node in nodes if node.kind == "router"]
        listed = endpoints[:QUERY_MAX_ROWS]
        self._verify(
            run,
            loaded,
            Assertion(
                frozenset(n.key for n in [*listed, *page, *destinations, *routers]),
                frozenset(edge.key for edge in connections),
            ),
            deadline,
        )
        outcomes = Counter(
            str(branch.get("outcome"))
            for node in nodes
            if node.kind == "client_call"
            for branch in node.attributes.get("branches", [])
        )
        return ApiView(
            run,
            reasons,
            tuple(listed),
            len(endpoints),
            tuple(page),
            len(clients),
            tuple(destinations),
            tuple(routers),
            tuple(connections),
            dict(sorted(outcomes.items())),
            dict(cast(dict[str, object], run.coverage.get("api") or {})),
            limit,
            offset,
        )

    def view(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        name: str,
        *,
        limit: int = QUERY_MAX_ROWS,
    ) -> ViewResult:
        """One Milestone C view; every returned node, edge and diagnostic is verified."""
        if name not in VIEWS:
            raise ArchitectureQueryError(f"Unknown view {name!r}; expected one of {sorted(VIEWS)}.")
        limit = _bounded(limit, QUERY_MAX_ROWS, "limit")
        kinds, edge_kinds, categories, sections = VIEWS[name]
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        nodes = sorted(
            (node for node in loaded.nodes.values() if node.kind in kinds), key=lambda n: n.key
        )
        edges = sorted(
            (edge for edge in loaded.edges.values() if edge.kind in edge_kinds),
            key=lambda edge: edge.key,
        )
        diagnostics = [d for d in loaded.stored.diagnostics if d.category in categories][:limit]
        shown_nodes, shown_edges = nodes[:limit], edges[:limit]
        self._verify(
            run,
            loaded,
            Assertion(
                frozenset(n.key for n in shown_nodes),
                frozenset(e.key for e in shown_edges),
                diagnostics=frozenset(d.ordinal for d in diagnostics),
            ),
            deadline,
        )
        return ViewResult(
            run,
            reasons,
            name,
            tuple(shown_nodes),
            len(nodes),
            tuple(shown_edges),
            len(edges),
            tuple(diagnostics),
            {key: run.coverage.get(key) for key in sections},
            limit,
        )

    def tree(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None, parent: str | None
    ) -> Tree:
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        node = self._node(loaded, parent) if parent else None
        keys = loaded.children.get(parent, [])
        if parent is None:
            keys = [
                key
                for key in keys
                if loaded.nodes[key].kind not in {"external_package", "external_destination"}
            ]
        if len(keys) > QUERY_MAX_ROWS:
            keys = keys[:QUERY_MAX_ROWS]
        counts: Counter[str] = Counter()
        subjects: dict[str, str] = {}
        for key in keys:
            for module in self._descendant_modules(loaded, key) or [key]:
                subjects[module] = key
        relevant = []
        for diagnostic in loaded.stored.diagnostics:
            owner = next((subjects[s] for s in diagnostic.subjects if s in subjects), None)
            if owner is not None:
                counts[owner] += 1
                relevant.append(diagnostic.ordinal)
        self._verify(
            run,
            loaded,
            Assertion(
                frozenset([*keys, *([parent] if parent else [])]),
                diagnostics=frozenset(relevant),
            ),
            deadline,
        )
        return Tree(
            run,
            reasons,
            node,
            tuple(loaded.nodes[key] for key in keys),
            dict(sorted(counts.items())),
        )

    # ----------------------------------------------------------------- node and edges

    def node(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        key: str,
        limit: int = 100,
    ) -> NodeDetail:
        limit = _bounded(limit, QUERY_MAX_ROWS, "limit")
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        node = self._node(loaded, key)
        chain = [
            loaded.nodes[item] for item in ancestors(key, parents_of(loaded.nodes.values()))[1:]
        ]
        children = [loaded.nodes[item] for item in loaded.children.get(key, [])][:QUERY_MAX_ROWS]
        # Modules list their own imports; aggregates belong to container views.
        own = [e for e in loaded.edges.values() if node.kind != "module" or e.kind != "depends_on"]
        outgoing = sorted((e for e in own if e.source_key == key), key=lambda e: e.key)
        incoming = sorted((e for e in own if e.target_key == key), key=lambda e: e.key)
        components = [c for c in loaded.components.values() if key in c.members]
        diagnostics = [d for d in loaded.stored.diagnostics if key in d.subjects]
        shown_out, shown_in = outgoing[:limit], incoming[:limit]
        self._verify(
            run,
            loaded,
            Assertion(
                frozenset([key, *(n.key for n in chain), *(n.key for n in children)]),
                frozenset(e.key for e in [*shown_out, *shown_in]),
                frozenset(c.key for c in components),
                frozenset(d.ordinal for d in diagnostics),
            ),
            deadline,
        )
        return NodeDetail(
            run,
            reasons,
            node,
            tuple(chain),
            tuple(children),
            tuple(shown_out),
            tuple(shown_in),
            len(outgoing),
            len(incoming),
            tuple(components),
            tuple(diagnostics),
        )

    def edges(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        *,
        kind: str | None = None,
        source: str | None = None,
        target: str | None = None,
        tier: str | None = None,
        resolution: str | None = None,
        import_class: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> EdgePage:
        limit = _bounded(limit, QUERY_MAX_ROWS, "limit")
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        values = [
            edge
            for edge in sorted(loaded.edges.values(), key=lambda e: e.ordinal)
            if (kind is None or edge.kind == kind)
            and (source is None or edge.source_key == source)
            and (target is None or edge.target_key == target or edge.target_text == target)
            and (tier is None or edge.tier == tier)
            and (resolution is None or edge.resolution_status == resolution)
            and (import_class is None or edge.import_class == import_class)
        ]
        page = values[offset : offset + limit]
        self._verify(run, loaded, Assertion(edges=frozenset(e.key for e in page)), deadline)
        return EdgePage(run, reasons, tuple(page), len(values), limit, offset)

    # ----------------------------------------------------------------- traversal

    def dependencies(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        node: str,
        *,
        direction: Direction = "out",
        class_set: str | None = None,
        depth: int = QUERY_MAX_DEPTH,
        limit: int = QUERY_MAX_ROWS,
    ) -> Dependencies:
        name = _class_set(class_set)
        depth = _bounded(depth, QUERY_MAX_DEPTH, "depth")
        limit = _bounded(limit, QUERY_MAX_ROWS, "limit")
        if direction not in {"out", "in"}:
            raise ArchitectureQueryError("direction must be 'out' or 'in'.")
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        origin = self._node(loaded, node)
        origins = (
            [origin.key]
            if origin.kind == "module"
            else self._descendant_modules(loaded, origin.key)
        )
        if not origins:
            raise ArchitectureQueryError("The node contains no modules.")
        graph = loaded.module_graphs[name]
        result = breadth_first(graph, origins, direction, depth, limit, deadline)
        reached = [
            Reached(key, reach.depth, result.path(key))
            for key, reach in sorted(result.reached.items(), key=lambda i: (i[1].depth, i[0]))
            if key not in result.origins
        ]
        edge_keys = {key for item in reached for key in item.path}
        gaps = self._gaps(loaded, run, result.reached)
        by_container = Counter(loaded.nodes[item.node].parent_key or "" for item in reached)
        self._verify(
            run,
            loaded,
            Assertion(
                frozenset([origin.key, *(item.node for item in reached)]),
                frozenset([*edge_keys, *(edge.key for edge in gaps.unknown_exits)]),
            ),
            deadline,
        )
        return Dependencies(
            run,
            reasons,
            origin.key,
            tuple(origins),
            direction,
            name,
            depth,
            result.status,
            result.frontier,
            tuple(reached),
            {key: loaded.edges[key] for key in sorted(edge_keys)},
            {item.node: loaded.nodes[item.node] for item in reached},
            gaps,
            dict(sorted(by_container.items())),
        )

    def path(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        source: str,
        target: str,
        *,
        class_set: str | None = None,
        depth: int = QUERY_MAX_DEPTH,
    ) -> PathResult:
        name = _class_set(class_set)
        depth = _bounded(depth, QUERY_MAX_DEPTH, "depth")
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        for key in (source, target):
            if self._node(loaded, key).kind != "module":
                raise ArchitectureQueryError("Paths are computed between modules.")
        graph = loaded.module_graphs[name]
        keys, search = shortest_path(graph, source, target, depth, deadline)
        if keys is not None:
            edges = tuple(loaded.edges[key] for key in keys)
            self._verify(
                run,
                loaded,
                Assertion(
                    frozenset([source, target, *(e.target_key or "" for e in edges)]) - {""},
                    frozenset(keys),
                ),
                deadline,
            )
            return PathResult(run, reasons, source, target, name, "found", edges, Gaps((), (), ()))
        visited = breadth_first(graph, source, "out", depth, 10**9, deadline)
        gaps = self._gaps(loaded, run, visited.reached)
        if search != "complete" or visited.status != "complete":
            status = "not_found_within_bounds"
        elif gaps.complete:
            status = "not_found"
        else:
            status = "not_found_in_observed_graph"
        self._verify(
            run,
            loaded,
            Assertion(
                frozenset([source, target]), frozenset(edge.key for edge in gaps.unknown_exits)
            ),
            deadline,
        )
        return PathResult(run, reasons, source, target, name, status, (), gaps)

    # ----------------------------------------------------------------- cycles

    def cycles(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        *,
        level: str | None = None,
        class_set: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Cycles:
        limit = _bounded(limit, QUERY_MAX_ROWS, "limit")
        if class_set is not None:
            _class_set(class_set)
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        items = [
            component
            for component in sorted(loaded.components.values(), key=lambda c: c.ordinal)
            if (level is None or component.level == level)
            and (class_set is None or component.class_set == class_set)
        ]
        page = items[offset : offset + limit]
        witness = {key for component in page for key in component.witness}
        self._verify(
            run,
            loaded,
            Assertion(
                frozenset(member for component in page for member in component.members),
                frozenset(witness),
                frozenset(component.key for component in page),
            ),
            deadline,
        )
        return Cycles(
            run,
            reasons,
            tuple(page),
            {key: loaded.edges[key] for key in sorted(witness)},
            len(items),
        )

    def cycle(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        key: str,
        limit: int = 200,
    ) -> CycleDetail:
        limit = _bounded(limit, QUERY_MAX_ROWS, "limit")
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        component = loaded.components.get(key)
        if component is None:
            raise ArchitectureQueryError(f"Unknown component {key!r}.")
        members = set(component.members)
        allowed = CLASS_SETS[component.class_set]
        kinds = MODULE_EDGE_KINDS if component.level == "module" else frozenset({"depends_on"})
        internal = sorted(
            (
                edge
                for edge in loaded.edges.values()
                if edge.kind in kinds
                and edge.source_key in members
                and edge.target_key in members
                and edge.import_class in allowed
                and (
                    component.level == "module"
                    or edge.attributes.get("scope") == component.scope_key
                )
            ),
            key=lambda edge: edge.key,
        )
        shown = internal[:limit]
        self._verify(
            run,
            loaded,
            Assertion(
                frozenset(members),
                frozenset([*component.witness, *(edge.key for edge in shown)]),
                frozenset([key]),
            ),
            deadline,
        )
        return CycleDetail(
            run,
            reasons,
            component,
            tuple(loaded.edges[item] for item in component.witness),
            tuple(shown),
            len(internal),
            tuple(loaded.nodes[member] for member in sorted(members)),
        )

    # ----------------------------------------------------------------- matrix and neighbourhood

    def matrix(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        parent: str,
        *,
        class_set: str | None = None,
    ) -> Matrix:
        name = _class_set(class_set)
        allowed = CLASS_SETS[name]
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        node = self._node(loaded, parent)
        if node.kind not in {"workspace", "container"}:
            raise ArchitectureQueryError("A matrix needs a workspace or container.")
        children = loaded.children.get(parent, [])
        if len(children) > QUERY_MAX_ROWS:
            raise ArchitectureQueryError("Too many children for one matrix; drill down.")
        child_set = set(children)
        module_edges = [e for e in loaded.edges.values() if e.kind in MODULE_EDGE_KINDS]
        shapes = {
            shape.key: shape
            for shape in aggregate_shapes(module_edges, parents_of(loaded.nodes.values()))
        }
        cells: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        counts: Counter[tuple[str, str]] = Counter()
        edge_counts: Counter[tuple[str, str]] = Counter()
        asserted: set[str] = set()
        for edge in loaded.edges.values():
            if (
                edge.kind != "depends_on"
                or edge.attributes.get("scope") != parent
                or edge.import_class not in allowed
                or edge.source_key not in child_set
                or edge.target_key not in child_set
            ):
                continue
            pair = (edge.source_key, str(edge.target_key))
            cells[pair][str(edge.import_class)] += edge.count
            counts[pair] += edge.count
            shape = shapes.get(edge.key)
            constituents = shape.keys if shape else ()
            edge_counts[pair] += len(constituents)
            asserted.add(edge.key)
            asserted.update(constituents)
        self._verify(
            run,
            loaded,
            Assertion(frozenset([parent, *children]), frozenset(asserted)),
            deadline,
        )
        cyclic = sorted(
            pair for pair in counts if (pair[1], pair[0]) in counts and pair[0] < pair[1]
        )
        return Matrix(
            run,
            reasons,
            node,
            name,
            tuple(loaded.nodes[key] for key in children),
            tuple(
                MatrixCell(source, target, counts[(source, target)], edge_counts[(source, target)],
                           dict(sorted(cells[(source, target)].items())))
                for source, target in sorted(cells)
            ),
            tuple(cyclic),
        )  # fmt: skip

    def neighbourhood(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        node: str,
        *,
        class_set: str | None = None,
        radius: int = 1,
    ) -> Neighbourhood:
        name = _class_set(class_set)
        radius = _bounded(radius, 2, "radius")
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        if self._node(loaded, node).kind != "module":
            raise ArchitectureQueryError("Neighbourhoods are computed around modules.")
        graph = loaded.module_graphs[name]
        out = breadth_first(graph, node, "out", radius, 10**6, deadline)
        inc = breadth_first(graph, node, "in", radius, 10**6, deadline)
        ranked = sorted(
            set(out.reached) | set(inc.reached),
            key=lambda key: (
                min(
                    out.reached[key].depth if key in out.reached else 99,
                    inc.reached[key].depth if key in inc.reached else 99,
                ),
                key,
            ),
        )
        total = len(ranked)
        chosen = set(ranked[:NEIGHBOURHOOD_MAX_NODES])
        edges = sorted(
            (
                edge
                for edge in loaded.edges.values()
                if edge.kind in MODULE_EDGE_KINDS
                and edge.source_key in chosen
                and edge.target_key in chosen
                and edge.import_class in CLASS_SETS[name]
            ),
            key=lambda edge: edge.key,
        )
        self._verify(
            run, loaded, Assertion(frozenset(chosen), frozenset(e.key for e in edges)), deadline
        )
        return Neighbourhood(
            run,
            reasons,
            node,
            name,
            radius,
            tuple(loaded.nodes[key] for key in sorted(chosen)),
            tuple(edges),
            total > len(chosen),
            total,
        )

    # ----------------------------------------------------------------- diagnostics and coverage

    def diagnostics(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        *,
        code: str | None = None,
        category: str | None = None,
        subject: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> DiagnosticPage:
        limit = _bounded(limit, QUERY_MAX_ROWS, "limit")
        run, reasons, loaded, deadline = self._open(project_id, snapshot_id, run_id)
        values = [
            item
            for item in loaded.stored.diagnostics
            if (code is None or item.code == code)
            and (category is None or item.category == category)
            and (subject is None or subject in item.subjects)
        ]
        page = values[offset : offset + limit]
        self._verify(
            run,
            loaded,
            Assertion(
                frozenset(key for item in page for key in item.subjects),
                diagnostics=frozenset(item.ordinal for item in page),
            ),
            deadline,
        )
        return DiagnosticPage(
            run,
            reasons,
            tuple(page),
            len(values),
            limit,
            offset,
            dict(sorted(Counter(item.code for item in loaded.stored.diagnostics).items())),
        )

    def coverage(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None
    ) -> tuple[ArchitectureRun, tuple[str, ...], dict[str, object]]:
        """Run coverage is bound into ``graph_digest`` and verified with the structure."""
        run, reasons, _, _ = self._open(project_id, snapshot_id, run_id)
        return run, reasons, dict(run.coverage)
