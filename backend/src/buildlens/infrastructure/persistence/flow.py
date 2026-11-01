"""PostgreSQL persistence for Stage 15 flow model runs (Milestone B).

Inputs are loaded from PostgreSQL only (zero source reads). Publication follows the Stage 13/14
pattern. Reads re-verify every returned element against its stored digest and every cited row
against the row digest recorded in its evidence item (INV-6); a mismatch fails closed.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from buildlens.application.flow import FlowConflictError, FlowIntegrityError, FlowStore
from buildlens.application.flow_inputs import (
    ExportInput,
    FlowInput,
    ImportInput,
    LiteralInput,
    ScopeInput,
    Stage14Element,
    SymbolInput,
    fact_row_digest,
)
from buildlens.domain.flow import (
    FlowBuild,
    FlowDiagnostic,
    FlowEdge,
    FlowEvidenceItem,
    FlowNode,
    FlowRun,
    graph_digest_of,
)
from buildlens.domain.flow_evidence import (
    FlowCallsite,
    FlowEvidenceRun,
    FlowJsxBinding,
    FlowScope,
    callsite_digest,
    jsx_digest,
)
from buildlens.domain.source import digest
from buildlens.domain.structural import SourceSpan
from buildlens.infrastructure.persistence.flow_evidence import SqlAlchemyFlowEvidenceStore
from buildlens.infrastructure.persistence.models import (
    ArchitectureEdgeRecord,
    ArchitectureLiteralRecord,
    ArchitectureNodeRecord,
    ArtifactAnalysisRecord,
    ArtifactExtractionRecord,
    ArtifactRecord,
    ExportReferenceRecord,
    FlowArtifactCoverageRecord,
    FlowCallsiteRecord,
    FlowDiagnosticRecord,
    FlowEdgeRecord,
    FlowEvidenceItemRecord,
    FlowJsxBindingRecord,
    FlowNodeRecord,
    FlowRunRecord,
    FlowScopeRecord,
    FlowTraceRecord,
    ImportReferenceRecord,
    ProjectVersionRecord,
    SymbolRecord,
)

_BATCH = 1000
_STAGE14_NODES = ("client_call", "api_endpoint", "db_table")
_STAGE14_EDGES = ("calls_endpoint", "maps_table")


def _span(r: Any) -> SourceSpan:
    return SourceSpan(r.start_line, r.end_line, r.start_column, r.end_column)


def _callsite(c: FlowCallsiteRecord) -> FlowCallsite:
    return FlowCallsite(
        c.ordinal,
        c.cfg_node,
        dict(c.callee),
        tuple(c.arguments),
        c.awaited,
        _span(c),
        c.start_offset,
        c.end_offset,
        c.text_key,
        tuple(c.copies),
    )


def _jsx(j: FlowJsxBindingRecord) -> FlowJsxBinding:
    return FlowJsxBinding(
        j.ordinal,
        j.cfg_node,
        j.element,
        j.intrinsic,
        j.attribute,
        dict(j.value),
        _span(j),
        j.start_offset,
        j.end_offset,
    )


def _scope(
    r: FlowScopeRecord, callsites: list[FlowCallsite], jsx: list[FlowJsxBinding]
) -> FlowScope:
    return FlowScope(
        r.scope_key,
        r.parent_scope_key,
        r.kind,
        r.callable_kind,
        r.name,
        _span(r),
        r.start_offset,
        r.end_offset,
        r.truncated,
        dict(r.cfg),
        dict(r.summary),
        tuple(callsites),
        tuple(jsx),
        r.owner_symbol_id,
    )


def _run(r: FlowRunRecord) -> FlowRun:
    return FlowRun(
        r.id,
        r.project_id,
        r.project_version_id,
        r.revision,
        r.started_at,
        r.completed_at,
        r.outcome,
        r.flow_evidence_run_id,
        r.evidence_digest,
        r.catalog_run_id,
        r.catalog_digest,
        r.architecture_run_id,
        r.architecture_graph_digest,
        r.structural_analysis_id,
        dict(r.contract),
        r.input_authority_digest,
        r.graph_digest,
        r.portable_digest,
        dict(r.coverage),
        tuple(r.diagnostics),
        r.superseded,
        r.captured_active_flow_run_id,
        r.captured_latest_revision,
        r.failure_code,
    )


def _node(r: FlowNodeRecord) -> FlowNode:
    return FlowNode(r.node_key, r.kind, r.label, r.locator, dict(r.attributes), tuple(r.evidence))


def _edge(r: FlowEdgeRecord) -> FlowEdge:
    return FlowEdge(
        r.edge_key,
        r.relation,
        r.source_key,
        r.target_key,
        r.tier,
        r.rule,
        tuple(r.conditions),
        dict(r.attributes),
        tuple(r.evidence),
    )


class SqlAlchemyFlowStore(FlowStore):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._evidence = SqlAlchemyFlowEvidenceStore(session_factory)

    # ----------------------------------------------------------------- inputs

    def load_input(
        self,
        evidence_run: FlowEvidenceRun,
        catalog_run_id: UUID,
        catalog_digest: str,
        architecture_run_id: UUID,
        architecture_graph_digest: str,
    ) -> FlowInput:
        run_id = evidence_run.id
        analysis_id = evidence_run.structural_analysis_id
        with self._session_factory() as session:
            coverage_rows = session.scalars(
                select(FlowArtifactCoverageRecord).where(
                    FlowArtifactCoverageRecord.flow_evidence_run_id == run_id
                )
            ).all()
            by_analysis = {c.artifact_analysis_id: c for c in coverage_rows}
            callsites: dict[tuple[UUID, str], list[FlowCallsite]] = defaultdict(list)
            for c in session.scalars(
                select(FlowCallsiteRecord)
                .where(FlowCallsiteRecord.flow_evidence_run_id == run_id)
                .order_by(
                    FlowCallsiteRecord.artifact_analysis_id,
                    FlowCallsiteRecord.scope_key,
                    FlowCallsiteRecord.ordinal,
                )
            ):
                callsites[(c.artifact_analysis_id, c.scope_key)].append(_callsite(c))
            jsx: dict[tuple[UUID, str], list[FlowJsxBinding]] = defaultdict(list)
            for j in session.scalars(
                select(FlowJsxBindingRecord)
                .where(FlowJsxBindingRecord.flow_evidence_run_id == run_id)
                .order_by(
                    FlowJsxBindingRecord.artifact_analysis_id,
                    FlowJsxBindingRecord.scope_key,
                    FlowJsxBindingRecord.ordinal,
                )
            ):
                jsx[(j.artifact_analysis_id, j.scope_key)].append(_jsx(j))
            scopes: list[ScopeInput] = []
            for r in session.scalars(
                select(FlowScopeRecord).where(FlowScopeRecord.flow_evidence_run_id == run_id)
            ):
                cov = by_analysis[r.artifact_analysis_id]
                key = (r.artifact_analysis_id, r.scope_key)
                scopes.append(
                    ScopeInput(
                        r.artifact_analysis_id,
                        cov.locator,
                        cov.language or "",
                        _scope(r, callsites.get(key, []), jsx.get(key, [])),
                    )
                )
            scopes.sort(
                key=lambda s: (
                    s.locator,
                    [int(p) if p.isdigit() else -1 for p in s.scope.key.split(".")[1:]],
                )
            )
            locators = {
                a.id: a.locator
                for a in session.scalars(
                    select(ArtifactAnalysisRecord).where(
                        ArtifactAnalysisRecord.structural_analysis_id == analysis_id
                    )
                )
            }
            imports = tuple(
                ImportInput(
                    i.artifact_analysis_id,
                    locators.get(i.artifact_analysis_id, ""),
                    i.owner_symbol_id,
                    i.form,
                    i.from_module,
                    i.name,
                    i.alias,
                    i.level,
                    i.statement_ordinal,
                    i.item_ordinal,
                )
                for i in session.scalars(
                    select(ImportReferenceRecord)
                    .where(ImportReferenceRecord.structural_analysis_id == analysis_id)
                    .order_by(
                        ImportReferenceRecord.artifact_analysis_id,
                        ImportReferenceRecord.statement_ordinal,
                        ImportReferenceRecord.item_ordinal,
                    )
                )
            )
            exports = tuple(
                ExportInput(
                    e.artifact_analysis_id,
                    locators.get(e.artifact_analysis_id, ""),
                    e.kind,
                    e.name,
                    e.local_name,
                    e.source_module,
                    e.ordinal,
                )
                for e in session.scalars(
                    select(ExportReferenceRecord)
                    .where(ExportReferenceRecord.artifact_analysis_id.in_(list(locators)))
                    .order_by(
                        ExportReferenceRecord.artifact_analysis_id, ExportReferenceRecord.ordinal
                    )
                )
            )
            symbols = tuple(
                SymbolInput(
                    s.id,
                    s.artifact_analysis_id,
                    locators.get(s.artifact_analysis_id, ""),
                    s.kind,
                    s.name,
                    s.qualified_name,
                    s.stable_identity_id,
                    s.parent_id,
                    s.exported,
                )
                for s in session.scalars(
                    select(SymbolRecord)
                    .where(SymbolRecord.structural_analysis_id == analysis_id)
                    .order_by(SymbolRecord.id)
                )
            )
            literals = tuple(
                LiteralInput(
                    lit.artifact_analysis_id,
                    locators.get(lit.artifact_analysis_id, ""),
                    lit.ordinal,
                    lit.kind,
                    (lit.start_line, lit.start_column, lit.end_line, lit.end_column),
                )
                for lit in session.scalars(
                    select(ArchitectureLiteralRecord)
                    .join(
                        ArtifactExtractionRecord,
                        ArtifactExtractionRecord.id == ArchitectureLiteralRecord.extraction_id,
                    )
                    .where(
                        ArtifactExtractionRecord.structural_analysis_id == analysis_id,
                        ArchitectureLiteralRecord.kind.in_(["http_call", "url_call"]),
                    )
                )
            )
            inventory = frozenset(
                session.scalars(
                    select(ArtifactRecord.locator).where(
                        ArtifactRecord.project_version_id == evidence_run.project_version_id,
                        ArtifactRecord.kind == "file",
                    )
                )
            )
            nodes = tuple(
                Stage14Element(
                    "architecture_nodes",
                    n.node_key,
                    n.kind,
                    n.node_digest,
                    attributes=dict(n.attributes),
                    catalog_entity_key=n.catalog_entity_key,
                    locator=n.locator,
                )
                for n in session.scalars(
                    select(ArchitectureNodeRecord)
                    .where(
                        ArchitectureNodeRecord.architecture_run_id == architecture_run_id,
                        ArchitectureNodeRecord.kind.in_(_STAGE14_NODES),
                    )
                    .order_by(ArchitectureNodeRecord.node_key)
                )
            )
            edges = tuple(
                Stage14Element(
                    "architecture_edges",
                    e.edge_key,
                    e.kind,
                    e.edge_digest,
                    e.source_key,
                    e.target_key,
                    e.tier,
                    tuple(e.conditions),
                    dict(e.attributes),
                )
                for e in session.scalars(
                    select(ArchitectureEdgeRecord)
                    .where(
                        ArchitectureEdgeRecord.architecture_run_id == architecture_run_id,
                        ArchitectureEdgeRecord.kind.in_(_STAGE14_EDGES),
                    )
                    .order_by(ArchitectureEdgeRecord.edge_key)
                )
            )
        return FlowInput(
            evidence_run,
            catalog_run_id,
            catalog_digest,
            architecture_run_id,
            architecture_graph_digest,
            inventory,
            {c.locator: c.state for c in coverage_rows},
            tuple(scopes),
            imports,
            exports,
            symbols,
            literals,
            nodes,
            edges,
            tuple(self._evidence.framework_facts(run_id)),
            tuple(self._evidence.module_facts(run_id)),
        )

    # ----------------------------------------------------------------- runs

    def _query(self, project_id: UUID, snapshot_id: UUID) -> Any:
        return select(FlowRunRecord).where(
            FlowRunRecord.project_id == project_id, FlowRunRecord.project_version_id == snapshot_id
        )

    def latest(self, project_id: UUID, snapshot_id: UUID) -> FlowRun | None:
        with self._session_factory() as session:
            r = session.scalar(
                self._query(project_id, snapshot_id)
                .order_by(FlowRunRecord.revision.desc())
                .limit(1)
            )
            return _run(r) if r else None

    def active(self, project_id: UUID, snapshot_id: UUID) -> FlowRun | None:
        with self._session_factory() as session:
            r = session.scalar(
                select(FlowRunRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.active_flow_run_id == FlowRunRecord.id,
                )
                .where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                )
            )
            return _run(r) if r else None

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> FlowRun | None:
        with self._session_factory() as session:
            r = session.scalar(
                self._query(project_id, snapshot_id).where(FlowRunRecord.id == run_id)
            )
            return _run(r) if r else None

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[FlowRun], int]:
        with self._session_factory() as session:
            total = session.scalar(
                select(func.count()).select_from(self._query(project_id, snapshot_id).subquery())
            )
            records = session.scalars(
                self._query(project_id, snapshot_id)
                .order_by(FlowRunRecord.revision.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return [_run(r) for r in records], int(total or 0)

    def _lock(self, session: Session, run: FlowRun) -> tuple[ProjectVersionRecord, int]:
        version = session.scalar(
            select(ProjectVersionRecord)
            .where(
                ProjectVersionRecord.id == run.project_version_id,
                ProjectVersionRecord.project_id == run.project_id,
            )
            .with_for_update()
        )
        if version is None:
            raise FlowConflictError("Snapshot disappeared before publication.")
        latest = session.scalar(
            select(func.max(FlowRunRecord.revision)).where(
                FlowRunRecord.project_version_id == run.project_version_id
            )
        )
        return version, int(latest or 0)

    @staticmethod
    def _record(run: FlowRun, revision: int, superseded: bool) -> FlowRunRecord:
        return FlowRunRecord(
            id=run.id,
            project_id=run.project_id,
            project_version_id=run.project_version_id,
            revision=revision,
            started_at=run.started_at,
            completed_at=run.completed_at,
            outcome=run.outcome,
            flow_evidence_run_id=run.flow_evidence_run_id,
            evidence_digest=run.evidence_digest,
            catalog_run_id=run.catalog_run_id,
            catalog_digest=run.catalog_digest,
            architecture_run_id=run.architecture_run_id,
            architecture_graph_digest=run.architecture_graph_digest,
            structural_analysis_id=run.structural_analysis_id,
            contract=run.contract,
            contract_digest=digest(run.contract),
            input_authority_digest=run.input_authority_digest,
            graph_digest=run.graph_digest,
            portable_digest=run.portable_digest,
            coverage=run.coverage,
            diagnostics=list(run.diagnostics),
            failure_code=run.failure_code,
            superseded=superseded,
            captured_active_flow_run_id=run.captured_active_run_id,
            captured_latest_revision=run.captured_latest_revision,
        )

    def publish(self, run: FlowRun, build: FlowBuild) -> FlowRun:
        with self._session_factory.begin() as session:
            version, latest = self._lock(session, run)
            if (
                version.active_flow_evidence_run_id != run.flow_evidence_run_id
                or version.active_semantic_catalog_run_id != run.catalog_run_id
                or version.active_architecture_run_id != run.architecture_run_id
                or version.active_analysis_id != run.structural_analysis_id
            ):
                raise FlowConflictError("Flow model input authority changed.")
            superseded = (
                latest != run.captured_latest_revision
                or version.active_flow_run_id != run.captured_active_run_id
            )
            record = self._record(run, latest + 1, superseded)
            session.add(record)
            session.flush()
            self._insert(session, run.id, build)
            if not superseded and run.outcome != "failed":
                version.active_flow_run_id = run.id
            return _run(record)

    def save_failure(self, run: FlowRun, code: str) -> FlowRun:
        with self._session_factory.begin() as session:
            _, latest = self._lock(session, run)
            failed = replace(
                run,
                outcome="failed",
                graph_digest=None,
                portable_digest=None,
                diagnostics=(code,),
                failure_code=code,
            )
            record = self._record(failed, latest + 1, False)
            session.add(record)
            session.flush()
            return _run(record)

    @staticmethod
    def _rows(session: Session, model: Any, rows: list[dict[str, Any]]) -> None:
        for start in range(0, len(rows), _BATCH):
            session.execute(insert(model), rows[start : start + _BATCH])

    def _insert(self, session: Session, run_id: UUID, build: FlowBuild) -> None:
        self._rows(
            session,
            FlowEvidenceItemRecord,
            [
                {
                    "flow_run_id": run_id,
                    "digest": e.digest,
                    "kind": e.kind,
                    "refs": list(e.refs),
                    "derived": e.derived,
                }
                for e in build.evidence
            ],
        )
        self._rows(
            session,
            FlowNodeRecord,
            [
                {
                    "flow_run_id": run_id,
                    "node_key": n.key,
                    "ordinal": i,
                    "kind": n.kind,
                    "label": n.label,
                    "locator": n.locator,
                    "attributes": n.attributes,
                    "evidence": list(n.evidence),
                    "node_digest": n.element_digest(),
                }
                for i, n in enumerate(build.nodes)
            ],
        )
        self._rows(
            session,
            FlowEdgeRecord,
            [
                {
                    "flow_run_id": run_id,
                    "ordinal": i,
                    "edge_key": e.key,
                    "relation": e.relation,
                    "family": e.family,
                    "source_key": e.source_key,
                    "target_key": e.target_key,
                    "tier": e.tier,
                    "rule": e.rule,
                    "conditions": list(e.conditions),
                    "attributes": e.attributes,
                    "evidence": list(e.evidence),
                    "edge_digest": e.element_digest(),
                }
                for i, e in enumerate(build.edges)
            ],
        )
        self._rows(
            session,
            FlowDiagnosticRecord,
            [
                {
                    "flow_run_id": run_id,
                    "ordinal": i,
                    "code": d.code,
                    "subject": d.subject,
                    "attributes": d.attributes,
                    "diagnostic_digest": d.digest,
                }
                for i, d in enumerate(build.diagnostics)
            ],
        )

    # ----------------------------------------------------------------- reads

    def node(self, run_id: UUID, key: str) -> FlowNode | None:
        with self._session_factory() as session:
            r = session.get(FlowNodeRecord, (run_id, key))
            return _node(r) if r else None

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
    ) -> tuple[list[FlowEdge], int]:
        with self._session_factory() as session:
            query = select(FlowEdgeRecord).where(FlowEdgeRecord.flow_run_id == run_id)
            if source is not None:
                query = query.where(FlowEdgeRecord.source_key == source)
            if target is not None:
                query = query.where(FlowEdgeRecord.target_key == target)
            if relation is not None:
                query = query.where(FlowEdgeRecord.relation == relation)
            if tier is not None:
                query = query.where(FlowEdgeRecord.tier == tier)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            records = session.scalars(
                query.order_by(FlowEdgeRecord.edge_key).limit(limit).offset(offset)
            ).all()
            return [_edge(r) for r in records], int(total or 0)

    def diagnostics(self, run_id: UUID) -> list[FlowDiagnostic]:
        with self._session_factory() as session:
            return [
                FlowDiagnostic(r.code, r.subject, dict(r.attributes))
                for r in session.scalars(
                    select(FlowDiagnosticRecord)
                    .where(FlowDiagnosticRecord.flow_run_id == run_id)
                    .order_by(FlowDiagnosticRecord.ordinal)
                )
            ]

    # ----------------------------------------------------------------- trace inputs (Milestone C)

    def graph(self, run_id: UUID) -> tuple[dict[str, FlowNode], list[FlowEdge]]:
        """Every node and edge of a run, for the trace engine's per-run cache."""
        with self._session_factory() as session:
            nodes = {
                r.node_key: _node(r)
                for r in session.scalars(
                    select(FlowNodeRecord)
                    .where(FlowNodeRecord.flow_run_id == run_id)
                    .order_by(FlowNodeRecord.node_key)
                )
            }
            edges = [
                _edge(r)
                for r in session.scalars(
                    select(FlowEdgeRecord)
                    .where(FlowEdgeRecord.flow_run_id == run_id)
                    .order_by(FlowEdgeRecord.edge_key)
                )
            ]
        return nodes, edges

    def scopes(
        self, evidence_run_id: UUID, keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], ScopeInput]:
        """Flow evidence scopes (with callsites and JSX) by (locator, scope key)."""
        wanted = set(keys)
        if not wanted:
            return {}
        found: dict[tuple[str, str], ScopeInput] = {}
        with self._session_factory() as session:
            coverage = {
                c.locator: c
                for c in session.scalars(
                    select(FlowArtifactCoverageRecord).where(
                        FlowArtifactCoverageRecord.flow_evidence_run_id == evidence_run_id,
                        FlowArtifactCoverageRecord.locator.in_({loc for loc, _ in wanted}),
                    )
                )
            }
            for locator, key in sorted(wanted):
                cov = coverage.get(locator)
                if cov is None:
                    continue
                r = session.get(FlowScopeRecord, (evidence_run_id, cov.artifact_analysis_id, key))
                if r is None:
                    continue
                calls = session.scalars(
                    select(FlowCallsiteRecord)
                    .where(
                        FlowCallsiteRecord.flow_evidence_run_id == evidence_run_id,
                        FlowCallsiteRecord.artifact_analysis_id == cov.artifact_analysis_id,
                        FlowCallsiteRecord.scope_key == key,
                    )
                    .order_by(FlowCallsiteRecord.ordinal)
                ).all()
                jsx = session.scalars(
                    select(FlowJsxBindingRecord)
                    .where(
                        FlowJsxBindingRecord.flow_evidence_run_id == evidence_run_id,
                        FlowJsxBindingRecord.artifact_analysis_id == cov.artifact_analysis_id,
                        FlowJsxBindingRecord.scope_key == key,
                    )
                    .order_by(FlowJsxBindingRecord.ordinal)
                ).all()
                found[(locator, key)] = ScopeInput(
                    cov.artifact_analysis_id,
                    locator,
                    cov.language or "",
                    _scope(r, [_callsite(c) for c in calls], [_jsx(j) for j in jsx]),
                )
        return found

    def fingerprints(self, evidence_run_id: UUID, locators: set[str]) -> dict[str, str | None]:
        """Input fingerprints of the artifacts a trace touches (its ``source_digest``)."""
        if not locators:
            return {}
        with self._session_factory() as session:
            return {
                r.locator: r.input_fingerprint
                for r in session.scalars(
                    select(FlowArtifactCoverageRecord).where(
                        FlowArtifactCoverageRecord.flow_evidence_run_id == evidence_run_id,
                        FlowArtifactCoverageRecord.locator.in_(locators),
                    )
                )
            }

    def pin(
        self, run: FlowRun, trace_digest: str, query: dict[str, Any], result: dict[str, Any]
    ) -> None:
        with self._session_factory.begin() as session:
            if session.get(FlowTraceRecord, (run.id, trace_digest)) is None:
                session.add(
                    FlowTraceRecord(
                        flow_run_id=run.id,
                        trace_digest=trace_digest,
                        query=query,
                        result=result,
                        reason="pinned",
                    )
                )

    def pinned(
        self, project_id: UUID, snapshot_id: UUID, trace_digest: str
    ) -> list[tuple[UUID, dict[str, Any], dict[str, Any]]]:
        with self._session_factory() as session:
            rows = session.execute(
                select(FlowTraceRecord.flow_run_id, FlowTraceRecord.query, FlowTraceRecord.result)
                .join(FlowRunRecord, FlowRunRecord.id == FlowTraceRecord.flow_run_id)
                .where(
                    FlowRunRecord.project_id == project_id,
                    FlowRunRecord.project_version_id == snapshot_id,
                    FlowTraceRecord.trace_digest == trace_digest,
                )
                .order_by(FlowRunRecord.revision)
            ).all()
        return [(r[0], dict(r[1]), dict(r[2])) for r in rows]

    # ----------------------------------------------------------------- verification (INV-6)

    def verify(
        self,
        run: FlowRun,
        node_keys: list[str],
        edge_keys: list[str],
        expected: dict[str, str] | None = None,
    ) -> list[str]:
        """Failures (empty when every element, evidence item and cited row verifies).

        ``expected`` maps element keys to the digests a cached computation used; a stored
        element that differs from them fails (the cache must equal the database)."""
        failures: list[str] = []
        with self._session_factory() as session:
            nodes = (
                session.scalars(
                    select(FlowNodeRecord).where(
                        FlowNodeRecord.flow_run_id == run.id, FlowNodeRecord.node_key.in_(node_keys)
                    )
                ).all()
                if node_keys
                else []
            )
            edges = (
                session.scalars(
                    select(FlowEdgeRecord).where(
                        FlowEdgeRecord.flow_run_id == run.id, FlowEdgeRecord.edge_key.in_(edge_keys)
                    )
                ).all()
                if edge_keys
                else []
            )
            if len(nodes) != len(set(node_keys)) or len(edges) != len(set(edge_keys)):
                failures.append("ELEMENT_MISSING")
            cited: set[str] = set()
            for n in nodes:
                if _node(n).element_digest() != n.node_digest:
                    failures.append(f"NODE_DIGEST_MISMATCH:{n.node_key}")
                if expected and expected.get(n.node_key, n.node_digest) != n.node_digest:
                    failures.append(f"CACHE_DIFFERS:{n.node_key}")
                cited.update(n.evidence)
            for e in edges:
                if _edge(e).element_digest() != e.edge_digest:
                    failures.append(f"EDGE_DIGEST_MISMATCH:{e.edge_key}")
                if expected and expected.get(e.edge_key, e.edge_digest) != e.edge_digest:
                    failures.append(f"CACHE_DIFFERS:{e.edge_key}")
                cited.update(e.evidence)
            failures += self._verify_evidence(session, run, cited)
        return failures

    def _verify_evidence(
        self, session: Session, run: FlowRun, digests: set[str], bulk: bool = False
    ) -> list[str]:
        """Re-verify evidence items and every row they cite. ``bulk`` (full integrity) preloads
        the cited flow evidence run's rows once instead of one lookup per citation."""
        failures: list[str] = []
        if not digests:
            return failures
        items = {
            r.digest: r
            for r in session.scalars(
                select(FlowEvidenceItemRecord).where(
                    FlowEvidenceItemRecord.flow_run_id == run.id,
                    FlowEvidenceItemRecord.digest.in_(list(digests)),
                )
            )
        }
        verified: dict[str, bool] = {}  # one check per distinct cited row within this call
        facts: dict[tuple[str, str], list[Any]] = {}
        if bulk:
            facts[("preloaded", str(run.flow_evidence_run_id))] = [
                self._preload(session, run.flow_evidence_run_id)
            ]
        for d in sorted(digests):
            r = items.get(d)
            if r is None:
                failures.append(f"EVIDENCE_MISSING:{d[:12]}")
                continue
            item = FlowEvidenceItem(r.kind, tuple(r.refs), dict(r.derived))
            if item.digest != d:
                failures.append(f"EVIDENCE_DIGEST_MISMATCH:{d[:12]}")
                continue
            for ref in item.refs:
                memo = json.dumps(ref, sort_keys=True, default=str)
                if memo not in verified:
                    verified[memo] = self._row_verifies(session, ref, facts)
                if not verified[memo]:
                    failures.append(f"CITED_ROW_CHANGED:{ref.get('table')}:{d[:12]}")
        return failures

    @staticmethod
    def _preload(session: Session, evidence_run_id: UUID) -> dict[str, dict[Any, Any]]:
        """Digests of one evidence run's callsites, JSX bindings and (recomputed) scopes."""
        calls: dict[tuple[UUID, str], list[FlowCallsite]] = defaultdict(list)
        sites: dict[Any, str] = {}
        for c in session.scalars(
            select(FlowCallsiteRecord)
            .where(FlowCallsiteRecord.flow_evidence_run_id == evidence_run_id)
            .order_by(FlowCallsiteRecord.ordinal)
        ):
            value = _callsite(c)
            calls[(c.artifact_analysis_id, c.scope_key)].append(value)
            sites[(c.artifact_analysis_id, c.scope_key, c.ordinal)] = callsite_digest(value)
        bindings: dict[tuple[UUID, str], list[FlowJsxBinding]] = defaultdict(list)
        jsx: dict[Any, str] = {}
        for j in session.scalars(
            select(FlowJsxBindingRecord)
            .where(FlowJsxBindingRecord.flow_evidence_run_id == evidence_run_id)
            .order_by(FlowJsxBindingRecord.ordinal)
        ):
            value_j = _jsx(j)
            bindings[(j.artifact_analysis_id, j.scope_key)].append(value_j)
            jsx[(j.artifact_analysis_id, j.scope_key, j.ordinal)] = jsx_digest(value_j)
        scopes: dict[Any, str | None] = {}
        for r in session.scalars(
            select(FlowScopeRecord).where(FlowScopeRecord.flow_evidence_run_id == evidence_run_id)
        ):
            owner = (r.artifact_analysis_id, r.scope_key)
            computed = _scope(r, calls.get(owner, []), bindings.get(owner, [])).digest
            scopes[owner] = computed if computed == r.scope_digest else None
        return {"flow_callsites": sites, "flow_jsx_bindings": jsx, "flow_scopes": scopes}

    def _row_verifies(
        self, session: Session, ref: dict[str, Any], facts: dict[tuple[str, str], list[Any]]
    ) -> bool:
        table, ids, expected = ref.get("table"), dict(ref.get("ids") or {}), ref.get("row_digest")
        preloaded = facts.get(("preloaded", str(ids.get("run"))))
        if preloaded and table in {"flow_callsites", "flow_jsx_bindings", "flow_scopes"}:
            rows = preloaded[0][str(table)]
            if table == "flow_scopes":
                found = rows.get((UUID(ids["artifact_analysis"]), ids["scope"]))
            else:
                found = rows.get(
                    (UUID(ids["artifact_analysis"]), ids["scope"], int(ids["ordinal"]))
                )
            return found is not None and found == expected
        if table == "flow_callsites":
            r = session.get(
                FlowCallsiteRecord,
                (
                    UUID(ids["run"]),
                    UUID(ids["artifact_analysis"]),
                    ids["scope"],
                    int(ids["ordinal"]),
                ),
            )
            return r is not None and callsite_digest(_callsite(r)) == expected
        if table == "flow_jsx_bindings":
            j = session.get(
                FlowJsxBindingRecord,
                (
                    UUID(ids["run"]),
                    UUID(ids["artifact_analysis"]),
                    ids["scope"],
                    int(ids["ordinal"]),
                ),
            )
            return j is not None and jsx_digest(_jsx(j)) == expected
        if table == "flow_scopes":
            s = session.get(
                FlowScopeRecord, (UUID(ids["run"]), UUID(ids["artifact_analysis"]), ids["scope"])
            )
            if s is None:
                return False
            calls = session.scalars(
                select(FlowCallsiteRecord)
                .where(
                    FlowCallsiteRecord.flow_evidence_run_id == s.flow_evidence_run_id,
                    FlowCallsiteRecord.artifact_analysis_id == s.artifact_analysis_id,
                    FlowCallsiteRecord.scope_key == s.scope_key,
                )
                .order_by(FlowCallsiteRecord.ordinal)
            ).all()
            jsx = session.scalars(
                select(FlowJsxBindingRecord)
                .where(
                    FlowJsxBindingRecord.flow_evidence_run_id == s.flow_evidence_run_id,
                    FlowJsxBindingRecord.artifact_analysis_id == s.artifact_analysis_id,
                    FlowJsxBindingRecord.scope_key == s.scope_key,
                )
                .order_by(FlowJsxBindingRecord.ordinal)
            ).all()
            scope = _scope(s, [_callsite(c) for c in calls], [_jsx(j) for j in jsx])
            return bool(scope.digest == expected == s.scope_digest)
        if table == "architecture_nodes":
            n = session.get(ArchitectureNodeRecord, (UUID(ids["run"]), ids["key"]))
            return n is not None and n.node_digest == expected
        if table == "architecture_edges":
            e = session.scalar(
                select(ArchitectureEdgeRecord).where(
                    ArchitectureEdgeRecord.architecture_run_id == UUID(ids["run"]),
                    ArchitectureEdgeRecord.edge_key == ids["key"],
                )
            )
            return e is not None and e.edge_digest == expected
        if table == "import_references":
            i = session.scalar(
                select(ImportReferenceRecord).where(
                    ImportReferenceRecord.artifact_analysis_id == UUID(ids["artifact_analysis"]),
                    ImportReferenceRecord.statement_ordinal == int(ids["statement"]),
                    ImportReferenceRecord.item_ordinal == int(ids["item"]),
                )
            )
            if i is None:
                return False
            locator = (
                session.scalar(
                    select(ArtifactAnalysisRecord.locator).where(
                        ArtifactAnalysisRecord.id == i.artifact_analysis_id
                    )
                )
                or ""
            )
            return bool(
                ImportInput(
                    i.artifact_analysis_id,
                    locator,
                    i.owner_symbol_id,
                    i.form,
                    i.from_module,
                    i.name,
                    i.alias,
                    i.level,
                    i.statement_ordinal,
                    i.item_ordinal,
                ).row_digest
                == expected
            )
        if table in {"flow_framework_facts", "flow_module_facts"}:
            run_id = UUID(ids["run"])
            if (table, ids["run"]) not in facts:
                facts[(table, ids["run"])] = list(
                    self._evidence.framework_facts(run_id)
                    if table == "flow_framework_facts"
                    else self._evidence.module_facts(run_id)
                )
            return any(
                f.locator == ids["locator"] and fact_row_digest(f) == expected
                for f in facts[(table, ids["run"])]
            )
        return False

    def integrity(self, run: FlowRun) -> dict[str, Any]:
        """Full, non-authoritative inspection: every element, every evidence item, the graph digest."""  # noqa: E501
        with self._session_factory() as session:
            node_rows = session.scalars(
                select(FlowNodeRecord).where(FlowNodeRecord.flow_run_id == run.id)
            ).all()
            edge_rows = session.scalars(
                select(FlowEdgeRecord).where(FlowEdgeRecord.flow_run_id == run.id)
            ).all()
            nodes = [_node(r) for r in node_rows]
            edges = [_edge(r) for r in edge_rows]
            stored_nodes = {r.node_key: r.node_digest for r in node_rows}
            stored_edges = {r.edge_key: r.edge_digest for r in edge_rows}
            items = {
                r.digest: FlowEvidenceItem(r.kind, tuple(r.refs), dict(r.derived))
                for r in session.scalars(
                    select(FlowEvidenceItemRecord).where(
                        FlowEvidenceItemRecord.flow_run_id == run.id
                    )
                )
            }
            failures = [
                f"NODE_DIGEST_MISMATCH:{n.key}"
                for n in nodes
                if n.element_digest() != stored_nodes[n.key]
            ]
            failures += [
                f"EDGE_DIGEST_MISMATCH:{e.key}"
                for e in edges
                if e.element_digest() != stored_edges[e.key]
            ]
            failures += self._verify_evidence(
                session,
                run,
                set(items)
                | {d for n in nodes for d in n.evidence}
                | {d for e in edges for d in e.evidence},
                bulk=True,
            )
            diagnostics = [
                FlowDiagnostic(r.code, r.subject, dict(r.attributes))
                for r in session.scalars(
                    select(FlowDiagnosticRecord).where(FlowDiagnosticRecord.flow_run_id == run.id)
                )
            ]
        if (
            not failures
            and graph_digest_of(tuple(nodes), tuple(edges), tuple(diagnostics), items)
            != run.graph_digest
        ):
            failures.append("GRAPH_DIGEST_MISMATCH")
        return {
            "nodes": len(nodes),
            "edges": len(edges),
            "evidence": len(items),
            "failures": failures[:200],
            "failure_count": len(failures),
            "verified": not failures,
        }


__all__ = ["FlowIntegrityError", "SqlAlchemyFlowStore"]
