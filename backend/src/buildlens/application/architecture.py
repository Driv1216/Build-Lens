"""Stage 14 architecture service: build, freshness, integrity and verified graph access.

Integrity contract ``architecture-integrity.v1``:

* I-C1 a cached graph is created only after full structural verification: every element
  digest recomputed from its row content, the run's ``graph_digest`` reproduced, and the
  persisted aggregates and components re-derived from the persisted module edges;
* I-C2 every request re-checks freshness and the per-table fingerprint;
* I-C3 every element a response asserts or aggregates is re-read, re-digested and has its
  evidence verified, within a per-request budget;
* I-C4 negative claims are answered from the verified structure of the published run;
* I-C5 verification results are never cached and any failure evicts the run.

Authoritative queries fail closed; they never return partial or unverified results.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from buildlens.application.architecture_builder import (
    ArchitectureBuildError,
    ArchitectureInput,
    aggregate_shapes,
    build_architecture,
    component_shapes,
    parents_of,
)
from buildlens.application.semantic_catalog import (
    SemanticCatalogRequiredError,
    SemanticCatalogService,
)
from buildlens.domain.architecture import (
    CLASS_SET_NAMES,
    CLASS_SETS,
    MODULE_EDGE_KINDS,
    VERIFICATION_BUDGET,
    ArchitectureBuild,
    ArchitectureComponent,
    ArchitectureDiagnostic,
    ArchitectureEdge,
    ArchitectureEvidence,
    ArchitectureNode,
    ArchitectureRun,
    EvidenceLink,
    architecture_contract,
    component_digest,
    diagnostic_digest,
    edge_digest,
    graph_digest_of,
    node_digest,
)
from buildlens.domain.architecture_graph import Graph, GraphEdge
from buildlens.domain.semantic import SemanticCatalogRun
from buildlens.domain.source import digest

CACHE_RUNS = 4


class ArchitectureBusyError(Exception):
    pass


class ArchitectureConflictError(Exception):
    pass


class ArchitectureRequiredError(Exception):
    pass


class ArchitectureStaleError(Exception):
    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = tuple(reasons)


class ArchitectureNotFoundError(Exception):
    pass


class ArchitectureQueryError(ValueError):
    pass


class ArchitectureIntegrityError(Exception):
    """Persisted architecture rows disagree with their digests or derivations."""

    def __init__(self, failures: Sequence[str]) -> None:
        super().__init__("; ".join(failures[:5]))
        self.failures = tuple(failures)


class ArchitectureEvidenceInvalidError(Exception):
    def __init__(self, failures: dict[str, str]) -> None:
        super().__init__(f"{len(failures)} evidence item(s) failed verification")
        self.failures = failures


class ArchitectureBudgetError(Exception):
    pass


# --------------------------------------------------------------------------- ports


@dataclass(frozen=True, slots=True)
class StoredGraph:
    """Elements as reconstructed from rows, with the digests stored beside them."""

    nodes: tuple[ArchitectureNode, ...] = ()
    node_digests: tuple[str, ...] = ()
    edges: tuple[ArchitectureEdge, ...] = ()
    edge_digests: tuple[str, ...] = ()
    components: tuple[ArchitectureComponent, ...] = ()
    component_digests: tuple[str, ...] = ()
    diagnostics: tuple[ArchitectureDiagnostic, ...] = ()
    diagnostic_digests: tuple[str, ...] = ()


class ArchitectureStore(Protocol):
    def load_input(self, catalog: SemanticCatalogRun) -> ArchitectureInput: ...
    def latest(self, project_id: UUID, snapshot_id: UUID) -> ArchitectureRun | None: ...
    def active(self, project_id: UUID, snapshot_id: UUID) -> ArchitectureRun | None: ...
    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> ArchitectureRun | None: ...
    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[ArchitectureRun], int]: ...
    def publish(self, run: ArchitectureRun, build: ArchitectureBuild) -> ArchitectureRun: ...
    def save_failure(self, run: ArchitectureRun, code: str) -> ArchitectureRun: ...
    def load_graph(self, run_id: UUID) -> tuple[StoredGraph, str]: ...
    def fingerprint(self, run_id: UUID) -> str: ...
    def reload(
        self,
        run_id: UUID,
        *,
        nodes: Iterable[str] = (),
        edges: Iterable[int] = (),
        components: Iterable[str] = (),
        diagnostics: Iterable[int] = (),
    ) -> StoredGraph: ...
    def verify_evidence(
        self, run: ArchitectureRun, links: Sequence[EvidenceLink]
    ) -> dict[EvidenceLink, str]: ...
    def evidence(self, run_id: UUID, digests: Sequence[str]) -> list[ArchitectureEvidence]: ...


# --------------------------------------------------------------------------- verified graph


@dataclass(slots=True)
class LoadedGraph:
    """A structurally verified, immutable view of one run (cache entry, never evidence)."""

    run_id: UUID
    graph_digest: str
    fingerprint: str
    stored: StoredGraph
    nodes: dict[str, ArchitectureNode] = field(default_factory=dict)
    node_digest: dict[str, str] = field(default_factory=dict)
    edges: dict[str, ArchitectureEdge] = field(default_factory=dict)
    edge_digest: dict[str, str] = field(default_factory=dict)
    components: dict[str, ArchitectureComponent] = field(default_factory=dict)
    component_digest: dict[str, str] = field(default_factory=dict)
    diagnostic_digest: dict[int, str] = field(default_factory=dict)
    module_graphs: dict[str, Graph] = field(default_factory=dict)
    children: dict[str | None, list[str]] = field(default_factory=dict)

    @classmethod
    def of(cls, run: ArchitectureRun, stored: StoredGraph, fingerprint: str) -> LoadedGraph:
        assert run.graph_digest is not None
        value = cls(run.id, run.graph_digest, fingerprint, stored)
        for node, stored_digest in zip(stored.nodes, stored.node_digests, strict=True):
            value.nodes[node.key] = node
            value.node_digest[node.key] = stored_digest
            value.children.setdefault(node.parent_key, []).append(node.key)
        for edge, stored_digest in zip(stored.edges, stored.edge_digests, strict=True):
            value.edges[edge.key] = edge
            value.edge_digest[edge.key] = stored_digest
        for component, stored_digest in zip(
            stored.components, stored.component_digests, strict=True
        ):
            value.components[component.key] = component
            value.component_digest[component.key] = stored_digest
        for diagnostic, stored_digest in zip(
            stored.diagnostics, stored.diagnostic_digests, strict=True
        ):
            value.diagnostic_digest[diagnostic.ordinal] = stored_digest
        module_edges = [
            GraphEdge(edge.key, edge.source_key, edge.target_key, edge.import_class)
            for edge in stored.edges
            if edge.kind in MODULE_EDGE_KINDS
            and edge.target_key is not None
            and edge.target_key.startswith("module:")
        ]
        modules = [key for key, node in value.nodes.items() if node.kind == "module"]
        for name in CLASS_SET_NAMES:
            value.module_graphs[name] = Graph(module_edges, CLASS_SETS[name], modules)
        for keys in value.children.values():
            keys.sort()
        return value

    def edge_by_ordinal(self) -> dict[int, ArchitectureEdge]:
        return {edge.ordinal: edge for edge in self.edges.values()}


def verify_structure(run: ArchitectureRun, stored: StoredGraph) -> list[str]:
    """Full structural verification (I-C1); returns failure descriptions, empty when valid."""
    failures: list[str] = []
    pairs: list[tuple[str, str, str]] = []
    for node, value in zip(stored.nodes, stored.node_digests, strict=True):
        pairs.append((f"node {node.key}", node_digest(node), value))
    for edge, value in zip(stored.edges, stored.edge_digests, strict=True):
        pairs.append((f"edge {edge.key}", edge_digest(edge), value))
    for component, value in zip(stored.components, stored.component_digests, strict=True):
        pairs.append((f"component {component.key}", component_digest(component), value))
    for diagnostic, value in zip(stored.diagnostics, stored.diagnostic_digests, strict=True):
        pairs.append((f"diagnostic {diagnostic.ordinal}", diagnostic_digest(diagnostic), value))
    failures.extend(
        f"{label} digest mismatch" for label, actual, expected in pairs if actual != expected
    )
    for label, elements in (
        ("node", stored.nodes),
        ("edge", stored.edges),
        ("component", stored.components),
        ("diagnostic", stored.diagnostics),
    ):
        failures.extend(
            f"{label} {getattr(element, 'key', getattr(element, 'ordinal', '?'))} has no evidence"
            for element in elements
            if not element.evidence
        )
    graph = graph_digest_of(
        stored.node_digests,
        stored.edge_digests,
        stored.component_digests,
        stored.diagnostic_digests,
        run.coverage,
    )
    if graph != run.graph_digest:
        failures.append("graph digest mismatch")
    try:
        parents = parents_of(stored.nodes)
        module_edges = [edge for edge in stored.edges if edge.kind != "depends_on"]
        expected_aggregates = {
            shape.key: (shape.count, shape.attributes())
            for shape in aggregate_shapes(module_edges, parents)
        }
        observed_aggregates = {
            edge.key: (edge.count, edge.attributes)
            for edge in stored.edges
            if edge.kind == "depends_on"
        }
        if expected_aggregates != observed_aggregates:
            failures.append("aggregate edges differ from their constituents")
        expected_components = [
            (shape.key, shape.level, shape.class_set, shape.scope, shape.members, shape.witness,
             shape.attributes())
            for shape in component_shapes(list(stored.edges), parents)
        ]  # fmt: skip
        observed_components = [
            (c.key, c.level, c.class_set, c.scope_key, c.members, c.witness, c.attributes)
            for c in stored.components
        ]
        if expected_components != observed_components:
            failures.append("components differ from a recomputation over the persisted edges")
    except (KeyError, StopIteration):
        failures.append("hierarchy or edges reference missing nodes")
    return failures


class ArchitectureGraphCache:
    """Per-process LRU of verified topology; holds no verification results (I-C5)."""

    def __init__(self, size: int = CACHE_RUNS) -> None:
        self._items: OrderedDict[UUID, LoadedGraph] = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def get(self, run_id: UUID) -> LoadedGraph | None:
        with self._lock:
            value = self._items.get(run_id)
            if value is not None:
                self._items.move_to_end(run_id)
            return value

    def put(self, value: LoadedGraph) -> None:
        with self._lock:
            self._items[value.run_id] = value
            self._items.move_to_end(value.run_id)
            while len(self._items) > self._size:
                self._items.popitem(last=False)

    def evict(self, run_id: UUID) -> None:
        with self._lock:
            self._items.pop(run_id, None)


# --------------------------------------------------------------------------- service


@dataclass(frozen=True, slots=True)
class ArchitectureStatus:
    latest_attempt: ArchitectureRun | None
    active_run: ArchitectureRun | None
    freshness: str
    freshness_reasons: tuple[str, ...]
    contract: dict[str, object]
    catalog_ready: bool


@dataclass(frozen=True, slots=True)
class Assertion:
    """What a response asserts or aggregates; all of it is verified before returning."""

    nodes: frozenset[str] = frozenset()
    edges: frozenset[str] = frozenset()
    components: frozenset[str] = frozenset()
    diagnostics: frozenset[int] = frozenset()

    def __or__(self, other: Assertion) -> Assertion:
        return Assertion(
            self.nodes | other.nodes,
            self.edges | other.edges,
            self.components | other.components,
            self.diagnostics | other.diagnostics,
        )


class ArchitectureService:
    def __init__(
        self,
        store: ArchitectureStore,
        catalog: SemanticCatalogService,
        lock: threading.Lock | None = None,
        cache: ArchitectureGraphCache | None = None,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._lock = lock or threading.Lock()
        self._cache = cache or ArchitectureGraphCache()

    # ----------------------------------------------------------------- authority

    def freshness(
        self, project_id: UUID, snapshot_id: UUID, run: ArchitectureRun
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if run.quarantined:
            reasons.append("ARCHITECTURE_QUARANTINED")
        if run.outcome == "failed":
            reasons.append("ARCHITECTURE_FAILED")
        current = architecture_contract()
        if digest(run.architecture_contract) != digest(current):
            reasons.append("ARCHITECTURE_CONTRACT_CHANGED")
            if run.architecture_contract.get("architecture") == current["architecture"] and (
                run.architecture_contract.get("literals") != current["literals"]
            ):
                reasons.append("LITERAL_CONTRACT_CHANGED")
        status = self._catalog.status(project_id, snapshot_id)
        if status.active_run is None or status.active_run.id != run.catalog_run_id:
            reasons.append("CATALOG_AUTHORITY_CHANGED")
        else:
            reasons.extend(self._catalog.freshness(project_id, snapshot_id, status.active_run))
        return tuple(dict.fromkeys(reasons))

    def status(self, project_id: UUID, snapshot_id: UUID) -> ArchitectureStatus:
        self._catalog.require_snapshot(project_id, snapshot_id)
        latest = self._store.latest(project_id, snapshot_id)
        active = self._store.active(project_id, snapshot_id)
        reasons = self.freshness(project_id, snapshot_id, active) if active else ()
        catalog = self._catalog.status(project_id, snapshot_id)
        return ArchitectureStatus(
            latest,
            active,
            "none" if active is None else "stale" if reasons else "fresh",
            reasons,
            architecture_contract(),
            catalog.freshness == "fresh",
        )

    def build(self, project_id: UUID, snapshot_id: UUID) -> ArchitectureRun:
        if not self._lock.acquire(blocking=False):
            raise ArchitectureBusyError("An architecture build is already running.")
        try:
            catalog, reasons = self._catalog.select(project_id, snapshot_id, None)
            if reasons:  # pragma: no cover - select raises for stale current reads
                raise SemanticCatalogRequiredError("The semantic catalog is not current.")
            assert catalog.catalog_digest is not None
            structural = catalog.structural_analysis_id
            contract = architecture_contract()
            authority = digest(
                {
                    "snapshot": str(snapshot_id),
                    "catalog": [str(catalog.id), catalog.catalog_digest],
                    "structure": [str(structural), catalog.structural_revision],
                    "contract": contract,
                }
            )
            active = self._store.active(project_id, snapshot_id)
            if (
                active is not None
                and active.input_authority_digest == authority
                and not self.freshness(project_id, snapshot_id, active)
            ):
                return active
            latest = self._store.latest(project_id, snapshot_id)
            started = datetime.now(UTC)
            run = ArchitectureRun(
                uuid4(),
                project_id,
                snapshot_id,
                0,
                started,
                started,
                "failed",
                catalog.id,
                catalog.catalog_digest,
                structural,
                catalog.structural_revision,
                contract,
                authority,
                None,
                None,
                None,
                {},
                captured_active_architecture_run_id=active.id if active else None,
                captured_latest_revision=latest.revision if latest else 0,
            )
            try:
                build = build_architecture(self._store.load_input(catalog))
            except ArchitectureBuildError as error:
                self._store.save_failure(replace(run, completed_at=datetime.now(UTC)), error.code)
                raise
            run = replace(
                run,
                completed_at=datetime.now(UTC),
                outcome=build.outcome,
                graph_digest=build.graph_digest,
                architecture_digest=build.architecture_digest,
                portable_digest=build.portable_digest,
                coverage=build.coverage,
            )
            try:
                return self._store.publish(run, build)
            except ArchitectureConflictError:
                self._store.save_failure(run, "ARCHITECTURE_AUTHORITY_CHANGED")
                raise
        finally:
            self._lock.release()

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[ArchitectureRun], int]:
        self._catalog.require_snapshot(project_id, snapshot_id)
        return self._store.history(project_id, snapshot_id, limit, offset)

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> ArchitectureRun:
        self._catalog.require_snapshot(project_id, snapshot_id)
        value = self._store.run(project_id, snapshot_id, run_id)
        if value is None:
            raise ArchitectureNotFoundError("Architecture run not found.")
        return value

    def select(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None
    ) -> tuple[ArchitectureRun, tuple[str, ...]]:
        """Current reads need a fresh active run; explicit history is labelled."""
        if run_id is not None:
            run = self.run(project_id, snapshot_id, run_id)
            if run.outcome == "failed":
                raise ArchitectureNotFoundError("Failed architecture runs have no graph.")
            return run, self.freshness(project_id, snapshot_id, run)
        self._catalog.require_snapshot(project_id, snapshot_id)
        active = self._store.active(project_id, snapshot_id)
        if active is None:
            raise ArchitectureRequiredError("Build the architecture model for this snapshot.")
        reasons = self.freshness(project_id, snapshot_id, active)
        if reasons:
            raise ArchitectureStaleError(reasons)
        return active, ()

    # ----------------------------------------------------------------- verified access

    def graph(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None
    ) -> tuple[ArchitectureRun, tuple[str, ...], LoadedGraph]:
        run, reasons = self.select(project_id, snapshot_id, run_id)
        fingerprint = self._store.fingerprint(run.id)
        cached = self._cache.get(run.id)
        if cached is not None and cached.graph_digest == run.graph_digest:
            if cached.fingerprint == fingerprint:
                return run, reasons, cached
            self._cache.evict(run.id)
            raise ArchitectureIntegrityError(["persisted rows changed after verification"])
        stored, loaded_fingerprint = self._store.load_graph(run.id)
        failures = verify_structure(run, stored)
        if loaded_fingerprint != fingerprint:
            failures.append("persisted rows changed during verification")
        if failures:
            raise ArchitectureIntegrityError(failures)
        value = LoadedGraph.of(run, stored, loaded_fingerprint)
        self._cache.put(value)
        return run, reasons, value

    def verify_assertion(
        self,
        run: ArchitectureRun,
        loaded: LoadedGraph,
        assertion: Assertion,
        deadline: float | None = None,
    ) -> None:
        """I-C3: re-read, re-digest and verify the evidence of every asserted element."""
        try:
            by_key = loaded.edges
            ordinals = sorted(by_key[key].ordinal for key in assertion.edges)
        except KeyError as error:
            raise ArchitectureIntegrityError([f"unknown edge {error}"]) from error
        fresh = self._store.reload(
            run.id,
            nodes=sorted(assertion.nodes),
            edges=ordinals,
            components=sorted(assertion.components),
            diagnostics=sorted(assertion.diagnostics),
        )
        failures: list[str] = []
        links: set[EvidenceLink] = set()

        def check(label: str, actual: str, stored: str, cached: str | None) -> None:
            if actual != stored or stored != cached:
                failures.append(f"{label} changed since verification")

        if len(fresh.nodes) != len(assertion.nodes) or len(fresh.edges) != len(ordinals):
            failures.append("asserted rows are missing")
        for node, stored in zip(fresh.nodes, fresh.node_digests, strict=True):
            check(f"node {node.key}", node_digest(node), stored, loaded.node_digest.get(node.key))
            links.update(node.evidence)
        for edge, stored in zip(fresh.edges, fresh.edge_digests, strict=True):
            check(f"edge {edge.key}", edge_digest(edge), stored, loaded.edge_digest.get(edge.key))
            links.update(edge.evidence)
        for component, stored in zip(fresh.components, fresh.component_digests, strict=True):
            check(
                f"component {component.key}",
                component_digest(component),
                stored,
                loaded.component_digest.get(component.key),
            )
            links.update(component.evidence)
        for diagnostic, stored in zip(fresh.diagnostics, fresh.diagnostic_digests, strict=True):
            check(
                f"diagnostic {diagnostic.ordinal}",
                diagnostic_digest(diagnostic),
                stored,
                loaded.diagnostic_digest.get(diagnostic.ordinal),
            )
            links.update(diagnostic.evidence)
        if failures:
            self._cache.evict(run.id)
            raise ArchitectureIntegrityError(failures)
        if len(links) > VERIFICATION_BUDGET or (
            deadline is not None and time.monotonic() > deadline
        ):
            raise ArchitectureBudgetError(
                f"{len(links)} evidence items exceed the verification budget; narrow the query."
            )
        statuses = self._store.verify_evidence(
            run, sorted(links, key=lambda link: (link.source, link.digest))
        )
        invalid = {
            f"{link.source}:{link.digest}": status
            for link, status in statuses.items()
            if status != "verified"
        }
        if invalid or len(statuses) != len(links):
            self._cache.evict(run.id)
            raise ArchitectureEvidenceInvalidError(invalid or {"missing": "EVIDENCE_NOT_CHECKED"})

    def evidence(
        self, project_id: UUID, snapshot_id: UUID, value: str, run_id: UUID | None
    ) -> tuple[ArchitectureRun, ArchitectureEvidence]:
        """Non-authoritative inspection: returns the row with its verification state."""
        run, _ = self.select(project_id, snapshot_id, run_id)
        rows = self._store.evidence(run.id, [value])
        if not rows:
            raise ArchitectureNotFoundError("Evidence not found in this run.")
        link = EvidenceLink("architecture", value)
        status = self._store.verify_evidence(run, [link]).get(link, "EVIDENCE_NOT_CHECKED")
        return run, replace(rows[0], verification=status)

    def integrity(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None
    ) -> dict[str, object]:
        """Non-authoritative full inspection: structure plus every evidence item."""
        run = (
            self.run(project_id, snapshot_id, run_id)
            if run_id
            else self.select(project_id, snapshot_id, None)[0]
        )
        if run.outcome == "failed":
            raise ArchitectureNotFoundError("Failed architecture runs have no graph.")
        stored, _ = self._store.load_graph(run.id)
        structural = verify_structure(run, stored)
        links = {
            link
            for group in (stored.nodes, stored.edges, stored.components, stored.diagnostics)
            for element in group
            for link in element.evidence
        }
        statuses = self._store.verify_evidence(
            run, sorted(links, key=lambda link: (link.source, link.digest))
        )
        invalid = {
            f"{link.source}:{link.digest}": status
            for link, status in statuses.items()
            if status != "verified"
        }
        if structural or invalid:
            self._cache.evict(run.id)
        return {
            "run_id": str(run.id),
            "structure": "verified" if not structural else "failed",
            "structural_failures": structural[:50],
            "evidence_checked": len(statuses),
            "evidence_failures": dict(sorted(invalid.items())[:50]),
            "evidence_failure_count": len(invalid),
            "label": "non-authoritative inspection",
        }
