"""PostgreSQL-native Stage 8 retrieval under one authoritative snapshot lock."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, func, literal_column, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from buildlens.application.embeddings import EmbeddingConflictError
from buildlens.application.processing import processing_freshness
from buildlens.application.retrieval import RetrievalSession
from buildlens.domain.embeddings import ChunkEmbedding, EmbeddingRun
from buildlens.domain.processing import Chunk, ChunkSpan, ProcessingRun
from buildlens.domain.retrieval import (
    CandidatePage,
    ExactCandidate,
    ExactMatch,
    LexicalCandidate,
    LexicalField,
    RetrievalFilters,
    SemanticCandidate,
)
from buildlens.infrastructure.persistence.models import (
    ArtifactProcessingRecord,
    ArtifactRecord,
    ChunkEmbeddingRecord,
    ChunkRecord,
    EmbeddingRunRecord,
    ProcessingRunRecord,
    ProjectVersionRecord,
    SymbolRecord,
)
from buildlens.infrastructure.persistence.processing import structural_artifacts_in_session


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


class SqlAlchemyRetrievalStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def session(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        authority: ProcessingRun,
        embedding_run: EmbeddingRun | None,
        timeout_ms: int,
    ) -> Iterator[RetrievalSession]:
        with self._session_factory.begin() as session:
            timeout = f"{max(1, timeout_ms)}ms"
            session.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": timeout},
            )
            session.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": timeout},
            )
            adapter = _SqlAlchemyRetrievalSession(
                session, project_id, snapshot_id, authority, embedding_run
            )
            adapter.verify()
            yield adapter
            adapter.verify()


class SqlAlchemyContextEvidenceStore(SqlAlchemyRetrievalStore):
    """Short shared-lock sessions used for Stage 9 expansion and final hydration."""

    @contextmanager
    def session(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        authority: ProcessingRun,
        embedding_run: EmbeddingRun | None,
        timeout_ms: int,
    ) -> Iterator[_SqlAlchemyRetrievalSession]:
        with self._session_factory.begin() as session:
            timeout = f"{max(1, timeout_ms)}ms"
            session.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": timeout},
            )
            session.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": timeout},
            )
            adapter = _SqlAlchemyRetrievalSession(
                session, project_id, snapshot_id, authority, embedding_run
            )
            adapter.verify()
            yield adapter
            adapter.verify()


class _SqlAlchemyRetrievalSession:
    def __init__(
        self,
        session: Session,
        project_id: UUID,
        snapshot_id: UUID,
        authority: ProcessingRun,
        embedding_run: EmbeddingRun | None,
    ) -> None:
        self._session = session
        self._project_id = project_id
        self._snapshot_id = snapshot_id
        self._authority = authority
        self._embedding_run = embedding_run

    def verify(self) -> None:
        version = self._session.scalar(
            select(ProjectVersionRecord)
            .where(
                ProjectVersionRecord.id == self._snapshot_id,
                ProjectVersionRecord.project_id == self._project_id,
            )
            .with_for_update(read=True)
        )
        if version is None or version.active_processing_run_id != self._authority.id:
            raise EmbeddingConflictError("Search evidence changed.")
        processing = self._session.scalar(
            select(ProcessingRunRecord).where(
                ProcessingRunRecord.id == self._authority.id,
                ProcessingRunRecord.project_version_id == self._snapshot_id,
            )
        )
        if (
            processing is None
            or processing.quarantined
            or processing.superseded
            or processing.revision != self._authority.revision
            or processing.processing_contract != self._authority.processing_contract
        ):
            raise EmbeddingConflictError("Search processing authority changed.")
        dependencies = list(
            self._session.execute(
                select(
                    ArtifactProcessingRecord.artifact_id,
                    ArtifactProcessingRecord.dependency_fingerprint,
                ).where(
                    ArtifactProcessingRecord.processing_run_id == self._authority.id,
                    ArtifactProcessingRecord.dependency_fingerprint.is_not(None),
                )
            )
        )
        if processing_freshness(
            self._authority,
            cast(list[tuple[UUID, str]], dependencies),
            structural_artifacts_in_session(self._session, version.active_analysis_id),
        ):
            raise EmbeddingConflictError("Search processing evidence is stale.")
        if self._embedding_run is not None:
            run = self._embedding_run
            selected = self._session.get(EmbeddingRunRecord, run.id)
            if (
                version.active_embedding_run_id != run.id
                or selected is None
                or selected.outcome != "complete"
                or selected.superseded
                or selected.quarantined
                or selected.project_id != self._project_id
                or selected.project_version_id != self._snapshot_id
                or selected.processing_run_id != self._authority.id
                or selected.processing_revision != self._authority.revision
                or selected.processing_contract != self._authority.processing_contract
                or selected.contract_digest != run.contract.digest
                or selected.dimensions != run.contract.dimensions
            ):
                raise EmbeddingConflictError("Search embedding authority changed.")

    def exact(self, query: str, filters: RetrievalFilters, depth: int) -> CandidatePage:
        name, qualified, locator = self._exact_expressions(query)
        statement = (
            self._base_select(
                ChunkRecord.id,
                ChunkRecord.stable_key,
                SymbolRecord.name,
                SymbolRecord.qualified_name,
                ArtifactProcessingRecord.locator,
            )
            .where(or_(name, qualified, locator))
            .order_by(ChunkRecord.stable_key.collate("C"), ChunkRecord.id)
            .limit(depth + 1)
        )
        statement = self._filters(statement, filters)
        rows = self._session.execute(statement).all()
        items = tuple(
            ExactCandidate(row[0], row[1], self._matches(query, row[2], row[3], row[4]))
            for row in rows[:depth]
        )
        return CandidatePage(items, len(rows) > depth)

    def lexical(self, query: str, filters: RetrievalFilters, depth: int) -> CandidatePage:
        config: Any = literal_column("'pg_catalog.simple'")
        normalize = func.buildlens_lexical_normalize_v1
        parsed = func.plainto_tsquery(config, normalize(query))
        metadata = func.concat_ws(
            " ",
            func.array_to_string(ChunkRecord.heading_path, " "),
            func.coalesce(ChunkRecord.json_path, ""),
        )
        groups = {
            "content": func.to_tsvector(config, normalize(ChunkRecord.content)),
            "symbol": func.to_tsvector(config, normalize(SymbolRecord.qualified_name)),
            "path": func.to_tsvector(config, normalize(ArtifactProcessingRecord.locator)),
            "metadata": func.to_tsvector(config, normalize(metadata)),
        }
        matches = {key: value.op("@@")(parsed) for key, value in groups.items()}
        scores = {
            key: case((matches[key], func.ts_rank(value, parsed)), else_=0.0)
            for key, value in groups.items()
        }
        lexical_score = func.greatest(*scores.values())
        name, qualified, locator = self._exact_expressions(query)
        exact = func.coalesce(or_(name, qualified, locator), False)
        statement = self._base_select(
            ChunkRecord.id,
            ChunkRecord.stable_key,
            SymbolRecord.name,
            SymbolRecord.qualified_name,
            ArtifactProcessingRecord.locator,
            lexical_score.label("lexical_score"),
            *(matches[key].label(f"{key}_match") for key in groups),
            *(scores[key].label(f"{key}_score") for key in groups),
        ).where(or_(exact, *matches.values()))
        statement = self._filters(statement, filters)
        rows = self._session.execute(
            statement.order_by(
                exact.desc(),
                lexical_score.desc(),
                ChunkRecord.stable_key.collate("C"),
                ChunkRecord.id,
            ).limit(depth + 1)
        ).all()
        items: list[LexicalCandidate] = []
        for row in rows[:depth]:
            field_scores: dict[LexicalField, float] = {}
            for offset, key in enumerate(groups):
                if row[6 + offset]:
                    field_scores[cast(LexicalField, key)] = float(row[10 + offset])
            items.append(
                LexicalCandidate(
                    row[0],
                    row[1],
                    float(row[5]),
                    field_scores,
                    self._matches(query, row[2], row[3], row[4]),
                )
            )
        return CandidatePage(tuple(items), len(rows) > depth)

    def semantic(
        self,
        run: EmbeddingRun,
        query_vector: tuple[float, ...],
        filters: RetrievalFilters,
        depth: int,
    ) -> CandidatePage:
        distance = ChunkEmbeddingRecord.embedding.cosine_distance(list(query_vector))
        columns = (
            ChunkEmbeddingRecord.id,
            ChunkEmbeddingRecord.embedding_run_id,
            ChunkEmbeddingRecord.project_id,
            ChunkEmbeddingRecord.project_version_id,
            ChunkEmbeddingRecord.processing_run_id,
            ChunkEmbeddingRecord.artifact_processing_id,
            ChunkEmbeddingRecord.chunk_id,
            ChunkEmbeddingRecord.stable_chunk_key,
            ChunkEmbeddingRecord.input_hash,
            ChunkEmbeddingRecord.logical_embedding_key,
            ChunkEmbeddingRecord.contract_digest,
            ChunkEmbeddingRecord.dimensions,
            ChunkEmbeddingRecord.window_count,
            ChunkEmbeddingRecord.execution_mode,
            ChunkEmbeddingRecord.reuse_source_embedding_id,
            (1 - distance).label("score"),
        )
        statement = (
            select(*columns)
            .join(ChunkRecord, ChunkRecord.id == ChunkEmbeddingRecord.chunk_id)
            .join(
                ArtifactProcessingRecord,
                ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
            )
            .join(ArtifactRecord, ArtifactRecord.id == ArtifactProcessingRecord.artifact_id)
            .where(
                ChunkEmbeddingRecord.embedding_run_id == run.id,
                ChunkEmbeddingRecord.project_id == self._project_id,
                ChunkEmbeddingRecord.project_version_id == self._snapshot_id,
                ChunkEmbeddingRecord.processing_run_id == self._authority.id,
                ChunkEmbeddingRecord.contract_digest == run.contract.digest,
                ChunkEmbeddingRecord.dimensions == run.contract.dimensions,
            )
        )
        statement = self._filters(statement, filters)
        rows = self._session.execute(
            statement.order_by(
                distance,
                ChunkEmbeddingRecord.stable_chunk_key.collate("C"),
                ChunkEmbeddingRecord.chunk_id,
            ).limit(depth + 1)
        ).all()
        items = tuple(
            SemanticCandidate(
                row[6],
                row[7],
                float(row[15]),
                ChunkEmbedding(
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    cast(Any, row[13]),
                    row[14],
                ),
            )
            for row in rows[:depth]
        )
        return CandidatePage(items, len(rows) > depth)

    def exact_matches(
        self, chunk_ids: list[UUID], query: str
    ) -> dict[UUID, tuple[ExactMatch, ...]]:
        if not chunk_ids:
            return {}
        rows = self._session.execute(
            self._base_select(
                ChunkRecord.id,
                SymbolRecord.name,
                SymbolRecord.qualified_name,
                ArtifactProcessingRecord.locator,
            ).where(ChunkRecord.id.in_(chunk_ids))
        ).all()
        return {
            row[0]: matches
            for row in rows
            if (matches := self._matches(query, row[1], row[2], row[3]))
        }

    def hydrate(self, chunk_ids: list[UUID]) -> dict[UUID, Chunk]:
        if not chunk_ids:
            return {}
        rows = self._session.execute(
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
                ArtifactProcessingRecord.processing_run_id == self._authority.id,
                ArtifactProcessingRecord.project_version_id == self._snapshot_id,
            )
        ).all()
        return {row[0].id: _chunk(row) for row in rows}

    def neighbours(self, seeds: list[Chunk], filters: RetrievalFilters) -> tuple[Chunk, ...]:
        if not seeds:
            return ()
        identities = [
            and_(
                ChunkRecord.artifact_processing_id == seed.artifact_processing_id,
                ChunkRecord.unit_anchor == seed.unit_anchor,
                ChunkRecord.kind == seed.kind,
                ChunkRecord.part_index.in_((seed.part_index - 1, seed.part_index + 1)),
            )
            for seed in seeds
        ]
        statement = self._base_select(
            ChunkRecord,
            ArtifactProcessingRecord.locator,
            ArtifactProcessingRecord.artifact_id,
            ArtifactProcessingRecord.processing_run_id,
            ArtifactProcessingRecord.project_version_id,
        ).where(or_(*identities))
        statement = self._filters(statement, filters)
        rows = self._session.execute(
            statement.order_by(
                ChunkRecord.artifact_processing_id,
                ChunkRecord.unit_anchor.collate("C"),
                ChunkRecord.part_index,
                ChunkRecord.stable_key.collate("C"),
                ChunkRecord.id,
            )
        ).all()
        return tuple(_chunk(row) for row in rows)

    def _base_select(self, *columns: Any) -> Any:
        return (
            select(*columns)
            .select_from(ChunkRecord)
            .join(
                ArtifactProcessingRecord,
                ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
            )
            .join(ArtifactRecord, ArtifactRecord.id == ArtifactProcessingRecord.artifact_id)
            .outerjoin(
                SymbolRecord,
                and_(
                    SymbolRecord.id == ChunkRecord.symbol_id,
                    SymbolRecord.artifact_analysis_id
                    == ChunkRecord.structural_artifact_analysis_id,
                ),
            )
            .where(
                ArtifactProcessingRecord.processing_run_id == self._authority.id,
                ArtifactProcessingRecord.project_version_id == self._snapshot_id,
                ArtifactRecord.project_id == self._project_id,
            )
        )

    @staticmethod
    def _filters(statement: Any, filters: RetrievalFilters) -> Any:
        if filters.locator_prefix is not None:
            statement = statement.where(
                ArtifactProcessingRecord.locator.collate("C").startswith(
                    filters.locator_prefix, autoescape=True
                )
            )
        if filters.language is not None:
            statement = statement.where(ArtifactRecord.language == filters.language)
        if filters.kind is not None:
            statement = statement.where(ChunkRecord.kind == filters.kind)
        return statement

    @staticmethod
    def _exact_expressions(query: str) -> tuple[Any, Any, Any]:
        return (
            SymbolRecord.name.collate("C") == query,
            SymbolRecord.qualified_name.collate("C") == query,
            ArtifactProcessingRecord.locator.collate("C") == query,
        )

    @staticmethod
    def _matches(
        query: str, name: str | None, qualified_name: str | None, locator: str
    ) -> tuple[ExactMatch, ...]:
        values: list[ExactMatch] = []
        if name == query:
            values.append(ExactMatch("symbol_name", name))
        if qualified_name == query:
            values.append(ExactMatch("symbol_qualified_name", qualified_name))
        if locator == query:
            values.append(ExactMatch("locator", locator))
        return tuple(values)
