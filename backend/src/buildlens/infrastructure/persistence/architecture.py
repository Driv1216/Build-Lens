"""PostgreSQL persistence for immutable Stage 14 architecture runs.

Reads only PostgreSQL. Publication follows the catalog pattern (snapshot ``FOR UPDATE``,
captured authority, ``superseded`` flag, retained failures). Every element row stores its
digest, and evidence links are rows with same-run composite foreign keys.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, insert, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from buildlens.application.architecture import ArchitectureConflictError, StoredGraph
from buildlens.application.architecture_builder import (
    ArchitectureInput,
    architecture_input,
    artifact_row_digest,
    export_row_digest,
    import_row_digest,
)
from buildlens.domain.architecture import (
    EVIDENCE_RENDERER,
    ArchitectureBuild,
    ArchitectureComponent,
    ArchitectureDiagnostic,
    ArchitectureEdge,
    ArchitectureEvidence,
    ArchitectureNode,
    ArchitectureRun,
    EvidenceLink,
    component_digest,
    diagnostic_digest,
    edge_digest,
    evidence_digest,
    node_digest,
)
from buildlens.domain.semantic import Row, SemanticCatalogRun
from buildlens.domain.source import digest
from buildlens.infrastructure.persistence.models import (
    ArchitectureComponentMemberRecord,
    ArchitectureComponentRecord,
    ArchitectureDiagnosticRecord,
    ArchitectureDiagnosticSubjectRecord,
    ArchitectureEdgeRecord,
    ArchitectureEvidenceLinkRecord,
    ArchitectureEvidenceRecord,
    ArchitectureNodeRecord,
    ArchitectureRunRecord,
    ArtifactAnalysisRecord,
    ArtifactExtractionRecord,
    ArtifactRecord,
    ExportReferenceRecord,
    ImportReferenceRecord,
    ProjectVersionRecord,
    SemanticCatalogRunRecord,
    SemanticEntityRecord,
    SemanticFactRecord,
    SemanticRelationRecord,
    StructuralAnalysisRecord,
    SymbolRecord,
)
from buildlens.infrastructure.persistence.semantic import (
    SqlAlchemySemanticCatalogStore,
    _analysis_row,
    _entity,
    _export_row,
    _extraction_row,
    _import_row,
    _relation,
)

_BATCH = 1000


def _run(x: ArchitectureRunRecord) -> ArchitectureRun:
    return ArchitectureRun(
        x.id,
        x.project_id,
        x.project_version_id,
        x.revision,
        x.started_at,
        x.completed_at,
        cast(Any, x.outcome),
        x.catalog_run_id,
        x.catalog_digest,
        x.structural_analysis_id,
        x.structural_revision,
        dict(x.architecture_contract),
        x.input_authority_digest,
        x.graph_digest,
        x.architecture_digest,
        x.portable_digest,
        dict(x.coverage),
        tuple(x.diagnostics),
        x.superseded,
        x.quarantined,
        x.captured_active_architecture_run_id,
        x.captured_latest_revision,
    )


def _artifact_inventory_row(x: ArtifactRecord) -> Row:
    return {
        "id": str(x.id),
        "locator": x.locator,
        "language": x.language,
        "content_digest": x.content_digest,
        "stable_identity_id": str(x.stable_identity_id) if x.stable_identity_id else None,
    }


def _links(records: Iterable[ArchitectureEvidenceLinkRecord]) -> tuple[EvidenceLink, ...]:
    values = [
        EvidenceLink("catalog", record.catalog_fact_digest)
        if record.catalog_fact_digest
        else EvidenceLink("architecture", str(record.evidence_digest))
        for record in records
    ]
    return tuple(sorted(values, key=lambda link: (link.source, link.digest)))


class SqlAlchemyArchitectureStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._catalog = SqlAlchemySemanticCatalogStore(session_factory)

    # ----------------------------------------------------------------- input (DB only)

    def load_input(self, catalog: SemanticCatalogRun) -> ArchitectureInput:
        structural_id = catalog.structural_analysis_id
        with self._session_factory() as session:
            structure = session.get(StructuralAnalysisRecord, structural_id)
            if structure is None:
                raise ArchitectureConflictError("The catalog's structural run disappeared.")
            inventory = [
                _artifact_inventory_row(record)
                for record in session.scalars(
                    select(ArtifactRecord)
                    .where(
                        ArtifactRecord.project_version_id == catalog.snapshot_id,
                        ArtifactRecord.kind == "file",
                        ArtifactRecord.inventory_status == "included",
                    )
                    .order_by(ArtifactRecord.locator)
                )
            ]
            analyses = [
                _analysis_row(record)
                for record in session.scalars(
                    select(ArtifactAnalysisRecord)
                    .where(ArtifactAnalysisRecord.structural_analysis_id == structural_id)
                    .order_by(ArtifactAnalysisRecord.locator)
                )
            ]
            owned = select(ArtifactAnalysisRecord.id).where(
                ArtifactAnalysisRecord.structural_analysis_id == structural_id
            )

            def grouped(model: Any, normaliser: Any, *order: Any) -> dict[str, list[Row]]:
                values: dict[str, list[Row]] = defaultdict(list)
                for record in session.scalars(
                    select(model).where(model.artifact_analysis_id.in_(owned)).order_by(*order)
                ):
                    values[str(record.artifact_analysis_id)].append(normaliser(record))
                return values

            imports = grouped(
                ImportReferenceRecord,
                _import_row,
                ImportReferenceRecord.statement_ordinal,
                ImportReferenceRecord.item_ordinal,
            )
            exports = grouped(ExportReferenceRecord, _export_row, ExportReferenceRecord.ordinal)
            extractions = grouped(
                ArtifactExtractionRecord, _extraction_row, ArtifactExtractionRecord.extractor
            )
            owners = {
                str(identity): kind
                for identity, kind in session.execute(
                    select(SymbolRecord.id, SymbolRecord.kind).where(
                        SymbolRecord.id.in_(
                            select(ImportReferenceRecord.owner_symbol_id).where(
                                ImportReferenceRecord.artifact_analysis_id.in_(owned),
                                ImportReferenceRecord.owner_symbol_id.is_not(None),
                            )
                        )
                    )
                )
            }
            entities = [
                _entity(record)
                for record in session.scalars(
                    select(SemanticEntityRecord).where(
                        SemanticEntityRecord.catalog_run_id == catalog.id,
                        SemanticEntityRecord.kind == "file",
                    )
                )
            ]
            relations = [
                _relation(record)
                for record in session.scalars(
                    select(SemanticRelationRecord)
                    .where(
                        SemanticRelationRecord.catalog_run_id == catalog.id,
                        SemanticRelationRecord.kind.in_(["imports", "exports"]),
                    )
                    .order_by(SemanticRelationRecord.ordinal)
                )
            ]
            counts = {
                fact_digest: int((derived or {}).get("count", 1))
                for fact_digest, derived in session.execute(
                    select(SemanticFactRecord.fact_digest, SemanticFactRecord.derived).where(
                        SemanticFactRecord.catalog_run_id == catalog.id,
                        SemanticFactRecord.fact_kind == "relation",
                    )
                )
            }
            return architecture_input(
                inventory=inventory,
                analyses=analyses,
                imports=imports,
                exports=exports,
                extractions=extractions,
                owner_kinds=owners,
                producer_digest=digest(structure.producer_contract),
                entities=entities,
                relations=relations,
                relation_counts=counts,
                catalog_outcome=catalog.outcome,
            )

    # ----------------------------------------------------------------- runs

    def _query_runs(self, project_id: UUID, snapshot_id: UUID) -> Any:
        return select(ArchitectureRunRecord).where(
            ArchitectureRunRecord.project_id == project_id,
            ArchitectureRunRecord.project_version_id == snapshot_id,
        )

    def latest(self, project_id: UUID, snapshot_id: UUID) -> ArchitectureRun | None:
        with self._session_factory() as session:
            record = session.scalar(
                self._query_runs(project_id, snapshot_id)
                .order_by(ArchitectureRunRecord.revision.desc())
                .limit(1)
            )
            return _run(record) if record else None

    def active(self, project_id: UUID, snapshot_id: UUID) -> ArchitectureRun | None:
        with self._session_factory() as session:
            version = session.get(ProjectVersionRecord, snapshot_id)
            if (
                version is None
                or version.project_id != project_id
                or version.active_architecture_run_id is None
            ):
                return None
            record = session.get(ArchitectureRunRecord, version.active_architecture_run_id)
            return _run(record) if record else None

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> ArchitectureRun | None:
        with self._session_factory() as session:
            record = session.scalar(
                self._query_runs(project_id, snapshot_id).where(ArchitectureRunRecord.id == run_id)
            )
            return _run(record) if record else None

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[ArchitectureRun], int]:
        with self._session_factory() as session:
            total = session.scalar(
                select(func.count())
                .select_from(ArchitectureRunRecord)
                .where(
                    ArchitectureRunRecord.project_id == project_id,
                    ArchitectureRunRecord.project_version_id == snapshot_id,
                )
            )
            records = session.scalars(
                self._query_runs(project_id, snapshot_id)
                .order_by(ArchitectureRunRecord.revision.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return [_run(record) for record in records], int(total or 0)

    # ----------------------------------------------------------------- publication

    def _lock(self, session: Session, run: ArchitectureRun) -> tuple[ProjectVersionRecord, int]:
        version = session.scalar(
            select(ProjectVersionRecord)
            .where(
                ProjectVersionRecord.id == run.snapshot_id,
                ProjectVersionRecord.project_id == run.project_id,
            )
            .with_for_update()
        )
        if version is None:
            raise ArchitectureConflictError("Snapshot disappeared before publication.")
        latest = session.scalar(
            select(func.max(ArchitectureRunRecord.revision)).where(
                ArchitectureRunRecord.project_version_id == run.snapshot_id
            )
        )
        return version, int(latest or 0)

    @staticmethod
    def _record(run: ArchitectureRun, revision: int, superseded: bool) -> ArchitectureRunRecord:
        return ArchitectureRunRecord(
            id=run.id,
            project_id=run.project_id,
            project_version_id=run.snapshot_id,
            revision=revision,
            started_at=run.started_at,
            completed_at=run.completed_at,
            outcome=run.outcome,
            catalog_run_id=run.catalog_run_id,
            catalog_digest=run.catalog_digest,
            structural_analysis_id=run.structural_analysis_id,
            structural_revision=run.structural_revision,
            architecture_contract=run.architecture_contract,
            contract_digest=digest(run.architecture_contract),
            input_authority_digest=run.input_authority_digest,
            graph_digest=run.graph_digest,
            architecture_digest=run.architecture_digest,
            portable_digest=run.portable_digest,
            coverage=run.coverage,
            diagnostics=list(run.diagnostics),
            superseded=superseded,
            quarantined=False,
            captured_active_architecture_run_id=run.captured_active_architecture_run_id,
            captured_latest_revision=run.captured_latest_revision,
        )

    def publish(self, run: ArchitectureRun, build: ArchitectureBuild) -> ArchitectureRun:
        with self._session_factory.begin() as session:
            version, latest = self._lock(session, run)
            catalog = session.get(SemanticCatalogRunRecord, run.catalog_run_id)
            if (
                catalog is None
                or catalog.quarantined
                or version.active_semantic_catalog_run_id != run.catalog_run_id
                or version.active_analysis_id != run.structural_analysis_id
                or catalog.catalog_digest != run.catalog_digest
            ):
                raise ArchitectureConflictError("Architecture input authority changed.")
            superseded = (
                latest != run.captured_latest_revision
                or version.active_architecture_run_id != run.captured_active_architecture_run_id
            )
            record = self._record(run, latest + 1, superseded)
            session.add(record)
            session.flush()
            self._insert(session, run, build)
            if not superseded and run.outcome != "failed":
                version.active_architecture_run_id = run.id
            return _run(record)

    def save_failure(self, run: ArchitectureRun, code: str) -> ArchitectureRun:
        with self._session_factory.begin() as session:
            _, latest = self._lock(session, run)
            failed = replace(
                run,
                outcome="failed",
                graph_digest=None,
                architecture_digest=None,
                portable_digest=None,
                diagnostics=(code,),
            )
            record = self._record(failed, latest + 1, False)
            session.add(record)
            session.flush()
            return _run(record)

    @staticmethod
    def _rows(session: Session, model: Any, rows: list[dict[str, Any]]) -> None:
        for start in range(0, len(rows), _BATCH):
            session.execute(insert(model), rows[start : start + _BATCH])

    def _insert(self, session: Session, run: ArchitectureRun, build: ArchitectureBuild) -> None:
        run_id = run.id
        self._rows(
            session,
            ArchitectureEvidenceRecord,
            [
                {
                    "architecture_run_id": run_id,
                    "digest": item.digest,
                    "kind": item.kind,
                    "source_refs": list(item.source_refs),
                    "derived": item.derived,
                    "renderer_version": item.renderer_version,
                    "rendered_text": item.rendered_text,
                }
                for item in build.evidence
            ],
        )
        self._rows(
            session,
            ArchitectureNodeRecord,
            [
                {
                    "architecture_run_id": run_id,
                    "node_key": node.key,
                    "ordinal": node.ordinal,
                    "kind": node.kind,
                    "parent_key": node.parent_key,
                    "name": node.name,
                    "locator": node.locator,
                    "language": node.language,
                    "catalog_entity_key": node.catalog_entity_key,
                    "artifact_identity_id": node.artifact_identity_id,
                    "coverage_state": node.coverage_state,
                    "attributes": node.attributes,
                    "metrics": node.metrics,
                    "node_digest": node_digest(node),
                }
                for node in build.nodes
            ],
        )
        self._rows(
            session,
            ArchitectureEdgeRecord,
            [
                {
                    "architecture_run_id": run_id,
                    "ordinal": edge.ordinal,
                    "edge_key": edge.key,
                    "kind": edge.kind,
                    "source_key": edge.source_key,
                    "target_key": edge.target_key,
                    "target_text": edge.target_text,
                    "tier": edge.tier,
                    "resolution_status": edge.resolution_status,
                    "import_class": edge.import_class,
                    "count": edge.count,
                    "attributes": edge.attributes,
                    "conditions": list(edge.conditions),
                    "edge_digest": edge_digest(edge),
                }
                for edge in build.edges
            ],
        )
        self._rows(
            session,
            ArchitectureComponentRecord,
            [
                {
                    "architecture_run_id": run_id,
                    "component_key": item.key,
                    "ordinal": item.ordinal,
                    "level": item.level,
                    "class_set": item.class_set,
                    "scope_key": item.scope_key,
                    "witness": list(item.witness),
                    "attributes": item.attributes,
                    "component_digest": component_digest(item),
                }
                for item in build.components
            ],
        )
        self._rows(
            session,
            ArchitectureComponentMemberRecord,
            [
                {"architecture_run_id": run_id, "component_key": item.key, "node_key": member}
                for item in build.components
                for member in item.members
            ],
        )
        self._rows(
            session,
            ArchitectureDiagnosticRecord,
            [
                {
                    "architecture_run_id": run_id,
                    "ordinal": item.ordinal,
                    "code": item.code,
                    "category": item.category,
                    "tier": item.tier,
                    "attributes": item.attributes,
                    "diagnostic_digest": diagnostic_digest(item),
                }
                for item in build.diagnostics
            ],
        )
        self._rows(
            session,
            ArchitectureDiagnosticSubjectRecord,
            [
                {"architecture_run_id": run_id, "diagnostic_ordinal": item.ordinal, "node_key": key}
                for item in build.diagnostics
                for key in item.subjects
            ],
        )
        links: list[dict[str, Any]] = []

        def add(owner: dict[str, Any], values: Iterable[EvidenceLink]) -> None:
            for link in values:
                catalog = link.source == "catalog"
                links.append(
                    {
                        "architecture_run_id": run_id,
                        "ordinal": len(links),
                        "node_key": None,
                        "edge_ordinal": None,
                        "component_key": None,
                        "diagnostic_ordinal": None,
                        **owner,
                        "evidence_digest": None if catalog else link.digest,
                        "catalog_run_id": run.catalog_run_id if catalog else None,
                        "catalog_fact_digest": link.digest if catalog else None,
                    }
                )

        for node in build.nodes:
            add({"node_key": node.key}, node.evidence)
        for edge in build.edges:
            add({"edge_ordinal": edge.ordinal}, edge.evidence)
        for component in build.components:
            add({"component_key": component.key}, component.evidence)
        for diagnostic in build.diagnostics:
            add({"diagnostic_ordinal": diagnostic.ordinal}, diagnostic.evidence)
        self._rows(session, ArchitectureEvidenceLinkRecord, links)

    # ----------------------------------------------------------------- graph reads

    def _graph(
        self,
        session: Session,
        run_id: UUID,
        *,
        nodes: Sequence[str] | None = None,
        edges: Sequence[int] | None = None,
        components: Sequence[str] | None = None,
        diagnostics: Sequence[int] | None = None,
    ) -> StoredGraph:
        def scoped(model: Any, column: Any, values: Sequence[Any] | None) -> Any:
            query = select(model).where(model.architecture_run_id == run_id)
            return query if values is None else query.where(column.in_(list(values)))

        everything = all(value is None for value in (nodes, edges, components, diagnostics))
        wanted = (lambda value: value) if everything else (lambda value: value or [])
        node_rows = session.scalars(
            scoped(ArchitectureNodeRecord, ArchitectureNodeRecord.node_key, wanted(nodes)).order_by(
                ArchitectureNodeRecord.ordinal
            )
        ).all()
        edge_rows = session.scalars(
            scoped(ArchitectureEdgeRecord, ArchitectureEdgeRecord.ordinal, wanted(edges)).order_by(
                ArchitectureEdgeRecord.ordinal
            )
        ).all()
        component_rows = session.scalars(
            scoped(
                ArchitectureComponentRecord,
                ArchitectureComponentRecord.component_key,
                wanted(components),
            ).order_by(ArchitectureComponentRecord.ordinal)
        ).all()
        diagnostic_rows = session.scalars(
            scoped(
                ArchitectureDiagnosticRecord,
                ArchitectureDiagnosticRecord.ordinal,
                wanted(diagnostics),
            ).order_by(ArchitectureDiagnosticRecord.ordinal)
        ).all()
        link_query = select(ArchitectureEvidenceLinkRecord).where(
            ArchitectureEvidenceLinkRecord.architecture_run_id == run_id
        )
        node_links: dict[str, list[ArchitectureEvidenceLinkRecord]] = defaultdict(list)
        edge_links: dict[int, list[ArchitectureEvidenceLinkRecord]] = defaultdict(list)
        component_links: dict[str, list[ArchitectureEvidenceLinkRecord]] = defaultdict(list)
        diagnostic_links: dict[int, list[ArchitectureEvidenceLinkRecord]] = defaultdict(list)
        if not everything:
            link_query = link_query.where(
                or_(
                    ArchitectureEvidenceLinkRecord.node_key.in_([r.node_key for r in node_rows]),
                    ArchitectureEvidenceLinkRecord.edge_ordinal.in_([r.ordinal for r in edge_rows]),
                    ArchitectureEvidenceLinkRecord.component_key.in_(
                        [r.component_key for r in component_rows]
                    ),
                    ArchitectureEvidenceLinkRecord.diagnostic_ordinal.in_(
                        [r.ordinal for r in diagnostic_rows]
                    ),
                )
            )
        for link in session.scalars(link_query):
            if link.node_key is not None:
                node_links[link.node_key].append(link)
            elif link.edge_ordinal is not None:
                edge_links[link.edge_ordinal].append(link)
            elif link.component_key is not None:
                component_links[link.component_key].append(link)
            elif link.diagnostic_ordinal is not None:
                diagnostic_links[link.diagnostic_ordinal].append(link)
        members: dict[str, list[str]] = defaultdict(list)
        member_query = select(
            ArchitectureComponentMemberRecord.component_key,
            ArchitectureComponentMemberRecord.node_key,
        ).where(ArchitectureComponentMemberRecord.architecture_run_id == run_id)
        if not everything:
            member_query = member_query.where(
                ArchitectureComponentMemberRecord.component_key.in_(
                    [r.component_key for r in component_rows]
                )
            )
        for key, node in session.execute(member_query):
            members[key].append(node)
        subjects: dict[int, list[str]] = defaultdict(list)
        subject_query = select(
            ArchitectureDiagnosticSubjectRecord.diagnostic_ordinal,
            ArchitectureDiagnosticSubjectRecord.node_key,
        ).where(ArchitectureDiagnosticSubjectRecord.architecture_run_id == run_id)
        if not everything:
            subject_query = subject_query.where(
                ArchitectureDiagnosticSubjectRecord.diagnostic_ordinal.in_(
                    [r.ordinal for r in diagnostic_rows]
                )
            )
        for ordinal, node in session.execute(subject_query):
            subjects[ordinal].append(node)
        return StoredGraph(
            tuple(
                ArchitectureNode(
                    r.node_key,
                    cast(Any, r.kind),
                    r.parent_key,
                    r.name,
                    r.locator,
                    r.language,
                    r.catalog_entity_key,
                    r.artifact_identity_id,
                    cast(Any, r.coverage_state),
                    dict(r.attributes),
                    dict(r.metrics),
                    _links(node_links[r.node_key]),
                    r.ordinal,
                )
                for r in node_rows
            ),
            tuple(r.node_digest for r in node_rows),
            tuple(
                ArchitectureEdge(
                    r.ordinal,
                    r.edge_key,
                    cast(Any, r.kind),
                    r.source_key,
                    r.target_key,
                    r.target_text,
                    cast(Any, r.tier),
                    cast(Any, r.resolution_status),
                    cast(Any, r.import_class),
                    r.count,
                    dict(r.attributes),
                    tuple(r.conditions),
                    _links(edge_links[r.ordinal]),
                )
                for r in edge_rows
            ),
            tuple(r.edge_digest for r in edge_rows),
            tuple(
                ArchitectureComponent(
                    r.component_key,
                    r.ordinal,
                    cast(Any, r.level),
                    cast(Any, r.class_set),
                    r.scope_key,
                    tuple(sorted(members[r.component_key])),
                    tuple(r.witness),
                    dict(r.attributes),
                    _links(component_links[r.component_key]),
                )
                for r in component_rows
            ),
            tuple(r.component_digest for r in component_rows),
            tuple(
                ArchitectureDiagnostic(
                    r.ordinal,
                    r.code,
                    r.category,
                    cast(Any, r.tier),
                    tuple(sorted(subjects[r.ordinal])),
                    dict(r.attributes),
                    _links(diagnostic_links[r.ordinal]),
                )
                for r in diagnostic_rows
            ),
            tuple(r.diagnostic_digest for r in diagnostic_rows),
        )

    _FINGERPRINT = text(
        """
        SELECT
    (SELECT count(*) || ':' || coalesce(md5(string_agg(node_digest, ',' ORDER BY ordinal)), '')
       FROM architecture_nodes WHERE architecture_run_id = :run),
    (SELECT count(*) || ':' || coalesce(md5(string_agg(edge_digest, ',' ORDER BY ordinal)), '')
       FROM architecture_edges WHERE architecture_run_id = :run),
    (SELECT count(*) || ':' ||
            coalesce(md5(string_agg(component_digest, ',' ORDER BY ordinal)), '')
       FROM architecture_components WHERE architecture_run_id = :run),
    (SELECT count(*) || ':' ||
            coalesce(md5(string_agg(diagnostic_digest, ',' ORDER BY ordinal)), '')
       FROM architecture_diagnostics WHERE architecture_run_id = :run),
    (SELECT count(*) || ':' || coalesce(md5(string_agg(concat_ws('|', ordinal, node_key,
            edge_ordinal, component_key, diagnostic_ordinal, evidence_digest,
            catalog_fact_digest), ',' ORDER BY ordinal)), '')
       FROM architecture_evidence_links WHERE architecture_run_id = :run),
    (SELECT count(*) || ':' || coalesce(md5(string_agg(digest, ',' ORDER BY digest)), '')
       FROM architecture_evidence WHERE architecture_run_id = :run),
    (SELECT count(*) FROM architecture_component_members WHERE architecture_run_id = :run),
    (SELECT count(*) FROM architecture_diagnostic_subjects
       WHERE architecture_run_id = :run)
        """
    )

    def _fingerprint(self, session: Session, run_id: UUID) -> str:
        values = session.execute(self._FINGERPRINT, {"run": run_id}).one()
        return hashlib.sha256("|".join(str(value) for value in values).encode()).hexdigest()

    def fingerprint(self, run_id: UUID) -> str:
        with self._session_factory() as session:
            return self._fingerprint(session, run_id)

    def load_graph(self, run_id: UUID) -> tuple[StoredGraph, str]:
        """All elements and the fingerprint, read in one repeatable-read snapshot."""
        with self._session_factory() as session:
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            stored = self._graph(session, run_id)
            return stored, self._fingerprint(session, run_id)

    def reload(
        self,
        run_id: UUID,
        *,
        nodes: Iterable[str] = (),
        edges: Iterable[int] = (),
        components: Iterable[str] = (),
        diagnostics: Iterable[int] = (),
    ) -> StoredGraph:
        with self._session_factory() as session:
            return self._graph(
                session,
                run_id,
                nodes=list(nodes),
                edges=list(edges),
                components=list(components),
                diagnostics=list(diagnostics),
            )

    def evidence(self, run_id: UUID, digests: Sequence[str]) -> list[ArchitectureEvidence]:
        if not digests:
            return []
        with self._session_factory() as session:
            return [
                ArchitectureEvidence(
                    r.digest,
                    cast(Any, r.kind),
                    tuple(r.source_refs),
                    dict(r.derived),
                    r.renderer_version,
                    r.rendered_text,
                )
                for r in session.scalars(
                    select(ArchitectureEvidenceRecord)
                    .where(
                        ArchitectureEvidenceRecord.architecture_run_id == run_id,
                        ArchitectureEvidenceRecord.digest.in_(list(digests)),
                    )
                    .order_by(ArchitectureEvidenceRecord.digest)
                )
            ]

    # ----------------------------------------------------------------- evidence verification

    def verify_evidence(
        self, run: ArchitectureRun, links: Sequence[EvidenceLink]
    ) -> dict[EvidenceLink, str]:
        results: dict[EvidenceLink, str] = {}
        catalog = [link for link in links if link.source == "catalog"]
        if catalog:
            facts = self._catalog.facts(run.catalog_run_id, [link.digest for link in catalog])
            statuses = self._catalog.verify_facts(facts)
            for link in catalog:
                results[link] = statuses.get(link.digest, "FACT_SOURCE_MISSING")
        own = [link for link in links if link.source == "architecture"]
        if own:
            rows = {
                item.digest: item for item in self.evidence(run.id, [link.digest for link in own])
            }
            cited = self._cited_rows([ref for item in rows.values() for ref in item.source_refs])
            for link in own:
                item = rows.get(link.digest)
                results[link] = (
                    "EVIDENCE_MISSING" if item is None else self._verify_item(item, cited)
                )
        return results

    @staticmethod
    def _verify_item(
        item: ArchitectureEvidence, cited: dict[tuple[str, str], tuple[str, str]]
    ) -> str:
        if item.renderer_version != EVIDENCE_RENDERER:
            return "EVIDENCE_RENDERER_CHANGED"
        if (
            evidence_digest(
                item.renderer_version, item.rendered_text, list(item.source_refs), item.derived
            )
            != item.digest
        ):
            return "EVIDENCE_DIGEST_MISMATCH"
        for ref in item.source_refs:
            found = cited.get((str(ref.get("table")), str(ref.get("id"))))
            if found is None:
                return "EVIDENCE_SOURCE_MISSING"
            row_digest, locator = found
            if row_digest != ref.get("row_digest") or locator != ref.get("locator"):
                return "EVIDENCE_SOURCE_CHANGED"
        return "verified"

    def _cited_rows(self, refs: Sequence[Row]) -> dict[tuple[str, str], tuple[str, str]]:
        """(table, id) -> (row digest, locator), recomputed with the builder's normalisers."""
        wanted: dict[str, set[UUID]] = defaultdict(set)
        for ref in refs:
            if ref.get("id"):
                wanted[str(ref.get("table"))].add(UUID(str(ref["id"])))
        values: dict[tuple[str, str], tuple[str, str]] = {}
        with self._session_factory() as session:
            if wanted.get("artifacts"):
                for record in session.scalars(
                    select(ArtifactRecord).where(ArtifactRecord.id.in_(wanted["artifacts"]))
                ):
                    row = _artifact_inventory_row(record)
                    values[("artifacts", str(record.id))] = (
                        artifact_row_digest(row),
                        record.locator,
                    )
            if wanted.get("import_references"):
                for record, locator, owner_kind in session.execute(
                    select(ImportReferenceRecord, ArtifactAnalysisRecord.locator, SymbolRecord.kind)
                    .join(
                        ArtifactAnalysisRecord,
                        ArtifactAnalysisRecord.id == ImportReferenceRecord.artifact_analysis_id,
                    )
                    .outerjoin(
                        SymbolRecord, SymbolRecord.id == ImportReferenceRecord.owner_symbol_id
                    )
                    .where(ImportReferenceRecord.id.in_(wanted["import_references"]))
                ):
                    values[("import_references", str(record.id))] = (
                        import_row_digest(_import_row(record), owner_kind),
                        locator,
                    )
            if wanted.get("export_references"):
                for record, locator in session.execute(
                    select(ExportReferenceRecord, ArtifactAnalysisRecord.locator)
                    .join(
                        ArtifactAnalysisRecord,
                        ArtifactAnalysisRecord.id == ExportReferenceRecord.artifact_analysis_id,
                    )
                    .where(ExportReferenceRecord.id.in_(wanted["export_references"]))
                ):
                    values[("export_references", str(record.id))] = (
                        export_row_digest(_export_row(record)),
                        locator,
                    )
        return values
