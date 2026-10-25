"""SQLAlchemy store for independently retained processing attempts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer, sessionmaker

from buildlens.application.processing import ProcessingConflictError as ProcessingConflictError
from buildlens.application.processing import dependency_fingerprint
from buildlens.domain.entities import Artifact, Project, ProjectVersion
from buildlens.domain.processing import (
    ArtifactProcessing,
    Chunk,
    ChunkSpan,
    ProcessingDiagnostic,
    ProcessingRun,
    ProcessorContract,
)
from buildlens.domain.source import processing_contract
from buildlens.domain.structural import (
    ArtifactAnalysis,
    ArtifactExtraction,
    SourceSpan,
    StructuralAnalysis,
    Symbol,
)
from buildlens.infrastructure.persistence.models import (
    ArtifactAnalysisRecord,
    ArtifactExtractionRecord,
    ArtifactProcessingRecord,
    ArtifactRecord,
    ChunkRecord,
    ProcessingRunRecord,
    ProjectRecord,
    ProjectVersionRecord,
    SymbolRecord,
)
from buildlens.infrastructure.persistence.repositories import _to_domain
from buildlens.infrastructure.persistence.snapshots import _artifact, _version
from buildlens.infrastructure.persistence.structural import (
    SqlAlchemyStructuralAnalysisStore,
)


def _span(value: ChunkSpan | None) -> dict[str, int] | None:
    return (
        None
        if value is None
        else {
            key: getattr(value, key)
            for key in (
                "start_offset",
                "end_offset",
                "start_line",
                "end_line",
                "start_column",
                "end_column",
            )
        }
    )


def _span_from(value: dict[str, Any] | None) -> ChunkSpan | None:
    return ChunkSpan(**value) if value else None


def _diag(value: ProcessingDiagnostic) -> dict[str, object]:
    return {
        "code": value.code,
        "message": value.message,
        "severity": value.severity,
        "span": _span(value.span),
    }


def _diag_from(value: dict[str, Any]) -> ProcessingDiagnostic:
    return ProcessingDiagnostic(
        cast(str, value["code"]),
        cast(str, value["message"]),
        cast(Any, value["severity"]),
        _span_from(cast(dict[str, Any] | None, value.get("span"))),
    )


def _contract(value: ProcessorContract | None) -> dict[str, object] | None:
    return (
        None
        if value is None
        else {
            key: getattr(value, key)
            for key in (
                "processor",
                "version",
                "boundary_version",
                "decoding",
                "span_convention",
                "screening_version",
            )
        }
    )


def _contract_from(value: dict[str, Any] | None) -> ProcessorContract | None:
    return ProcessorContract(**value) if value else None


def structural_artifacts_in_session(
    session: Session, analysis_id: UUID | None
) -> dict[UUID, ArtifactAnalysis]:
    """Load dependency evidence on an existing transaction boundary."""
    if analysis_id is None:
        return {}
    records = session.scalars(
        select(ArtifactAnalysisRecord).where(
            ArtifactAnalysisRecord.structural_analysis_id == analysis_id
        )
    ).all()
    symbols = session.scalars(
        select(SymbolRecord)
        .options(
            defer(SymbolRecord.signature, raiseload=True),
            defer(SymbolRecord.decorators, raiseload=True),
            defer(SymbolRecord.type_parameters, raiseload=True),
        )
        .where(SymbolRecord.structural_analysis_id == analysis_id)
        .order_by(SymbolRecord.ordinal)
    ).all()
    grouped: dict[UUID, list[Symbol]] = defaultdict(list)
    for item in symbols:
        grouped[item.artifact_analysis_id].append(
            Symbol(
                item.id,
                item.artifact_analysis_id,
                item.parent_id,
                item.ordinal,
                cast(Any, item.kind),
                item.name,
                item.qualified_name,
                item.is_async,
                SourceSpan(item.start_line, item.end_line, item.start_column, item.end_column),
                item.full_start_line,
                None,
                stable_identity_id=item.stable_identity_id,
                state_fingerprint=item.state_fingerprint,
                declaration_form=item.declaration_form,
                declaration_space=item.declaration_space,
                start_offset=item.start_offset,
                end_offset=item.end_offset,
                additional_spans=tuple(
                    (int(pair[0]), int(pair[1])) for pair in (item.additional_spans or [])
                ),
            )
        )
    declarations = {
        record.id: ArtifactExtraction(
            record.id,
            record.extractor,
            {},
            record.contract_digest,
            cast(Any, record.outcome),
            record.input_fingerprint,
        )
        for record in session.scalars(
            select(ArtifactExtractionRecord).where(
                ArtifactExtractionRecord.structural_analysis_id == analysis_id,
                ArtifactExtractionRecord.id.in_(
                    select(ArtifactAnalysisRecord.declaration_extraction_id).where(
                        ArtifactAnalysisRecord.structural_analysis_id == analysis_id
                    )
                ),
            )
        ).all()
    }
    return {
        record.artifact_id: ArtifactAnalysis(
            record.id,
            record.structural_analysis_id,
            record.project_version_id,
            record.artifact_id,
            record.locator,
            cast(Any, record.outcome),
            record.read_started_at,
            record.read_completed_at,
            record.encoding,
            record.line_count,
            record.is_package_initializer,
            symbols=tuple(grouped[record.id]),
            input_fingerprint=record.input_fingerprint,
            snapshot_verified=record.snapshot_verified,
            language=record.language,
            declaration_extraction_id=record.declaration_extraction_id,
            extractions=(declarations[record.declaration_extraction_id],)
            if record.declaration_extraction_id in declarations
            else (),
        )
        for record in records
    }


class SqlAlchemyProcessingStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_project(self, project_id: UUID) -> Project | None:
        with self._session_factory() as session:
            value = session.get(ProjectRecord, project_id)
            return _to_domain(value) if value else None

    def get_snapshot(self, project_id: UUID, snapshot_id: UUID) -> ProjectVersion | None:
        with self._session_factory() as session:
            value = session.scalar(
                select(ProjectVersionRecord).where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                    ProjectVersionRecord.scan_completed_at.is_not(None),
                )
            )
            return _version(value) if value else None

    def artifacts(self, project_id: UUID, snapshot_id: UUID) -> list[Artifact]:
        with self._session_factory() as session:
            values = session.scalars(
                select(ArtifactRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ArtifactRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ArtifactRecord.project_version_id == snapshot_id,
                )
                .order_by(ArtifactRecord.locator)
            ).all()
            return [_artifact(x) for x in values]

    def active_structural(self, project_id: UUID, snapshot_id: UUID) -> StructuralAnalysis | None:
        return SqlAlchemyStructuralAnalysisStore(self._session_factory).latest_analysis(
            project_id, snapshot_id
        )

    def structural_artifacts(self, analysis_id: UUID) -> dict[UUID, ArtifactAnalysis]:
        with self._session_factory() as session:
            return structural_artifacts_in_session(session, analysis_id)

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> ProcessingRun | None:
        with self._session_factory() as session:
            value = session.scalar(
                select(ProcessingRunRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ProcessingRunRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProcessingRunRecord.project_version_id == snapshot_id,
                    ProcessingRunRecord.id == run_id,
                )
            )
            return self._run(session, value) if value else None

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[ProcessingRun], int]:
        with self._session_factory() as session:
            base = (
                select(ProcessingRunRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ProcessingRunRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProcessingRunRecord.project_version_id == snapshot_id,
                )
            )
            total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
            records = session.scalars(
                base.order_by(ProcessingRunRecord.revision.desc()).limit(limit).offset(offset)
            ).all()
            return [self._run(session, x) for x in records], total

    def dependencies(self, run_id: UUID) -> list[tuple[UUID, str]]:
        with self._session_factory() as session:
            return [
                (artifact, fingerprint)
                for artifact, fingerprint in session.execute(
                    select(
                        ArtifactProcessingRecord.artifact_id,
                        ArtifactProcessingRecord.dependency_fingerprint,
                    ).where(
                        ArtifactProcessingRecord.processing_run_id == run_id,
                        ArtifactProcessingRecord.dependency_fingerprint.is_not(None),
                    )
                ).all()
            ]

    def reusable(
        self, project_id: UUID, snapshot: ProjectVersion
    ) -> dict[UUID, ArtifactProcessing]:
        with self._session_factory() as session:
            versions = [snapshot.id] + (
                [snapshot.predecessor_id] if snapshot.predecessor_id else []
            )
            records = session.execute(
                select(ArtifactProcessingRecord, ArtifactRecord.stable_identity_id)
                .join(
                    ProcessingRunRecord,
                    ProcessingRunRecord.id == ArtifactProcessingRecord.processing_run_id,
                )
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.active_processing_run_id == ProcessingRunRecord.id,
                )
                .join(ArtifactRecord, ArtifactRecord.id == ArtifactProcessingRecord.artifact_id)
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProjectVersionRecord.id.in_(versions),
                    ProcessingRunRecord.quarantined.is_(False),
                    ProcessingRunRecord.processing_contract == processing_contract(),
                    ArtifactProcessingRecord.outcome.in_(["processed", "empty"]),
                )
                .order_by(ProjectVersionRecord.sequence)
            ).all()
            ids = [x.id for x, _ in records]
            grouped: dict[UUID, list[Chunk]] = defaultdict(list)
            for chunk in session.scalars(
                select(ChunkRecord)
                .where(ChunkRecord.artifact_processing_id.in_(ids))
                .order_by(ChunkRecord.ordinal)
            ):
                grouped[chunk.artifact_processing_id].append(self._chunk(chunk))
            return {
                identity: replace(
                    self._artifact_processing(session, record, False),
                    chunks=tuple(grouped[record.id]),
                )
                for record, identity in records
                if identity
            }

    def latest(self, project_id: UUID, snapshot_id: UUID) -> ProcessingRun | None:
        with self._session_factory() as session:
            value = session.scalar(
                select(ProcessingRunRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ProcessingRunRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProcessingRunRecord.project_version_id == snapshot_id,
                )
                .order_by(ProcessingRunRecord.revision.desc())
                .limit(1)
            )
            return self._run(session, value) if value else None

    def active(self, project_id: UUID, snapshot_id: UUID) -> ProcessingRun | None:
        with self._session_factory() as session:
            value = session.scalar(
                select(ProcessingRunRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.active_processing_run_id == ProcessingRunRecord.id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProjectVersionRecord.id == snapshot_id,
                )
            )
            return self._run(session, value) if value else None

    def save(
        self, project_id: UUID, snapshot_id: UUID, run: ProcessingRun, activate: bool
    ) -> ProcessingRun:
        with self._session_factory.begin() as session:
            version = session.scalar(
                select(ProjectVersionRecord)
                .where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                )
                .with_for_update()
            )
            if not version:
                raise RuntimeError("Snapshot disappeared before processing persistence.")
            latest = (
                session.scalar(
                    select(func.max(ProcessingRunRecord.revision)).where(
                        ProcessingRunRecord.project_version_id == snapshot_id
                    )
                )
                or 0
            )
            superseded = (
                latest != run.captured_latest_revision
                or version.active_processing_run_id != run.captured_active_processing_run_id
            )
            if version.active_analysis_id != run.structural_analysis_id:
                structures = (
                    self.structural_artifacts(version.active_analysis_id)
                    if version.active_analysis_id
                    else {}
                )
                superseded |= any(
                    value.dependency_fingerprint
                    and (
                        value.artifact_id not in structures
                        or dependency_fingerprint(structures[value.artifact_id])
                        != value.dependency_fingerprint
                    )
                    for value in run.artifact_processings
                )
            persisted = ProcessingRunRecord(
                id=run.id,
                project_version_id=snapshot_id,
                revision=latest + 1,
                started_at=run.started_at,
                completed_at=run.completed_at,
                outcome=run.outcome,
                structural_analysis_id=run.structural_analysis_id,
                processing_contract=run.processing_contract,
                limits=run.limits,
                coverage=run.coverage,
                diagnostics=[_diag(x) for x in run.diagnostics],
                captured_active_processing_run_id=run.captured_active_processing_run_id,
                captured_latest_revision=run.captured_latest_revision,
                quarantined=False,
                superseded=superseded,
                processor_set=list(run.processor_set) if run.processor_set else None,
            )
            session.add(persisted)
            session.flush()
            for value in run.artifact_processings:
                session.add(
                    ArtifactProcessingRecord(
                        id=value.id,
                        processing_run_id=run.id,
                        project_version_id=snapshot_id,
                        artifact_id=value.artifact_id,
                        locator=value.locator,
                        outcome=value.outcome,
                        processor_contract=_contract(value.processor_contract),
                        input_fingerprint=value.input_fingerprint,
                        encoding=value.encoding,
                        diagnostics=[_diag(x) for x in value.diagnostics],
                        execution_mode=value.execution_mode,
                        reuse_source_artifact_processing_id=value.reuse_source_artifact_processing_id,
                        structural_artifact_analysis_id=value.structural_artifact_analysis_id,
                        result_digest=value.result_digest,
                        dependency_fingerprint=value.dependency_fingerprint,
                        fingerprint_scheme=value.fingerprint_scheme,
                    )
                )
                session.flush()
                for chunk in value.chunks:
                    session.add(
                        ChunkRecord(
                            structural_artifact_analysis_id=value.structural_artifact_analysis_id,
                            id=chunk.id,
                            artifact_processing_id=value.id,
                            stable_key=chunk.stable_key,
                            ordinal=chunk.ordinal,
                            unit_anchor=chunk.unit_anchor,
                            part_index=chunk.part_index,
                            kind=chunk.kind,
                            content=chunk.content,
                            content_hash=chunk.content_hash,
                            start_offset=chunk.span.start_offset,
                            end_offset=chunk.span.end_offset,
                            start_line=chunk.span.start_line,
                            end_line=chunk.span.end_line,
                            start_column=chunk.span.start_column,
                            end_column=chunk.span.end_column,
                            symbol_id=chunk.symbol_id,
                            symbol_identity_id=chunk.symbol_identity_id,
                            heading_path=list(chunk.heading_path) or None,
                            json_path=chunk.json_path,
                        )
                    )
            if activate and not superseded:
                version.active_processing_run_id = run.id
            session.flush()
            return replace(run, revision=latest + 1, superseded=superseded)

    def page_artifacts(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID, limit: int, offset: int
    ) -> tuple[list[ArtifactProcessing], int]:
        with self._session_factory() as session:
            base = (
                select(ArtifactProcessingRecord)
                .join(
                    ProcessingRunRecord,
                    ProcessingRunRecord.id == ArtifactProcessingRecord.processing_run_id,
                )
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ProcessingRunRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProcessingRunRecord.project_version_id == snapshot_id,
                    ArtifactProcessingRecord.processing_run_id == run_id,
                )
            )
            total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
            values = session.scalars(
                base.order_by(ArtifactProcessingRecord.locator).limit(limit).offset(offset)
            ).all()
            ids = [value.id for value in values]
            counts: dict[UUID, int] = {
                key: count
                for key, count in session.execute(
                    select(ChunkRecord.artifact_processing_id, func.count())
                    .where(ChunkRecord.artifact_processing_id.in_(ids))
                    .group_by(ChunkRecord.artifact_processing_id)
                ).all()
            }
            return [
                replace(
                    self._artifact_processing(session, x, False),
                    stored_chunk_count=counts.get(x.id, 0),
                )
                for x in values
            ], total

    def page_chunks(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID, limit: int, offset: int
    ) -> tuple[list[Chunk], int]:
        with self._session_factory() as session:
            base = (
                select(
                    ChunkRecord,
                    ArtifactProcessingRecord.locator,
                    ArtifactProcessingRecord.artifact_id,
                    ProcessingRunRecord.id,
                    ProcessingRunRecord.project_version_id,
                )
                .join(
                    ArtifactProcessingRecord,
                    ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
                )
                .join(
                    ProcessingRunRecord,
                    ProcessingRunRecord.id == ArtifactProcessingRecord.processing_run_id,
                )
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ProcessingRunRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProcessingRunRecord.project_version_id == snapshot_id,
                    ArtifactProcessingRecord.processing_run_id == run_id,
                )
            )
            total = (
                session.scalar(
                    select(func.count())
                    .select_from(ChunkRecord)
                    .join(
                        ArtifactProcessingRecord,
                        ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
                    )
                    .where(ArtifactProcessingRecord.processing_run_id == run_id)
                )
                or 0
            )
            values = session.execute(
                base.options(defer(ChunkRecord.content, raiseload=True))
                .order_by(ArtifactProcessingRecord.locator, ChunkRecord.ordinal)
                .limit(limit)
                .offset(offset)
            ).all()
            return [
                replace(
                    self._chunk(x[0], False),
                    locator=x[1],
                    artifact_id=x[2],
                    run_id=x[3],
                    snapshot_id=x[4],
                )
                for x in values
            ], total

    def content_stats(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> tuple[int, int]:
        with self._session_factory() as session:
            count, source_bytes = session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(func.octet_length(ChunkRecord.content)), 0),
                )
                .select_from(ChunkRecord)
                .join(
                    ArtifactProcessingRecord,
                    ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
                )
                .join(
                    ProcessingRunRecord,
                    ProcessingRunRecord.id == ArtifactProcessingRecord.processing_run_id,
                )
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ProcessingRunRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProcessingRunRecord.project_version_id == snapshot_id,
                    ProcessingRunRecord.id == run_id,
                )
            ).one()
            return int(count), int(source_bytes)

    def page_chunk_content(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID, limit: int, offset: int
    ) -> list[Chunk]:
        with self._session_factory() as session:
            values = session.execute(
                self._content_query(project_id, snapshot_id, run_id)
                .order_by(
                    ArtifactProcessingRecord.locator,
                    ChunkRecord.ordinal,
                    ChunkRecord.id,
                )
                .limit(limit)
                .offset(offset)
            ).all()
            return [self._owned_chunk(value) for value in values]

    def chunks_by_id(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID,
        chunk_ids: Sequence[UUID],
    ) -> list[Chunk]:
        if not chunk_ids:
            return []
        with self._session_factory() as session:
            values = session.execute(
                self._content_query(project_id, snapshot_id, run_id).where(
                    ChunkRecord.id.in_(chunk_ids)
                )
            ).all()
            by_id = {value[0].id: self._owned_chunk(value) for value in values}
            return [by_id[value] for value in chunk_ids if value in by_id]

    @staticmethod
    def _content_query(project_id: UUID, snapshot_id: UUID, run_id: UUID) -> Any:
        return (
            select(
                ChunkRecord,
                ArtifactProcessingRecord.locator,
                ArtifactProcessingRecord.artifact_id,
                ProcessingRunRecord.id,
                ProcessingRunRecord.project_version_id,
            )
            .join(
                ArtifactProcessingRecord,
                ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
            )
            .join(
                ProcessingRunRecord,
                ProcessingRunRecord.id == ArtifactProcessingRecord.processing_run_id,
            )
            .join(
                ProjectVersionRecord,
                ProjectVersionRecord.id == ProcessingRunRecord.project_version_id,
            )
            .where(
                ProjectVersionRecord.project_id == project_id,
                ProcessingRunRecord.project_version_id == snapshot_id,
                ProcessingRunRecord.id == run_id,
                ProcessingRunRecord.quarantined.is_(False),
                ProcessingRunRecord.processing_contract == processing_contract(),
            )
        )

    def _owned_chunk(self, value: Any) -> Chunk:
        return replace(
            self._chunk(value[0]),
            locator=value[1],
            artifact_id=value[2],
            run_id=value[3],
            snapshot_id=value[4],
        )

    def chunk(
        self, project_id: UUID, snapshot_id: UUID, chunk_id: UUID, run_id: UUID
    ) -> Chunk | None:
        with self._session_factory() as session:
            value = session.execute(
                select(
                    ChunkRecord,
                    ArtifactProcessingRecord.locator,
                    ArtifactProcessingRecord.artifact_id,
                    ProcessingRunRecord.id,
                    ProcessingRunRecord.project_version_id,
                )
                .join(
                    ArtifactProcessingRecord,
                    ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
                )
                .join(
                    ProcessingRunRecord,
                    ProcessingRunRecord.id == ArtifactProcessingRecord.processing_run_id,
                )
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ProcessingRunRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProcessingRunRecord.project_version_id == snapshot_id,
                    ChunkRecord.id == chunk_id,
                    ProcessingRunRecord.id == run_id,
                    ProcessingRunRecord.quarantined.is_(False),
                    ProcessingRunRecord.processing_contract == processing_contract(),
                )
            ).first()
            return (
                replace(
                    self._chunk(value[0]),
                    locator=value[1],
                    artifact_id=value[2],
                    run_id=value[3],
                    snapshot_id=value[4],
                )
                if value
                else None
            )

    def _chunk(self, x: ChunkRecord, include_content: bool = True) -> Chunk:
        return Chunk(
            x.id,
            x.artifact_processing_id,
            x.stable_key,
            x.ordinal,
            x.unit_anchor,
            x.part_index,
            x.kind,
            x.content if include_content else "",
            x.content_hash,
            ChunkSpan(
                x.start_offset, x.end_offset, x.start_line, x.end_line, x.start_column, x.end_column
            ),
            x.symbol_id,
            x.symbol_identity_id,
            tuple(x.heading_path or []),
            x.json_path,
        )

    def _artifact_processing(
        self, session: Session, x: ArtifactProcessingRecord, include_chunks: bool = True
    ) -> ArtifactProcessing:
        chunks = (
            tuple(
                self._chunk(v)
                for v in session.scalars(
                    select(ChunkRecord)
                    .where(ChunkRecord.artifact_processing_id == x.id)
                    .order_by(ChunkRecord.ordinal)
                ).all()
            )
            if include_chunks
            else ()
        )
        return ArtifactProcessing(
            x.id,
            x.processing_run_id,
            x.project_version_id,
            x.artifact_id,
            x.locator,
            cast(Any, x.outcome),
            _contract_from(cast(dict[str, Any] | None, x.processor_contract)),
            x.input_fingerprint,
            x.encoding,
            tuple(_diag_from(v) for v in x.diagnostics),
            chunks,
            cast(Any, x.execution_mode),
            x.reuse_source_artifact_processing_id,
            x.structural_artifact_analysis_id,
            x.result_digest,
            dependency_fingerprint=x.dependency_fingerprint,
            fingerprint_scheme=x.fingerprint_scheme,
        )

    def _run(self, session: Session, x: ProcessingRunRecord) -> ProcessingRun:
        values: tuple[ArtifactProcessing, ...] = ()
        return ProcessingRun(
            x.id,
            x.project_version_id,
            x.started_at,
            x.completed_at,
            cast(Any, x.outcome),
            x.structural_analysis_id,
            x.processing_contract,
            cast(dict[str, int], x.limits),
            cast(dict[str, int], x.coverage),
            tuple(_diag_from(v) for v in x.diagnostics),
            values,
            x.revision,
            x.captured_active_processing_run_id,
            x.captured_latest_revision,
            quarantined=x.quarantined,
            superseded=x.superseded,
            processor_set=tuple(x.processor_set) if x.processor_set else None,
        )
