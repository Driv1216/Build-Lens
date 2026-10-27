"""PostgreSQL persistence for the immutable Stage 13 semantic catalog.

The same row normalisers serve the catalog build and read-time ``F#`` verification, so a
fact re-rendered from its cited rows reproduces its digest exactly unless a row changed.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, insert, or_, select
from sqlalchemy.orm import Session, sessionmaker

from buildlens.application.fact_renderer import rendered_fact
from buildlens.application.processing import dependency_fingerprint, processing_freshness
from buildlens.application.semantic_catalog import (
    ArtifactInput,
    CatalogInput,
    EntityFilters,
    SemanticCatalogConflictError,
)
from buildlens.domain.processing import ProcessingRun
from buildlens.domain.semantic import (
    RENDERER_VERSION,
    CatalogBuild,
    EvidenceChunk,
    OmittedRegion,
    Row,
    SemanticCatalogRun,
    SemanticEntity,
    SemanticFact,
    SemanticRelation,
    catalog_contract,
)
from buildlens.domain.source import digest
from buildlens.domain.structural import StructuralAnalysis
from buildlens.infrastructure.persistence.models import (
    ArtifactAnalysisRecord,
    ArtifactExtractionRecord,
    ArtifactProcessingRecord,
    ArtifactRecord,
    ChunkRecord,
    ExportReferenceRecord,
    ImportReferenceRecord,
    ProcessingRunRecord,
    ProjectVersionRecord,
    SemanticCatalogRunRecord,
    SemanticEntityRecord,
    SemanticFactRecord,
    SemanticRelationRecord,
    StructuralAnalysisRecord,
    SymbolFactsRecord,
    SymbolRecord,
    SymbolReferenceRecord,
)
from buildlens.infrastructure.persistence.processing import structural_artifacts_in_session


def _id(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


# --------------------------------------------------------------------------- row normalisers


def _analysis_row(x: ArtifactAnalysisRecord) -> Row:
    return {
        "id": str(x.id),
        "artifact_id": str(x.artifact_id),
        "locator": x.locator,
        "outcome": x.outcome,
        "language": x.language,
        "line_count": x.line_count,
        "is_package_initializer": x.is_package_initializer,
        "declaration_extraction_id": _id(x.declaration_extraction_id),
        "diagnostic_codes": [str(item.get("code")) for item in x.diagnostics],
    }


def _symbol_row(x: SymbolRecord) -> Row:
    return {
        "id": str(x.id),
        "artifact_analysis_id": str(x.artifact_analysis_id),
        "parent_id": _id(x.parent_id),
        "ordinal": x.ordinal,
        "kind": x.kind,
        "name": x.name,
        "qualified_name": x.qualified_name,
        "is_async": x.is_async,
        "start_line": x.start_line,
        "end_line": x.end_line,
        "full_start_line": x.full_start_line,
        "start_offset": x.start_offset,
        "end_offset": x.end_offset,
        "additional_spans": x.additional_spans or [],
        "signature": x.signature,
        "decorators": x.decorators,
        "type_parameters": x.type_parameters,
        "stable_identity_id": _id(x.stable_identity_id),
        "declaration_form": x.declaration_form,
        "declaration_space": x.declaration_space,
        "name_source": x.name_source,
        "exported": x.exported,
    }


def _import_row(x: ImportReferenceRecord) -> Row:
    return {
        "id": str(x.id),
        "artifact_analysis_id": str(x.artifact_analysis_id),
        "owner_symbol_id": _id(x.owner_symbol_id),
        "statement_ordinal": x.statement_ordinal,
        "item_ordinal": x.item_ordinal,
        "form": x.form,
        "from_module": x.from_module,
        "name": x.name,
        "alias": x.alias,
        "level": x.level,
        "start_line": x.start_line,
        "end_line": x.end_line,
    }


def _extraction_row(x: ArtifactExtractionRecord) -> Row:
    return {
        "id": str(x.id),
        "artifact_analysis_id": str(x.artifact_analysis_id),
        "extractor": x.extractor,
        "outcome": x.outcome,
        "contract_digest": x.contract_digest,
        "artifact_facts": x.artifact_facts,
        "diagnostic_codes": [str(item.get("code")) for item in x.diagnostics],
    }


def _facts_row(x: SymbolFactsRecord) -> Row:
    return {
        "id": str(x.id),
        "extraction_id": str(x.extraction_id),
        "symbol_id": str(x.symbol_id),
        "facts": x.facts,
    }


def _reference_row(x: SymbolReferenceRecord) -> Row:
    return {
        "id": str(x.id),
        "extraction_id": str(x.extraction_id),
        "owner_symbol_id": _id(x.owner_symbol_id),
        "ordinal": x.ordinal,
        "kind": x.kind,
        "target": x.target,
        "start_line": x.start_line,
        "end_line": x.end_line,
    }


def _export_row(x: ExportReferenceRecord) -> Row:
    return {
        "id": str(x.id),
        "extraction_id": str(x.extraction_id),
        "owner_symbol_id": _id(x.owner_symbol_id),
        "ordinal": x.ordinal,
        "kind": x.kind,
        "name": x.name,
        "local_name": x.local_name,
        "source_module": x.source_module,
        "start_line": x.start_line,
        "end_line": x.end_line,
    }


def _artifact_row(x: ArtifactRecord) -> Row:
    return {
        "id": str(x.id),
        "locator": x.locator,
        "language": x.language,
        "stable_identity_id": _id(x.stable_identity_id),
    }


def _chunk_row(x: ChunkRecord) -> Row:
    return {
        "id": str(x.id),
        "stable_key": x.stable_key,
        "ordinal": x.ordinal,
        "content": x.content,
        "content_hash": x.content_hash,
        "start_offset": x.start_offset,
        "end_offset": x.end_offset,
        "start_line": x.start_line,
        "end_line": x.end_line,
        "start_column": x.start_column,
        "end_column": x.end_column,
        "symbol_identity_id": _id(x.symbol_identity_id),
    }


_TABLES: dict[str, tuple[Any, Any]] = {
    "artifact_analyses": (ArtifactAnalysisRecord, _analysis_row),
    "symbols": (SymbolRecord, _symbol_row),
    "import_references": (ImportReferenceRecord, _import_row),
    "artifact_extractions": (ArtifactExtractionRecord, _extraction_row),
    "symbol_facts": (SymbolFactsRecord, _facts_row),
    "symbol_references": (SymbolReferenceRecord, _reference_row),
    "export_references": (ExportReferenceRecord, _export_row),
    "artifacts": (ArtifactRecord, _artifact_row),
}


# --------------------------------------------------------------------------- domain mapping


def _run(x: SemanticCatalogRunRecord) -> SemanticCatalogRun:
    return SemanticCatalogRun(
        x.id,
        x.project_id,
        x.project_version_id,
        x.revision,
        x.started_at,
        x.completed_at,
        cast(Any, x.outcome),
        x.structural_analysis_id,
        x.structural_revision,
        x.processing_run_id,
        x.processing_revision,
        dict(x.processing_contract),
        dict(x.catalog_contract),
        x.input_authority_digest,
        x.catalog_digest,
        x.portable_digest,
        dict(x.coverage),
        tuple(x.diagnostics),
        x.superseded,
        x.quarantined,
        x.captured_active_catalog_run_id,
        x.captured_latest_revision,
    )


def _entity(x: SemanticEntityRecord) -> SemanticEntity:
    evidence = cast(dict[str, Any], x.evidence)
    package = cast(dict[str, Any], x.package)
    return SemanticEntity(
        x.entity_key,
        cast(Any, x.kind),
        x.language,
        x.locator,
        x.artifact_identity_id,
        x.symbol_identity_id,
        x.subject_entity_key,
        x.name,
        x.qualified_name,
        x.symbol_kind,
        x.start_line,
        x.end_line,
        x.ordinal,
        x.portable_key,
        cast(Any, x.stored_coverage),
        x.coverage_reason,
        cast(Any, x.context_coverage),
        x.context_reason,
        x.package_digest,
        x.package_bytes,
        tuple(x.limitations),
        tuple(
            EvidenceChunk(
                UUID(item[0]),
                item[1],
                item[2],
                item[3],
                item[4],
                item[5],
                item[6],
                UUID(item[7]) if item[7] else None,
            )
            for item in evidence.get("chunks", [])
        ),
        dict(evidence.get("gaps") or {}),
        tuple(package.get("chunks", [])),
        tuple(package.get("facts", [])),
        tuple(package.get("neighbors", [])),
        tuple(OmittedRegion(item[0], item[1], item[2]) for item in package.get("omitted", [])),
        x.content_digest,
    )


def _fact(x: SemanticFactRecord) -> SemanticFact:
    return SemanticFact(
        x.fact_digest,
        x.ordinal,
        x.subject_entity_key,
        cast(Any, x.fact_kind),
        tuple(x.source_refs),
        dict(x.derived),
        x.renderer_version,
        x.rendered_text,
    )


def _relation(x: SemanticRelationRecord) -> SemanticRelation:
    return SemanticRelation(
        x.ordinal,
        cast(Any, x.kind),
        x.source_entity_key,
        x.target_entity_key,
        x.target_text,
        cast(Any, x.resolution_status),
        x.fact_digest,
    )


def _run_record(
    run: SemanticCatalogRun, revision: int, superseded: bool
) -> SemanticCatalogRunRecord:
    return SemanticCatalogRunRecord(
        id=run.id,
        project_id=run.project_id,
        project_version_id=run.snapshot_id,
        revision=revision,
        started_at=run.started_at,
        completed_at=run.completed_at,
        outcome=run.outcome,
        structural_analysis_id=run.structural_analysis_id,
        structural_revision=run.structural_revision,
        processing_run_id=run.processing_run_id,
        processing_revision=run.processing_revision,
        processing_contract=run.processing_contract,
        catalog_contract=run.catalog_contract,
        contract_digest=digest(run.catalog_contract),
        input_authority_digest=run.input_authority_digest,
        catalog_digest=run.catalog_digest,
        portable_digest=run.portable_digest,
        coverage=run.coverage,
        diagnostics=list(run.diagnostics),
        superseded=superseded,
        quarantined=False,
        captured_active_catalog_run_id=run.captured_active_catalog_run_id,
        captured_latest_revision=run.captured_latest_revision,
    )


class SqlAlchemySemanticCatalogStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ----------------------------------------------------------------- input (DB only)

    def load_input(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        structural: StructuralAnalysis,
        processing: ProcessingRun,
    ) -> CatalogInput:
        with self._session_factory() as session:
            inventory_records = session.scalars(
                select(ArtifactRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ArtifactRecord.project_version_id,
                )
                .where(
                    ArtifactRecord.project_version_id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                    ArtifactRecord.kind == "file",
                    ArtifactRecord.inventory_status == "included",
                )
                .order_by(ArtifactRecord.locator)
            ).all()
            identities = {record.id: record.stable_identity_id for record in inventory_records}
            analyses = session.scalars(
                select(ArtifactAnalysisRecord)
                .where(ArtifactAnalysisRecord.structural_analysis_id == structural.id)
                .order_by(ArtifactAnalysisRecord.locator)
            ).all()
            owned = select(ArtifactAnalysisRecord.id).where(
                ArtifactAnalysisRecord.structural_analysis_id == structural.id
            )

            def grouped(model: Any, normaliser: Any, *order: Any) -> dict[str, list[Row]]:
                values: dict[str, list[Row]] = defaultdict(list)
                for record in session.scalars(
                    select(model).where(model.artifact_analysis_id.in_(owned)).order_by(*order)
                ):
                    values[str(record.artifact_analysis_id)].append(normaliser(record))
                return values

            symbols = grouped(SymbolRecord, _symbol_row, SymbolRecord.ordinal)
            imports = grouped(
                ImportReferenceRecord,
                _import_row,
                ImportReferenceRecord.statement_ordinal,
                ImportReferenceRecord.item_ordinal,
            )
            extractions = grouped(
                ArtifactExtractionRecord, _extraction_row, ArtifactExtractionRecord.extractor
            )
            facts = grouped(SymbolFactsRecord, _facts_row, SymbolFactsRecord.symbol_id)
            references = grouped(
                SymbolReferenceRecord,
                _reference_row,
                SymbolReferenceRecord.extraction_id,
                SymbolReferenceRecord.ordinal,
            )
            exports = grouped(
                ExportReferenceRecord,
                _export_row,
                ExportReferenceRecord.extraction_id,
                ExportReferenceRecord.ordinal,
            )
            processings = {
                record.artifact_id: record
                for record in session.scalars(
                    select(ArtifactProcessingRecord).where(
                        ArtifactProcessingRecord.processing_run_id == processing.id
                    )
                )
            }
            chunks: dict[UUID, list[Row]] = defaultdict(list)
            for record, artifact_id in session.execute(
                select(ChunkRecord, ArtifactProcessingRecord.artifact_id)
                .join(
                    ArtifactProcessingRecord,
                    ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
                )
                .where(ArtifactProcessingRecord.processing_run_id == processing.id)
                .order_by(ArtifactProcessingRecord.locator, ChunkRecord.ordinal)
            ):
                chunks[artifact_id].append(_chunk_row(record))
            observed = structural_artifacts_in_session(session, structural.id)
            artifacts: list[ArtifactInput] = []
            for record in analyses:
                key = str(record.id)
                processed = processings.get(record.artifact_id)
                current = observed.get(record.artifact_id)
                dependency_current = bool(
                    processed is not None
                    and current is not None
                    and current.outcome == "parsed"
                    and processed.dependency_fingerprint is not None
                    and processed.dependency_fingerprint == dependency_fingerprint(current)
                )
                identity = record.artifact_identity_id or identities.get(record.artifact_id)
                artifacts.append(
                    ArtifactInput(
                        _analysis_row(record),
                        _id(identity),
                        tuple(symbols.get(key, [])),
                        tuple(imports.get(key, [])),
                        tuple(extractions.get(key, [])),
                        tuple(facts.get(key, [])),
                        tuple(references.get(key, [])),
                        tuple(exports.get(key, [])),
                        {
                            "id": str(processed.id),
                            "outcome": processed.outcome,
                            "fingerprint_scheme": processed.fingerprint_scheme,
                        }
                        if processed is not None
                        else None,
                        tuple(chunks.get(record.artifact_id, [])),
                        dependency_current,
                    )
                )
            contract = catalog_contract()
            return CatalogInput(
                digest(structural.producer_contract),
                structural.outcome,
                processing.outcome,
                tuple(_artifact_row(record) for record in inventory_records),
                tuple(artifacts),
                cast(dict[str, str], contract["extractors"]),
            )

    # ----------------------------------------------------------------- runs

    def _selected(
        self, project_id: UUID, snapshot_id: UUID, active: bool
    ) -> SemanticCatalogRun | None:
        with self._session_factory() as session:
            query = (
                select(SemanticCatalogRunRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == SemanticCatalogRunRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    SemanticCatalogRunRecord.project_version_id == snapshot_id,
                )
            )
            if active:
                query = query.where(
                    ProjectVersionRecord.active_semantic_catalog_run_id
                    == SemanticCatalogRunRecord.id
                )
            else:
                query = query.order_by(SemanticCatalogRunRecord.revision.desc()).limit(1)
            record = session.scalar(query)
            return _run(record) if record else None

    def latest(self, project_id: UUID, snapshot_id: UUID) -> SemanticCatalogRun | None:
        return self._selected(project_id, snapshot_id, False)

    def active(self, project_id: UUID, snapshot_id: UUID) -> SemanticCatalogRun | None:
        return self._selected(project_id, snapshot_id, True)

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> SemanticCatalogRun | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(SemanticCatalogRunRecord).where(
                    SemanticCatalogRunRecord.id == run_id,
                    SemanticCatalogRunRecord.project_id == project_id,
                    SemanticCatalogRunRecord.project_version_id == snapshot_id,
                )
            )
            return _run(record) if record else None

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[SemanticCatalogRun], int]:
        with self._session_factory() as session:
            scope = (
                SemanticCatalogRunRecord.project_id == project_id,
                SemanticCatalogRunRecord.project_version_id == snapshot_id,
            )
            total = session.scalar(
                select(func.count()).select_from(SemanticCatalogRunRecord).where(*scope)
            )
            records = session.scalars(
                select(SemanticCatalogRunRecord)
                .where(*scope)
                .order_by(SemanticCatalogRunRecord.revision.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return [_run(record) for record in records], int(total or 0)

    # ----------------------------------------------------------------- publication

    def _lock(self, session: Session, run: SemanticCatalogRun) -> tuple[ProjectVersionRecord, int]:
        version = session.scalar(
            select(ProjectVersionRecord)
            .where(
                ProjectVersionRecord.id == run.snapshot_id,
                ProjectVersionRecord.project_id == run.project_id,
            )
            .with_for_update()
        )
        if version is None:
            raise SemanticCatalogConflictError("Snapshot disappeared before catalog publication.")
        latest = (
            session.scalar(
                select(func.max(SemanticCatalogRunRecord.revision)).where(
                    SemanticCatalogRunRecord.project_version_id == run.snapshot_id
                )
            )
            or 0
        )
        return version, int(latest)

    def publish(self, run: SemanticCatalogRun, build: CatalogBuild) -> SemanticCatalogRun:
        with self._session_factory.begin() as session:
            version, latest = self._lock(session, run)
            structure = session.get(StructuralAnalysisRecord, run.structural_analysis_id)
            processing = session.get(ProcessingRunRecord, run.processing_run_id)
            if (
                structure is None
                or processing is None
                or version.active_analysis_id != run.structural_analysis_id
                or version.active_processing_run_id != run.processing_run_id
                or structure.quarantined
                or processing.quarantined
                or processing.revision != run.processing_revision
                or processing.processing_contract != run.processing_contract
            ):
                raise SemanticCatalogConflictError("Catalog input authority changed.")
            dependencies = [
                (artifact_id, expected)
                for artifact_id, expected in session.execute(
                    select(
                        ArtifactProcessingRecord.artifact_id,
                        ArtifactProcessingRecord.dependency_fingerprint,
                    ).where(
                        ArtifactProcessingRecord.processing_run_id == run.processing_run_id,
                        ArtifactProcessingRecord.dependency_fingerprint.is_not(None),
                    )
                )
            ]
            authority = ProcessingRun(
                processing.id,
                processing.project_version_id,
                processing.started_at,
                processing.completed_at,
                cast(Any, processing.outcome),
                processing.structural_analysis_id,
                dict(processing.processing_contract),
                {},
                {},
                revision=processing.revision,
                quarantined=processing.quarantined,
            )
            observed = structural_artifacts_in_session(session, version.active_analysis_id)
            if processing_freshness(authority, dependencies, observed):
                raise SemanticCatalogConflictError("Catalog processing authority is stale.")
            superseded = (
                latest != run.captured_latest_revision
                or version.active_semantic_catalog_run_id != run.captured_active_catalog_run_id
            )
            record = _run_record(run, latest + 1, superseded)
            session.add(record)
            session.flush()
            self._insert(session, run.id, build)
            if not superseded and run.outcome != "failed":
                version.active_semantic_catalog_run_id = run.id
            return _run(record)

    @staticmethod
    def _insert(session: Session, run_id: UUID, build: CatalogBuild) -> None:
        facts = [
            {
                "id": uuid4(),
                "catalog_run_id": run_id,
                "ordinal": fact.ordinal,
                "subject_entity_key": fact.subject_entity_key,
                "fact_kind": fact.fact_kind,
                "source_refs": list(fact.source_refs),
                "derived": fact.derived,
                "renderer_version": fact.renderer_version,
                "rendered_text": fact.rendered_text,
                "fact_digest": fact.fact_digest,
            }
            for fact in build.facts
        ]
        entities = [
            {
                "id": uuid4(),
                "catalog_run_id": run_id,
                "entity_key": item.entity_key,
                "kind": item.kind,
                "language": item.language,
                "locator": item.locator,
                "artifact_identity_id": item.artifact_identity_id,
                "symbol_identity_id": item.symbol_identity_id,
                "subject_entity_key": item.subject_entity_key,
                "name": item.name,
                "qualified_name": item.qualified_name,
                "symbol_kind": item.symbol_kind,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "ordinal": item.ordinal,
                "portable_key": item.portable_key,
                "stored_coverage": item.stored_coverage,
                "coverage_reason": item.coverage_reason,
                "context_coverage": item.context_coverage,
                "context_reason": item.context_reason,
                "package_digest": item.package_digest,
                "package_bytes": item.package_bytes,
                "content_digest": item.content_digest,
                "limitations": list(item.limitations),
                "evidence": {
                    "chunks": [
                        [
                            str(chunk.chunk_id),
                            chunk.stable_key,
                            chunk.content_hash,
                            chunk.start_offset,
                            chunk.end_offset,
                            chunk.start_line,
                            chunk.end_line,
                            _id(chunk.symbol_identity_id),
                        ]
                        for chunk in item.evidence
                    ],
                    "gaps": item.gaps,
                },
                "package": {
                    "chunks": list(item.package_chunks),
                    "facts": list(item.package_facts),
                    "neighbors": list(item.neighbor_facts),
                    "omitted": [
                        [region.entity_key, region.start_line, region.end_line]
                        for region in item.omitted
                    ],
                },
            }
            for item in build.entities
        ]
        relations = [
            {
                "id": uuid4(),
                "catalog_run_id": run_id,
                "ordinal": item.ordinal,
                "kind": item.kind,
                "source_entity_key": item.source_entity_key,
                "target_entity_key": item.target_entity_key,
                "target_text": item.target_text,
                "resolution_status": item.resolution_status,
                "fact_digest": item.fact_digest,
            }
            for item in build.relations
        ]
        for model, values in (
            (SemanticFactRecord, facts),
            (SemanticEntityRecord, entities),
            (SemanticRelationRecord, relations),
        ):
            for start in range(0, len(values), 1000):
                session.execute(insert(model), values[start : start + 1000])

    def save_failure(self, run: SemanticCatalogRun) -> SemanticCatalogRun:
        with self._session_factory.begin() as session:
            _, latest = self._lock(session, run)
            failed = replace(run, outcome="failed", catalog_digest=None, portable_digest=None)
            record = _run_record(failed, latest + 1, False)
            session.add(record)
            session.flush()
            return _run(record)

    # ----------------------------------------------------------------- reads

    def entity_page(
        self, run_id: UUID, filters: EntityFilters, limit: int, offset: int
    ) -> tuple[list[SemanticEntity], int]:
        with self._session_factory() as session:
            scope: list[Any] = [SemanticEntityRecord.catalog_run_id == run_id]
            if filters.kind:
                scope.append(SemanticEntityRecord.kind == filters.kind)
            if filters.language:
                scope.append(SemanticEntityRecord.language == filters.language)
            if filters.locator_prefix:
                escaped = (
                    filters.locator_prefix.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                scope.append(SemanticEntityRecord.locator.like(escaped + "%", escape="\\"))
            if filters.stored_coverage:
                scope.append(SemanticEntityRecord.stored_coverage == filters.stored_coverage)
            if filters.context_coverage:
                scope.append(SemanticEntityRecord.context_coverage == filters.context_coverage)
            if filters.keys is not None:
                scope.append(SemanticEntityRecord.entity_key.in_(list(filters.keys)))
            if filters.query:
                needle = (
                    "%"
                    + filters.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    + "%"
                )
                scope.append(
                    or_(
                        SemanticEntityRecord.name.ilike(needle, escape="\\"),
                        SemanticEntityRecord.qualified_name.ilike(needle, escape="\\"),
                        SemanticEntityRecord.locator.ilike(needle, escape="\\"),
                    )
                )
            total = session.scalar(
                select(func.count()).select_from(SemanticEntityRecord).where(*scope)
            )
            records = session.scalars(
                select(SemanticEntityRecord)
                .where(*scope)
                .order_by(SemanticEntityRecord.locator, SemanticEntityRecord.ordinal)
                .limit(limit)
                .offset(offset)
            ).all()
            return [_entity(record) for record in records], int(total or 0)

    def entity(self, run_id: UUID, entity_key: str) -> SemanticEntity | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(SemanticEntityRecord).where(
                    SemanticEntityRecord.catalog_run_id == run_id,
                    SemanticEntityRecord.entity_key == entity_key,
                )
            )
            return _entity(record) if record else None

    def facts(self, run_id: UUID, digests: Sequence[str]) -> list[SemanticFact]:
        if not digests:
            return []
        with self._session_factory() as session:
            records = session.scalars(
                select(SemanticFactRecord)
                .where(
                    SemanticFactRecord.catalog_run_id == run_id,
                    SemanticFactRecord.fact_digest.in_(list(digests)),
                )
                .order_by(SemanticFactRecord.ordinal)
            ).all()
            return [_fact(record) for record in records]

    def subject_facts(self, run_id: UUID, entity_key: str) -> list[SemanticFact]:
        with self._session_factory() as session:
            records = session.scalars(
                select(SemanticFactRecord)
                .where(
                    SemanticFactRecord.catalog_run_id == run_id,
                    SemanticFactRecord.subject_entity_key == entity_key,
                )
                .order_by(SemanticFactRecord.ordinal)
            ).all()
            return [_fact(record) for record in records]

    def relations(
        self, run_id: UUID, entity_key: str, limit: int
    ) -> tuple[list[SemanticRelation], list[SemanticRelation]]:
        with self._session_factory() as session:

            def page(column: Any) -> list[SemanticRelation]:
                return [
                    _relation(record)
                    for record in session.scalars(
                        select(SemanticRelationRecord)
                        .where(
                            SemanticRelationRecord.catalog_run_id == run_id, column == entity_key
                        )
                        .order_by(SemanticRelationRecord.ordinal)
                        .limit(limit)
                    )
                ]

            return (
                page(SemanticRelationRecord.source_entity_key),
                page(SemanticRelationRecord.target_entity_key),
            )

    def labels(self, run_id: UUID, entity_keys: Sequence[str]) -> dict[str, str]:
        if not entity_keys:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    SemanticEntityRecord.entity_key,
                    SemanticEntityRecord.kind,
                    SemanticEntityRecord.symbol_kind,
                    SemanticEntityRecord.name,
                    SemanticEntityRecord.locator,
                ).where(
                    SemanticEntityRecord.catalog_run_id == run_id,
                    SemanticEntityRecord.entity_key.in_(list(entity_keys)),
                )
            ).all()
        return {
            key: (
                f"file {locator}"
                if kind == "file"
                else f"{(symbol_kind if kind == 'symbol' else kind or '').replace('_', ' ')} "
                f"{name} · {locator}"
            )
            for key, kind, symbol_kind, name, locator in rows
        }

    # ----------------------------------------------------------------- read-time verification

    def _load_refs(
        self, facts: Sequence[SemanticFact]
    ) -> dict[tuple[str, str], tuple[Row, str | None, str]]:
        """Every cited row, normalised exactly as at build time, with its current authority."""
        wanted: dict[str, set[str]] = defaultdict(set)
        for fact in facts:
            for ref in fact.source_refs:
                wanted[str(ref.get("table"))].add(str(ref.get("id")))
        loaded: dict[tuple[str, str], tuple[Row, str | None, str]] = {}
        with self._session_factory() as session:
            for table, ids in wanted.items():
                if table not in _TABLES:
                    continue
                model, normaliser = _TABLES[table]
                records = session.scalars(
                    select(model).where(model.id.in_([UUID(item) for item in ids]))
                ).all()
                for record in records:
                    contract, locator = self._authority(session, table, record)
                    loaded[(table, str(record.id))] = (normaliser(record), contract, locator)
        return loaded

    def fact_rows(self, facts: Sequence[SemanticFact]) -> dict[str, list[Row]]:
        """Cited rows in reference order, for facts whose rows all still exist."""
        loaded = self._load_refs(facts)
        values: dict[str, list[Row]] = {}
        for fact in facts:
            rows = [
                loaded.get((str(ref.get("table")), str(ref.get("id")))) for ref in fact.source_refs
            ]
            if all(row is not None for row in rows):
                values[fact.fact_digest] = [row[0] for row in rows if row is not None]
        return values

    def names(self, run_id: UUID, entity_keys: Sequence[str]) -> dict[str, tuple[str, ...]]:
        if not entity_keys:
            return {}
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    SemanticEntityRecord.entity_key,
                    SemanticEntityRecord.name,
                    SemanticEntityRecord.qualified_name,
                ).where(
                    SemanticEntityRecord.catalog_run_id == run_id,
                    SemanticEntityRecord.entity_key.in_(list(entity_keys)),
                )
            ).all()
        return {
            key: tuple(dict.fromkeys(value for value in (name, qualified) if value))
            for key, name, qualified in rows
        }

    def artifact_ranges(
        self, structural_analysis_id: UUID, artifact_identity_id: UUID | None
    ) -> dict[str, tuple[tuple[int, int], ...]] | None:
        """Comment, docstring and string ranges (decoded offsets) for quote classification."""
        if artifact_identity_id is None:
            return None
        with self._session_factory() as session:
            analysis = session.scalar(
                select(ArtifactAnalysisRecord)
                .join(ArtifactRecord, ArtifactRecord.id == ArtifactAnalysisRecord.artifact_id)
                .where(
                    ArtifactAnalysisRecord.structural_analysis_id == structural_analysis_id,
                    or_(
                        ArtifactAnalysisRecord.artifact_identity_id == artifact_identity_id,
                        ArtifactRecord.stable_identity_id == artifact_identity_id,
                    ),
                )
            )
            if analysis is None:
                return None
            facts = [
                record.artifact_facts or {}
                for record in session.scalars(
                    select(ArtifactExtractionRecord).where(
                        ArtifactExtractionRecord.artifact_analysis_id == analysis.id,
                        ArtifactExtractionRecord.outcome.in_(["extracted", "partial"]),
                    )
                )
            ]
        usable = [value for value in facts if "comment_ranges" in value]
        if not usable:
            return None

        def ranges(*names: str) -> tuple[tuple[int, int], ...]:
            return tuple(
                (int(pair[0]), int(pair[1]))
                for value in usable
                for name in names
                for pair in value.get(name) or []
            )

        return {
            "documentation": ranges("comment_ranges", "docstring_ranges"),
            "string": ranges("string_ranges"),
        }

    def verify_facts(self, facts: Sequence[SemanticFact]) -> dict[str, str]:
        """Re-render every fact from its cited rows (invariant F2)."""
        loaded = self._load_refs(facts)
        results: dict[str, str] = {}
        for fact in facts:
            if fact.renderer_version != RENDERER_VERSION:
                results[fact.fact_digest] = "FACT_RENDERER_CHANGED"
                continue
            rows: list[Row] = []
            status = "verified"
            for ref in fact.source_refs:
                found = loaded.get((str(ref.get("table")), str(ref.get("id"))))
                if found is None:
                    status = "FACT_SOURCE_MISSING"
                    break
                row, contract, locator = found
                if contract != ref.get("contract") or locator != ref.get("locator"):
                    status = "FACT_PROVENANCE_CHANGED"
                    break
                rows.append(row)
            if status == "verified":
                try:
                    _, value = rendered_fact(
                        fact.fact_kind, list(fact.source_refs), rows, fact.derived
                    )
                except (KeyError, TypeError, ValueError, IndexError):
                    value = ""
                if value != fact.fact_digest:
                    status = "FACT_DIGEST_MISMATCH"
            results[fact.fact_digest] = status
        return results

    @staticmethod
    def _authority(session: Session, table: str, record: Any) -> tuple[str | None, str]:
        if table == "artifacts":
            return None, str(record.locator)
        analysis_id = record.id if table == "artifact_analyses" else record.artifact_analysis_id
        analysis = session.get(ArtifactAnalysisRecord, analysis_id)
        if analysis is None:
            return "", ""
        if table == "artifact_extractions":
            return str(record.contract_digest), analysis.locator
        if table in {"symbol_facts", "symbol_references", "export_references"}:
            extraction = session.get(ArtifactExtractionRecord, record.extraction_id)
            return (str(extraction.contract_digest) if extraction else ""), analysis.locator
        structure = session.get(StructuralAnalysisRecord, analysis.structural_analysis_id)
        producer = digest(structure.producer_contract) if structure else ""
        if table == "artifact_analyses" or analysis.language in {None, "Python"}:
            return producer, analysis.locator
        declaration = (
            session.get(ArtifactExtractionRecord, analysis.declaration_extraction_id)
            if analysis.declaration_extraction_id
            else None
        )
        return (str(declaration.contract_digest) if declaration else ""), analysis.locator

    def evidence(
        self, run: SemanticCatalogRun, chunks: Sequence[EvidenceChunk]
    ) -> list[EvidenceChunk]:
        """Chunk content for an entity, hash- and length-verified at read time (F2)."""
        if not chunks:
            return []
        with self._session_factory() as session:
            records = {
                record.id: record
                for record in session.scalars(
                    select(ChunkRecord)
                    .join(
                        ArtifactProcessingRecord,
                        ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
                    )
                    .where(
                        ArtifactProcessingRecord.processing_run_id == run.processing_run_id,
                        ChunkRecord.id.in_([chunk.chunk_id for chunk in chunks]),
                    )
                )
            }
        values: list[EvidenceChunk] = []
        for chunk in chunks:
            record = records.get(chunk.chunk_id)
            if record is None:
                values.append(replace(chunk, verified=False))
                continue
            verified = (
                record.stable_key == chunk.stable_key
                and record.content_hash == chunk.content_hash
                and hashlib.sha256(record.content.encode("utf-8")).hexdigest() == chunk.content_hash
                and len(record.content) == chunk.end_offset - chunk.start_offset
            )
            values.append(
                replace(chunk, content=record.content if verified else None, verified=verified)
            )
        return values
