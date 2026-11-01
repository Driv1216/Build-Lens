"""Stage 15 Milestone B: the flow model service — build, freshness, verified reads.

A flow model run captures one flow evidence run, the semantic catalog run and the architecture
run, all over the same structural analysis. It is current only while all three are the active,
fresh runs and the ``flow-model`` contract is unchanged (INV-4). Every authoritative read
re-verifies the elements it returns and the rows they cite (INV-6) and fails closed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from buildlens.application.architecture import ArchitectureService
from buildlens.application.flow_builder import FlowBuildError, build_flow
from buildlens.application.flow_evidence import FlowEvidenceService
from buildlens.application.flow_inputs import FlowInput
from buildlens.application.semantic_catalog import SemanticCatalogService
from buildlens.domain.flow import (
    FlowBuild,
    FlowDiagnostic,
    FlowEdge,
    FlowNode,
    FlowRun,
    flow_model_contract,
)
from buildlens.domain.flow_evidence import FlowEvidenceRun
from buildlens.domain.source import digest


class FlowNotFoundError(Exception):
    pass


class FlowRequiredError(Exception):
    pass


class FlowStaleError(Exception):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("The flow model run is stale.")
        self.reasons = reasons


class FlowBusyError(Exception):
    pass


class FlowConflictError(Exception):
    pass


class FlowInputsInconsistentError(Exception):
    pass


class FlowIntegrityError(Exception):
    def __init__(self, failures: list[str]) -> None:
        super().__init__("Flow model integrity verification failed.")
        self.failures = failures


class FlowStore(Protocol):
    def load_input(
        self,
        evidence_run: FlowEvidenceRun,
        catalog_run_id: UUID,
        catalog_digest: str,
        architecture_run_id: UUID,
        architecture_graph_digest: str,
    ) -> FlowInput: ...
    def latest(self, project_id: UUID, snapshot_id: UUID) -> FlowRun | None: ...
    def active(self, project_id: UUID, snapshot_id: UUID) -> FlowRun | None: ...
    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> FlowRun | None: ...
    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[FlowRun], int]: ...
    def publish(self, run: FlowRun, build: FlowBuild) -> FlowRun: ...
    def save_failure(self, run: FlowRun, code: str) -> FlowRun: ...
    def node(self, run_id: UUID, key: str) -> FlowNode | None: ...
    def edges(
        self,
        run_id: UUID,
        *,
        source: str | None = None,
        target: str | None = None,
        relation: str | None = None,
        tier: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> tuple[list[FlowEdge], int]: ...
    def diagnostics(self, run_id: UUID) -> list[FlowDiagnostic]: ...
    def verify(self, run: FlowRun, node_keys: list[str], edge_keys: list[str]) -> list[str]: ...
    def integrity(self, run: FlowRun) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FlowStatus:
    latest: FlowRun | None
    active: FlowRun | None
    freshness: str
    freshness_reasons: tuple[str, ...]
    contract: dict[str, Any]
    inputs_ready: bool


class FlowService:
    def __init__(
        self,
        store: FlowStore,
        evidence: FlowEvidenceService,
        catalog: SemanticCatalogService,
        architecture: ArchitectureService,
        lock: threading.Lock | None = None,
    ) -> None:
        self._store, self._evidence, self._catalog, self._architecture = (
            store,
            evidence,
            catalog,
            architecture,
        )
        self._lock = lock or threading.Lock()

    # ----------------------------------------------------------------- authority

    def freshness(self, project_id: UUID, snapshot_id: UUID, run: FlowRun) -> tuple[str, ...]:
        reasons: list[str] = []
        if run.outcome == "failed":
            reasons.append("FLOW_FAILED")
        if digest(run.contract) != digest(flow_model_contract()):
            reasons.append("FLOW_CONTRACT_CHANGED")
        evidence = self._evidence.status(project_id, snapshot_id)
        if evidence.active is None or evidence.active.id != run.flow_evidence_run_id:
            reasons.append("FLOW_EVIDENCE_AUTHORITY_CHANGED")
        else:
            reasons.extend(evidence.freshness_reasons)
        catalog = self._catalog.status(project_id, snapshot_id)
        if catalog.active_run is None or catalog.active_run.id != run.catalog_run_id:
            reasons.append("CATALOG_AUTHORITY_CHANGED")
        elif catalog.freshness != "fresh":
            reasons.extend(catalog.freshness_reasons)
        architecture = self._architecture.status(project_id, snapshot_id)
        if architecture.active_run is None or architecture.active_run.id != run.architecture_run_id:
            reasons.append("ARCHITECTURE_AUTHORITY_CHANGED")
        elif architecture.freshness != "fresh":
            reasons.extend(architecture.freshness_reasons)
        return tuple(dict.fromkeys(reasons))

    def status(self, project_id: UUID, snapshot_id: UUID) -> FlowStatus:
        self._evidence.require_snapshot(project_id, snapshot_id)
        latest = self._store.latest(project_id, snapshot_id)
        active = self._store.active(project_id, snapshot_id)
        reasons = self.freshness(project_id, snapshot_id, active) if active else ()
        ready = (
            self._evidence.status(project_id, snapshot_id).freshness == "fresh"
            and self._catalog.status(project_id, snapshot_id).freshness == "fresh"
            and self._architecture.status(project_id, snapshot_id).freshness == "fresh"
        )
        return FlowStatus(
            latest,
            active,
            "none" if active is None else "stale" if reasons else "fresh",
            reasons,
            flow_model_contract(),
            ready,
        )

    def build(self, project_id: UUID, snapshot_id: UUID) -> FlowRun:
        if not self._lock.acquire(blocking=False):
            raise FlowBusyError("A flow model build is already running.")
        try:
            evidence, _ = self._evidence.select(project_id, snapshot_id, None)
            catalog, _ = self._catalog.select(project_id, snapshot_id, None)
            architecture, _ = self._architecture.select(project_id, snapshot_id, None)
            structures = {
                evidence.structural_analysis_id,
                catalog.structural_analysis_id,
                architecture.structural_analysis_id,
            }
            if len(structures) != 1 or architecture.catalog_run_id != catalog.id:
                raise FlowInputsInconsistentError(
                    "Flow evidence, catalog and architecture must share one structural analysis."
                )
            assert evidence.evidence_digest and catalog.catalog_digest and architecture.graph_digest
            contract = flow_model_contract()
            authority = digest(
                {
                    "snapshot": str(snapshot_id),
                    "evidence": [str(evidence.id), evidence.evidence_digest],
                    "catalog": [str(catalog.id), catalog.catalog_digest],
                    "architecture": [str(architecture.id), architecture.graph_digest],
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
            run = FlowRun(
                uuid4(),
                project_id,
                snapshot_id,
                0,
                started,
                started,
                "failed",
                evidence.id,
                evidence.evidence_digest,
                catalog.id,
                catalog.catalog_digest,
                architecture.id,
                architecture.graph_digest,
                evidence.structural_analysis_id,
                contract,
                authority,
                None,
                None,
                captured_active_run_id=active.id if active else None,
                captured_latest_revision=latest.revision if latest else 0,
            )
            inputs = self._store.load_input(
                evidence,
                catalog.id,
                catalog.catalog_digest,
                architecture.id,
                architecture.graph_digest,
            )
            try:
                build = build_flow(inputs)
            except FlowBuildError as error:
                self._store.save_failure(replace(run, completed_at=datetime.now(UTC)), error.code)
                raise
            run = replace(
                run,
                completed_at=datetime.now(UTC),
                outcome=build.outcome,
                graph_digest=build.graph_digest,
                portable_digest=build.portable_digest,
                coverage=build.coverage,
            )
            try:
                return self._store.publish(run, build)
            except FlowConflictError:
                self._store.save_failure(run, "FLOW_AUTHORITY_CHANGED")
                raise
        finally:
            self._lock.release()

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[FlowRun], int]:
        self._evidence.require_snapshot(project_id, snapshot_id)
        return self._store.history(project_id, snapshot_id, limit, offset)

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> FlowRun:
        self._evidence.require_snapshot(project_id, snapshot_id)
        value = self._store.run(project_id, snapshot_id, run_id)
        if value is None:
            raise FlowNotFoundError("Flow model run not found.")
        return value

    def select(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None
    ) -> tuple[FlowRun, tuple[str, ...]]:
        if run_id is not None:
            run = self.run(project_id, snapshot_id, run_id)
            if run.outcome == "failed":
                raise FlowNotFoundError("Failed flow model runs hold no graph.")
            return run, self.freshness(project_id, snapshot_id, run)
        self._evidence.require_snapshot(project_id, snapshot_id)
        active = self._store.active(project_id, snapshot_id)
        if active is None:
            raise FlowRequiredError("Build the flow model for this snapshot.")
        reasons = self.freshness(project_id, snapshot_id, active)
        if reasons:
            raise FlowStaleError(reasons)
        return active, ()

    # ----------------------------------------------------------------- verified reads

    def node(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None, key: str, limit: int = 200
    ) -> dict[str, Any]:
        run, reasons = self.select(project_id, snapshot_id, run_id)
        node = self._store.node(run.id, key)
        if node is None:
            raise FlowNotFoundError("Flow node not found.")
        outgoing, out_total = self._store.edges(run.id, source=key, limit=limit)
        incoming, in_total = self._store.edges(run.id, target=key, limit=limit)
        failures = self._store.verify(run, [key], [e.key for e in outgoing + incoming])
        if failures:
            raise FlowIntegrityError(failures)
        return {
            "run": run,
            "freshness_reasons": reasons,
            "node": node,
            "outgoing": outgoing,
            "incoming": incoming,
            "outgoing_total": out_total,
            "incoming_total": in_total,
        }

    def edges(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None, **filters: Any
    ) -> dict[str, Any]:
        run, reasons = self.select(project_id, snapshot_id, run_id)
        edges, total = self._store.edges(run.id, **filters)
        failures = self._store.verify(run, [], [e.key for e in edges])
        if failures:
            raise FlowIntegrityError(failures)
        return {"run": run, "freshness_reasons": reasons, "edges": edges, "total": total}

    def integrity(self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None) -> dict[str, Any]:
        run = (
            self.run(project_id, snapshot_id, run_id)
            if run_id
            else self.select(project_id, snapshot_id, None)[0]
        )
        return {"run": run, **self._store.integrity(run)}

    def diagnostics(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None
    ) -> dict[str, Any]:
        run, reasons = self.select(project_id, snapshot_id, run_id)
        return {
            "run": run,
            "freshness_reasons": reasons,
            "diagnostics": self._store.diagnostics(run.id),
        }
