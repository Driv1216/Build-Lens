"""PostgreSQL/pgvector persistence for immutable embedding evidence."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session, defer, sessionmaker

from buildlens.application.embeddings import EmbeddingConflictError
from buildlens.application.processing import processing_freshness
from buildlens.domain.embeddings import (
    ChunkEmbedding,
    EmbeddingContract,
    EmbeddingRun,
    SemanticSearchFilters,
    SemanticSearchHit,
    membership_digest,
)
from buildlens.domain.processing import Chunk, ChunkSpan, ProcessingRun
from buildlens.infrastructure.persistence.models import (
    ArtifactProcessingRecord,
    ArtifactRecord,
    ChunkEmbeddingRecord,
    ChunkRecord,
    EmbeddingRunRecord,
    ProcessingRunRecord,
    ProjectVersionRecord,
)
from buildlens.infrastructure.persistence.processing import structural_artifacts_in_session


def _contract(value: dict[str, object]) -> EmbeddingContract:
    return EmbeddingContract(**cast(Any, value))


def _run(value: EmbeddingRunRecord, chunk_count: int = 0) -> EmbeddingRun:
    return EmbeddingRun(
        value.id,
        value.project_id,
        value.project_version_id,
        value.processing_run_id,
        value.processing_revision,
        dict(value.processing_contract),
        _contract(value.embedding_contract),
        value.revision,
        value.started_at,
        value.completed_at,
        cast(Any, value.outcome),
        value.superseded,
        value.quarantined,
        value.captured_active_embedding_run_id,
        value.captured_latest_revision,
        value.membership_digest,
        dict(value.limits),
        {key: int(cast(int | str, item)) for key, item in value.coverage.items()},
        tuple(value.diagnostics),
        chunk_count,
    )


def _embedding(value: ChunkEmbeddingRecord, include_vector: bool = False) -> ChunkEmbedding:
    values = tuple(float(item) for item in value.embedding) if include_vector else None
    return ChunkEmbedding(
        value.id,
        value.embedding_run_id,
        value.project_id,
        value.project_version_id,
        value.processing_run_id,
        value.artifact_processing_id,
        value.chunk_id,
        value.stable_chunk_key,
        value.input_hash,
        value.logical_embedding_key,
        value.contract_digest,
        value.dimensions,
        value.window_count,
        cast(Any, value.execution_mode),
        value.reuse_source_embedding_id,
        values,
    )


def _chunk(row: Any) -> Chunk:
    value: ChunkRecord = row[0]
    return Chunk(
        value.id,
        value.artifact_processing_id,
        value.stable_key,
        value.ordinal,
        value.unit_anchor,
        value.part_index,
        value.kind,
        value.content,
        value.content_hash,
        ChunkSpan(
            value.start_offset,
            value.end_offset,
            value.start_line,
            value.end_line,
            value.start_column,
            value.end_column,
        ),
        value.symbol_id,
        value.symbol_identity_id,
        tuple(value.heading_path or ()),
        value.json_path,
        locator=row[1],
        artifact_id=row[2],
        run_id=row[3],
        snapshot_id=row[4],
    )


class SqlAlchemyEmbeddingStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def latest(self, project_id: UUID, snapshot_id: UUID) -> EmbeddingRun | None:
        return self._selected_run(project_id, snapshot_id, False)

    def active(self, project_id: UUID, snapshot_id: UUID) -> EmbeddingRun | None:
        return self._selected_run(project_id, snapshot_id, True)

    def _selected_run(
        self, project_id: UUID, snapshot_id: UUID, active: bool
    ) -> EmbeddingRun | None:
        with self._session_factory() as session:
            query = (
                select(EmbeddingRunRecord, func.count(ChunkEmbeddingRecord.id))
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == EmbeddingRunRecord.project_version_id,
                )
                .outerjoin(
                    ChunkEmbeddingRecord,
                    ChunkEmbeddingRecord.embedding_run_id == EmbeddingRunRecord.id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    EmbeddingRunRecord.project_version_id == snapshot_id,
                )
                .group_by(EmbeddingRunRecord.id)
            )
            if active:
                query = query.where(
                    ProjectVersionRecord.active_embedding_run_id == EmbeddingRunRecord.id
                )
            else:
                query = query.order_by(EmbeddingRunRecord.revision.desc()).limit(1)
            row = session.execute(query).first()
            return _run(row[0], int(row[1])) if row else None

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[EmbeddingRun], int]:
        with self._session_factory() as session:
            scope = (
                EmbeddingRunRecord.project_id == project_id,
                EmbeddingRunRecord.project_version_id == snapshot_id,
            )
            total = session.scalar(
                select(func.count()).select_from(EmbeddingRunRecord).where(*scope)
            )
            rows = session.execute(
                select(EmbeddingRunRecord, func.count(ChunkEmbeddingRecord.id))
                .outerjoin(
                    ChunkEmbeddingRecord,
                    ChunkEmbeddingRecord.embedding_run_id == EmbeddingRunRecord.id,
                )
                .where(*scope)
                .group_by(EmbeddingRunRecord.id)
                .order_by(EmbeddingRunRecord.revision.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return [_run(item, int(count)) for item, count in rows], int(total or 0)

    def get(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> EmbeddingRun | None:
        with self._session_factory() as session:
            row = session.execute(
                select(EmbeddingRunRecord, func.count(ChunkEmbeddingRecord.id))
                .outerjoin(
                    ChunkEmbeddingRecord,
                    ChunkEmbeddingRecord.embedding_run_id == EmbeddingRunRecord.id,
                )
                .where(
                    EmbeddingRunRecord.id == run_id,
                    EmbeddingRunRecord.project_id == project_id,
                    EmbeddingRunRecord.project_version_id == snapshot_id,
                )
                .group_by(EmbeddingRunRecord.id)
            ).first()
            return _run(row[0], int(row[1])) if row else None

    def page_rows(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID, limit: int, offset: int
    ) -> tuple[list[ChunkEmbedding], int]:
        with self._session_factory() as session:
            scope = (
                ChunkEmbeddingRecord.project_id == project_id,
                ChunkEmbeddingRecord.project_version_id == snapshot_id,
                ChunkEmbeddingRecord.embedding_run_id == run_id,
            )
            total = session.scalar(
                select(func.count()).select_from(ChunkEmbeddingRecord).where(*scope)
            )
            values = session.scalars(
                select(ChunkEmbeddingRecord)
                .options(defer(ChunkEmbeddingRecord.embedding, raiseload=True))
                .where(*scope)
                .order_by(ChunkEmbeddingRecord.stable_chunk_key, ChunkEmbeddingRecord.chunk_id)
                .limit(limit)
                .offset(offset)
            ).all()
            return [_embedding(value) for value in values], int(total or 0)

    def reusable(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        logical_keys: list[str],
        contract: EmbeddingContract,
        processing_contract: dict[str, object],
    ) -> dict[str, ChunkEmbedding]:
        if not logical_keys:
            return {}
        with self._session_factory() as session:
            predecessor_id = session.scalar(
                select(ProjectVersionRecord.predecessor_id).where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                )
            )
            priority = case(
                (EmbeddingRunRecord.project_version_id == snapshot_id, 0),
                (EmbeddingRunRecord.project_version_id == predecessor_id, 1),
                else_=2,
            )
            values = session.scalars(
                select(ChunkEmbeddingRecord)
                .join(
                    EmbeddingRunRecord,
                    EmbeddingRunRecord.id == ChunkEmbeddingRecord.embedding_run_id,
                )
                .join(
                    ProcessingRunRecord,
                    ProcessingRunRecord.id == ChunkEmbeddingRecord.processing_run_id,
                )
                .where(
                    ChunkEmbeddingRecord.project_id == project_id,
                    ChunkEmbeddingRecord.logical_embedding_key.in_(logical_keys),
                    ChunkEmbeddingRecord.contract_digest == contract.digest,
                    ChunkEmbeddingRecord.dimensions == contract.dimensions,
                    EmbeddingRunRecord.embedding_contract == asdict(contract),
                    EmbeddingRunRecord.processing_contract == processing_contract,
                    EmbeddingRunRecord.outcome == "complete",
                    EmbeddingRunRecord.superseded.is_(False),
                    EmbeddingRunRecord.quarantined.is_(False),
                    ProcessingRunRecord.quarantined.is_(False),
                    ProcessingRunRecord.superseded.is_(False),
                )
                .order_by(
                    ChunkEmbeddingRecord.logical_embedding_key,
                    priority,
                    EmbeddingRunRecord.revision.desc(),
                    EmbeddingRunRecord.id,
                    ChunkEmbeddingRecord.id,
                )
                .distinct(ChunkEmbeddingRecord.logical_embedding_key)
                .limit(len(logical_keys))
            ).all()
            found: dict[str, ChunkEmbedding] = {}
            for value in values:
                found.setdefault(value.logical_embedding_key, _embedding(value, True))
            return found

    def publish(
        self,
        run: EmbeddingRun,
        rows: list[ChunkEmbedding],
        authority: ProcessingRun,
    ) -> EmbeddingRun:
        with self._session_factory.begin() as session:
            version = session.scalar(
                select(ProjectVersionRecord)
                .where(
                    ProjectVersionRecord.id == run.snapshot_id,
                    ProjectVersionRecord.project_id == run.project_id,
                )
                .with_for_update()
            )
            if version is None:
                raise EmbeddingConflictError("Snapshot disappeared before embedding publication.")
            processing = session.scalar(
                select(ProcessingRunRecord).where(
                    ProcessingRunRecord.id == authority.id,
                    ProcessingRunRecord.project_version_id == run.snapshot_id,
                )
            )
            if (
                processing is None
                or version.active_processing_run_id != authority.id
                or processing.quarantined
                or processing.processing_contract != authority.processing_contract
                or processing.revision != authority.revision
            ):
                raise EmbeddingConflictError("Current processing evidence changed.")
            dependencies = [
                (artifact_id, expected)
                for artifact_id, expected in session.execute(
                    select(
                        ArtifactProcessingRecord.artifact_id,
                        ArtifactProcessingRecord.dependency_fingerprint,
                    ).where(
                        ArtifactProcessingRecord.processing_run_id == authority.id,
                        ArtifactProcessingRecord.dependency_fingerprint.is_not(None),
                    )
                )
            ]
            current_authority = replace(
                authority,
                quarantined=processing.quarantined,
                processing_contract=dict(processing.processing_contract),
            )
            observed_structures = structural_artifacts_in_session(
                session, version.active_analysis_id
            )
            if processing_freshness(current_authority, dependencies, observed_structures):
                raise EmbeddingConflictError("Current processing evidence is stale.")
            membership = [
                tuple(value)
                for value in session.execute(
                    select(ChunkRecord.id, ChunkRecord.stable_key, ChunkRecord.content_hash)
                    .join(
                        ArtifactProcessingRecord,
                        ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
                    )
                    .where(ArtifactProcessingRecord.processing_run_id == authority.id)
                    .order_by(ArtifactProcessingRecord.locator, ChunkRecord.ordinal, ChunkRecord.id)
                ).all()
            ]
            observed = [(item.chunk_id, item.stable_chunk_key, item.input_hash) for item in rows]
            if membership != observed or membership_digest(rows) != run.membership_digest:
                raise EmbeddingConflictError("Embedding membership changed before publication.")
            latest = (
                session.scalar(
                    select(func.max(EmbeddingRunRecord.revision)).where(
                        EmbeddingRunRecord.project_version_id == run.snapshot_id
                    )
                )
                or 0
            )
            superseded = (
                latest != run.captured_latest_revision
                or version.active_embedding_run_id != run.captured_active_embedding_run_id
            )
            record = EmbeddingRunRecord(
                id=run.id,
                project_id=run.project_id,
                project_version_id=run.snapshot_id,
                processing_run_id=run.processing_run_id,
                processing_revision=run.processing_revision,
                processing_contract=run.processing_contract,
                embedding_contract=asdict(run.contract),
                contract_digest=run.contract.digest,
                dimensions=run.contract.dimensions,
                revision=latest + 1,
                started_at=run.started_at,
                completed_at=run.completed_at,
                outcome=run.outcome,
                superseded=superseded,
                quarantined=False,
                captured_active_embedding_run_id=run.captured_active_embedding_run_id,
                captured_latest_revision=run.captured_latest_revision,
                membership_digest=run.membership_digest,
                limits=run.limits,
                coverage=run.coverage,
                diagnostics=list(run.diagnostics),
            )
            session.add(record)
            session.flush()
            for item in rows:
                if item.values is None:
                    raise EmbeddingConflictError("Embedding candidate is incomplete.")
                session.add(
                    ChunkEmbeddingRecord(
                        id=item.id,
                        embedding_run_id=run.id,
                        project_id=run.project_id,
                        project_version_id=run.snapshot_id,
                        processing_run_id=run.processing_run_id,
                        artifact_processing_id=item.artifact_processing_id,
                        chunk_id=item.chunk_id,
                        stable_chunk_key=item.stable_chunk_key,
                        input_hash=item.input_hash,
                        logical_embedding_key=item.logical_embedding_key,
                        contract_digest=run.contract.digest,
                        dimensions=run.contract.dimensions,
                        embedding=list(item.values),
                        window_count=item.window_count,
                        execution_mode=item.execution_mode,
                        reuse_source_embedding_id=item.reuse_source_embedding_id,
                    )
                )
            session.flush()
            if not superseded and run.outcome == "complete":
                version.active_embedding_run_id = run.id
            return _run(record, len(rows))

    def save_failure(self, run: EmbeddingRun) -> EmbeddingRun:
        with self._session_factory.begin() as session:
            version = session.scalar(
                select(ProjectVersionRecord)
                .where(
                    ProjectVersionRecord.id == run.snapshot_id,
                    ProjectVersionRecord.project_id == run.project_id,
                )
                .with_for_update()
            )
            if version is None:
                raise EmbeddingConflictError("Snapshot disappeared before failure publication.")
            latest = (
                session.scalar(
                    select(func.max(EmbeddingRunRecord.revision)).where(
                        EmbeddingRunRecord.project_version_id == run.snapshot_id
                    )
                )
                or 0
            )
            record = EmbeddingRunRecord(
                id=run.id,
                project_id=run.project_id,
                project_version_id=run.snapshot_id,
                processing_run_id=run.processing_run_id,
                processing_revision=run.processing_revision,
                processing_contract=run.processing_contract,
                embedding_contract=asdict(run.contract),
                contract_digest=run.contract.digest,
                dimensions=run.contract.dimensions,
                revision=latest + 1,
                started_at=run.started_at,
                completed_at=run.completed_at,
                outcome="failed",
                superseded=False,
                quarantined=False,
                captured_active_embedding_run_id=run.captured_active_embedding_run_id,
                captured_latest_revision=run.captured_latest_revision,
                membership_digest=run.membership_digest,
                limits=run.limits,
                coverage=run.coverage,
                diagnostics=list(run.diagnostics),
            )
            session.add(record)
            session.flush()
            return _run(record)

    def search(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run: EmbeddingRun,
        query_vector: tuple[float, ...],
        top_k: int,
        filters: SemanticSearchFilters,
        timeout_ms: int,
    ) -> list[SemanticSearchHit]:
        with self._session_factory.begin() as session:
            timeout = f"{timeout_ms}ms"
            session.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": timeout},
            )
            session.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": timeout},
            )
            version = session.scalar(
                select(ProjectVersionRecord)
                .where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                )
                .with_for_update(read=True)
            )
            if (
                version is None
                or version.active_embedding_run_id != run.id
                or version.active_processing_run_id != run.processing_run_id
            ):
                raise EmbeddingConflictError("Search evidence changed.")
            selected_run = session.get(EmbeddingRunRecord, run.id)
            if (
                selected_run is None
                or selected_run.outcome != "complete"
                or selected_run.superseded
                or selected_run.quarantined
                or selected_run.project_id != project_id
                or selected_run.project_version_id != snapshot_id
                or selected_run.contract_digest != run.contract.digest
                or selected_run.dimensions != run.contract.dimensions
            ):
                raise EmbeddingConflictError("Search embedding authority changed.")
            processing = session.scalar(
                select(ProcessingRunRecord).where(
                    ProcessingRunRecord.id == run.processing_run_id,
                    ProcessingRunRecord.project_version_id == snapshot_id,
                )
            )
            if (
                processing is None
                or processing.quarantined
                or processing.revision != run.processing_revision
                or processing.processing_contract != run.processing_contract
            ):
                raise EmbeddingConflictError("Search processing authority changed.")
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
                cast(dict[str, int], processing.limits),
                cast(dict[str, int], processing.coverage),
                revision=processing.revision,
                quarantined=processing.quarantined,
                superseded=processing.superseded,
            )
            if processing_freshness(
                authority,
                dependencies,
                structural_artifacts_in_session(session, version.active_analysis_id),
            ):
                raise EmbeddingConflictError("Search processing evidence is stale.")
            distance = ChunkEmbeddingRecord.embedding.cosine_distance(list(query_vector))
            query = (
                select(
                    ChunkEmbeddingRecord,
                    (1 - distance).label("score"),
                )
                .join(ChunkRecord, ChunkRecord.id == ChunkEmbeddingRecord.chunk_id)
                .join(
                    ArtifactProcessingRecord,
                    ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
                )
                .join(ArtifactRecord, ArtifactRecord.id == ArtifactProcessingRecord.artifact_id)
                .where(
                    ChunkEmbeddingRecord.embedding_run_id == run.id,
                    ChunkEmbeddingRecord.project_id == project_id,
                    ChunkEmbeddingRecord.project_version_id == snapshot_id,
                    ChunkEmbeddingRecord.contract_digest == run.contract.digest,
                    ChunkEmbeddingRecord.dimensions == run.contract.dimensions,
                )
            )
            if filters.locator_prefix is not None:
                query = query.where(
                    ArtifactProcessingRecord.locator.collate("C").startswith(
                        filters.locator_prefix, autoescape=True
                    )
                )
            if filters.language is not None:
                query = query.where(ArtifactRecord.language == filters.language)
            if filters.kind is not None:
                query = query.where(ChunkRecord.kind == filters.kind)
            ranked = session.execute(
                query.order_by(
                    distance,
                    ChunkEmbeddingRecord.stable_chunk_key.collate("C"),
                    ChunkEmbeddingRecord.chunk_id,
                ).limit(top_k)
            ).all()
            chunk_ids = [row[0].chunk_id for row in ranked]
            hydrated = (
                session.execute(
                    select(
                        ChunkRecord,
                        ArtifactProcessingRecord.locator,
                        ArtifactProcessingRecord.artifact_id,
                        ArtifactProcessingRecord.processing_run_id,
                        ArtifactProcessingRecord.project_version_id,
                    )
                    .join(
                        ArtifactProcessingRecord,
                        ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
                    )
                    .where(
                        ChunkRecord.id.in_(chunk_ids),
                        ArtifactProcessingRecord.processing_run_id == run.processing_run_id,
                        ArtifactProcessingRecord.project_version_id == snapshot_id,
                    )
                ).all()
                if chunk_ids
                else []
            )
            by_id = {row[0].id: _chunk(row) for row in hydrated}
            if len(by_id) != len(ranked):
                raise EmbeddingConflictError("Selected chunks changed during search.")
            return [
                SemanticSearchHit(
                    float(row[1]),
                    _embedding(row[0]),
                    by_id[row[0].chunk_id],
                )
                for row in ranked
            ]
