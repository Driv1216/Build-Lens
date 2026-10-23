"""Mode orchestration and deterministic Stage 8 candidate fusion."""

from __future__ import annotations

import hashlib
import math
import time
from array import array
from contextlib import AbstractContextManager
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Protocol, cast
from uuid import UUID

from buildlens.application.embeddings import (
    EmbeddingBusyError,
    EmbeddingConflictError,
    EmbeddingInputError,
    EmbeddingPreparationRequiredError,
    EmbeddingRuntime,
    EmbeddingStore,
    EmbeddingUnavailableError,
)
from buildlens.application.processing import (
    CurrentContentUnavailableError,
    ProcessingConflictError,
    ProcessingService,
)
from buildlens.domain.embeddings import EmbeddingRun, EmbeddingVector
from buildlens.domain.processing import Chunk, ProcessingRun
from buildlens.domain.retrieval import (
    CANDIDATE_DEPTH,
    RRF_CONSTANT,
    CandidatePage,
    ComponentStatus,
    ExactCandidate,
    ExactMatch,
    LexicalCandidate,
    RankedComponent,
    RetrievalFilters,
    RetrievalHit,
    RetrievalMode,
    RetrievalResult,
    ScoreKind,
    SemanticCandidate,
)


class RetrievalSession(Protocol):
    def exact(self, query: str, filters: RetrievalFilters, depth: int) -> CandidatePage: ...
    def lexical(self, query: str, filters: RetrievalFilters, depth: int) -> CandidatePage: ...
    def semantic(
        self,
        run: EmbeddingRun,
        query_vector: tuple[float, ...],
        filters: RetrievalFilters,
        depth: int,
    ) -> CandidatePage: ...
    def exact_matches(
        self, chunk_ids: list[UUID], query: str
    ) -> dict[UUID, tuple[ExactMatch, ...]]: ...
    def hydrate(self, chunk_ids: list[UUID]) -> dict[UUID, Chunk]: ...


class RetrievalStore(Protocol):
    def session(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        authority: ProcessingRun,
        embedding_run: EmbeddingRun | None,
        timeout_ms: int,
    ) -> AbstractContextManager[RetrievalSession]: ...


@dataclass(frozen=True, slots=True)
class _Ranked:
    chunk_id: UUID
    stable_key: str
    exact_matches: tuple[ExactMatch, ...]
    exact_rank: int | None
    lexical: RankedComponent | None
    semantic: RankedComponent | None
    score: Fraction | float | None


class RetrievalService:
    def __init__(
        self,
        processing: ProcessingService,
        runtime: EmbeddingRuntime,
        embeddings: EmbeddingStore,
        store: RetrievalStore,
        *,
        search_timeout_seconds: float = 15,
    ) -> None:
        if search_timeout_seconds <= 0:
            raise ValueError("Retrieval timeout must be positive.")
        self._processing = processing
        self._runtime = runtime
        self._embeddings = embeddings
        self._store = store
        self._timeout = search_timeout_seconds

    def search(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        mode: RetrievalMode,
        query: str,
        top_k: int = 10,
        filters: RetrievalFilters | None = None,
    ) -> RetrievalResult:
        return self._search(project_id, snapshot_id, mode, query, top_k, filters, max_top_k=50)

    def context_candidates(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        mode: RetrievalMode,
        query: str,
        filters: RetrievalFilters | None = None,
    ) -> RetrievalResult:
        """Return the shared Stage 8 ranking at the bounded Stage 9 depth."""
        return self._search(
            project_id,
            snapshot_id,
            mode,
            query,
            CANDIDATE_DEPTH,
            filters,
            max_top_k=CANDIDATE_DEPTH,
        )

    def _search(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        mode: RetrievalMode,
        query: str,
        top_k: int,
        filters: RetrievalFilters | None,
        *,
        max_top_k: int,
    ) -> RetrievalResult:
        selected_query = self._validate(
            mode, query, top_k, filters or RetrievalFilters(), max_top_k=max_top_k
        )
        selected_filters = filters or RetrievalFilters()
        deadline = time.monotonic() + self._timeout
        run: EmbeddingRun | None = None
        query_vector: tuple[float, ...] | None = None
        locked = False
        try:
            authority = self._processing.current_authority(project_id, snapshot_id)
            if mode in {"semantic", "hybrid"}:
                if not self._runtime.lock.acquire(False):
                    raise EmbeddingBusyError("Another embedding operation is running.")
                locked = True
                run = self._require_embedding_run(project_id, snapshot_id, authority)
                self._check_deadline(deadline)
                remaining = max(0.001, deadline - time.monotonic())
                bounded_query = getattr(self._runtime.provider, "embed_query_with_timeout", None)
                vector = (
                    cast(Any, bounded_query)(selected_query, remaining)
                    if callable(bounded_query)
                    else self._runtime.provider.embed_query(selected_query)
                )
                query_vector = self._float32_vector(vector)
                self._check_deadline(deadline)
            timeout_ms = max(1, int((deadline - time.monotonic()) * 1000))
            with self._store.session(
                project_id, snapshot_id, authority, run, timeout_ms
            ) as retrieval:
                exact_page: CandidatePage | None = None
                lexical_page: CandidatePage | None = None
                semantic_page: CandidatePage | None = None
                hybrid_exact_count: int | None = None
                if mode == "exact":
                    exact_page = retrieval.exact(selected_query, selected_filters, CANDIDATE_DEPTH)
                    ranked = self._rank_exact(exact_page)
                elif mode == "lexical":
                    lexical_page = retrieval.lexical(
                        selected_query, selected_filters, CANDIDATE_DEPTH
                    )
                    ranked = self._rank_lexical(lexical_page)
                elif mode == "semantic":
                    assert run is not None and query_vector is not None
                    semantic_page = retrieval.semantic(
                        run, query_vector, selected_filters, CANDIDATE_DEPTH
                    )
                    ranked = self._rank_semantic(semantic_page)
                else:
                    assert run is not None and query_vector is not None
                    lexical_page = retrieval.lexical(
                        selected_query, selected_filters, CANDIDATE_DEPTH
                    )
                    semantic_page = retrieval.semantic(
                        run, query_vector, selected_filters, CANDIDATE_DEPTH
                    )
                    union_ids = list(
                        dict.fromkeys(
                            [item.chunk_id for item in lexical_page.items]
                            + [item.chunk_id for item in semantic_page.items]
                        )
                    )
                    union_exact = retrieval.exact_matches(union_ids, selected_query)
                    hybrid_exact_count = len(union_exact)
                    ranked = self._rank_hybrid(lexical_page, semantic_page, union_exact)
                selected = ranked[:top_k]
                chunks = retrieval.hydrate([item.chunk_id for item in selected])
                if len(chunks) != len(selected):
                    raise EmbeddingConflictError("Selected chunks changed during search.")
                hits = tuple(
                    self._hit(index, item, chunks[item.chunk_id], authority, mode)
                    for index, item in enumerate(selected, 1)
                )
            self._require_authority(project_id, snapshot_id, authority)
            self._check_deadline(deadline)
            return RetrievalResult(
                requested_mode=mode,
                executed_mode=mode,
                project_id=project_id,
                snapshot_id=snapshot_id,
                processing_run=authority,
                embedding_run=run,
                exact=self._component(
                    exact_page or lexical_page,
                    requested=mode in {"exact", "lexical", "hybrid"},
                    count=(
                        hybrid_exact_count
                        if mode == "hybrid"
                        else (
                            sum(
                                bool(cast(LexicalCandidate, item).exact_matches)
                                for item in lexical_page.items
                            )
                            if lexical_page is not None and mode == "lexical"
                            else None
                        )
                    ),
                ),
                lexical=self._component(lexical_page, requested=mode in {"lexical", "hybrid"}),
                semantic=self._component(semantic_page, requested=mode in {"semantic", "hybrid"}),
                returned_count=len(hits),
                hits=hits,
                final_candidate_cap_truncated=len(ranked) > top_k,
            )
        except (CurrentContentUnavailableError, ProcessingConflictError) as error:
            raise EmbeddingConflictError(
                "Current processing evidence changed during search."
            ) from error
        finally:
            if locked:
                self._runtime.lock.release()

    def _require_embedding_run(
        self, project_id: UUID, snapshot_id: UUID, authority: ProcessingRun
    ) -> EmbeddingRun:
        run = self._embeddings.active(project_id, snapshot_id)
        if (
            run is None
            or run.outcome != "complete"
            or run.superseded
            or run.quarantined
            or run.processing_run_id != authority.id
            or run.processing_revision != authority.revision
            or run.processing_contract != authority.processing_contract
            or run.contract.digest != self._runtime.provider.contract.digest
        ):
            raise EmbeddingPreparationRequiredError(
                "Prepare embeddings for the current snapshot before searching."
            )
        return run

    def _require_authority(
        self, project_id: UUID, snapshot_id: UUID, expected: ProcessingRun
    ) -> None:
        observed = self._processing.current_authority(project_id, snapshot_id)
        if (
            observed.id != expected.id
            or observed.revision != expected.revision
            or observed.processing_contract != expected.processing_contract
        ):
            raise EmbeddingConflictError("Current processing evidence changed during search.")

    @staticmethod
    def _rank_exact(page: CandidatePage) -> list[_Ranked]:
        return [
            _Ranked(item.chunk_id, item.stable_key, item.matches, rank, None, None, None)
            for rank, raw in enumerate(page.items, 1)
            for item in [cast(ExactCandidate, raw)]
        ]

    @staticmethod
    def _rank_lexical(page: CandidatePage) -> list[_Ranked]:
        return [
            _Ranked(
                item.chunk_id,
                item.stable_key,
                item.exact_matches,
                None,
                RankedComponent(rank, item.score, item.field_scores),
                None,
                item.score,
            )
            for rank, raw in enumerate(page.items, 1)
            for item in [cast(LexicalCandidate, raw)]
        ]

    @staticmethod
    def _rank_semantic(page: CandidatePage) -> list[_Ranked]:
        return [
            _Ranked(
                item.chunk_id,
                item.stable_key,
                (),
                None,
                None,
                RankedComponent(rank, item.score, embedding=item.embedding),
                item.score,
            )
            for rank, raw in enumerate(page.items, 1)
            for item in [cast(SemanticCandidate, raw)]
        ]

    @staticmethod
    def _rank_hybrid(
        lexical_page: CandidatePage,
        semantic_page: CandidatePage,
        exact: dict[UUID, tuple[ExactMatch, ...]],
    ) -> list[_Ranked]:
        values: dict[UUID, _Ranked] = {}
        for rank, raw in enumerate(lexical_page.items, 1):
            lexical_item = cast(LexicalCandidate, raw)
            if lexical_item.chunk_id in values:
                continue
            values[lexical_item.chunk_id] = _Ranked(
                lexical_item.chunk_id,
                lexical_item.stable_key,
                exact.get(lexical_item.chunk_id, lexical_item.exact_matches),
                None,
                RankedComponent(rank, lexical_item.score, lexical_item.field_scores),
                None,
                Fraction(1, RRF_CONSTANT + rank),
            )
        seen_semantic: set[UUID] = set()
        for rank, raw in enumerate(semantic_page.items, 1):
            semantic_item = cast(SemanticCandidate, raw)
            if semantic_item.chunk_id in seen_semantic:
                continue
            seen_semantic.add(semantic_item.chunk_id)
            previous = values.get(semantic_item.chunk_id)
            contribution = Fraction(1, RRF_CONSTANT + rank)
            values[semantic_item.chunk_id] = _Ranked(
                semantic_item.chunk_id,
                semantic_item.stable_key,
                exact.get(semantic_item.chunk_id, ()),
                None,
                previous.lexical if previous else None,
                RankedComponent(rank, semantic_item.score, embedding=semantic_item.embedding),
                cast(Fraction, previous.score) + contribution if previous else contribution,
            )
        ordered_exact = sorted(
            (item for item in values.values() if item.exact_matches),
            key=lambda item: (item.stable_key, str(item.chunk_id)),
        )
        exact_ranks = {item.chunk_id: rank for rank, item in enumerate(ordered_exact, 1)}
        ranked = [
            _Ranked(
                item.chunk_id,
                item.stable_key,
                item.exact_matches,
                exact_ranks.get(item.chunk_id),
                item.lexical,
                item.semantic,
                item.score,
            )
            for item in values.values()
        ]
        return sorted(
            ranked,
            key=lambda item: (
                not bool(item.exact_matches),
                -cast(Fraction, item.score),
                item.stable_key,
                str(item.chunk_id),
            ),
        )

    @staticmethod
    def _hit(
        rank: int,
        item: _Ranked,
        chunk: Chunk,
        authority: ProcessingRun,
        mode: RetrievalMode,
    ) -> RetrievalHit:
        if (
            chunk.id != item.chunk_id
            or chunk.run_id != authority.id
            or chunk.snapshot_id != authority.project_version_id
            or chunk.artifact_id is None
            or chunk.locator is None
            or hashlib.sha256(chunk.content.encode("utf-8")).hexdigest() != chunk.content_hash
        ):
            raise EmbeddingConflictError("Selected chunk provenance changed during search.")
        if item.semantic and item.semantic.embedding:
            embedding = item.semantic.embedding
            if (
                embedding.chunk_id != chunk.id
                or embedding.stable_chunk_key != chunk.stable_key
                or embedding.input_hash != chunk.content_hash
            ):
                raise EmbeddingConflictError("Selected embedding provenance changed during search.")
        score_kind = {"exact": "exact", "lexical": "lexical", "semantic": "cosine"}.get(mode, "rrf")
        score = float(item.score) if item.score is not None else None
        lexical_contribution = (
            1 / (RRF_CONSTANT + item.lexical.rank) if mode == "hybrid" and item.lexical else 0.0
        )
        semantic_contribution = (
            1 / (RRF_CONSTANT + item.semantic.rank) if mode == "hybrid" and item.semantic else 0.0
        )
        return RetrievalHit(
            rank,
            cast(ScoreKind, score_kind),
            score,
            chunk,
            item.exact_matches,
            item.exact_rank,
            item.lexical,
            item.semantic,
            lexical_contribution,
            semantic_contribution,
        )

    @staticmethod
    def _component(
        page: CandidatePage | None, *, requested: bool, count: int | None = None
    ) -> ComponentStatus:
        if not requested:
            return ComponentStatus("not_requested")
        return ComponentStatus(
            "completed",
            count if count is not None else (page.count if page else 0),
            page.truncated if page else False,
        )

    @staticmethod
    def _validate(
        mode: RetrievalMode,
        query: str,
        top_k: int,
        filters: RetrievalFilters,
        *,
        max_top_k: int = 50,
    ) -> str:
        if mode not in {"exact", "lexical", "semantic", "hybrid"}:
            raise EmbeddingInputError("Search mode is invalid.")
        selected = query if mode == "exact" else query.strip()
        if not query.strip():
            raise EmbeddingInputError("Query must not be empty.")
        if len(selected.encode("utf-8")) > 4096:
            raise EmbeddingInputError("Query exceeds 4,096 UTF-8 bytes.")
        if not 1 <= top_k <= max_top_k:
            raise EmbeddingInputError(f"top_k must be between 1 and {max_top_k}.")
        for value, limit in (
            (filters.locator_prefix, 2048),
            (filters.language, 64),
            (filters.kind, 32),
        ):
            if value is not None and (not value or len(value.encode("utf-8")) > limit):
                raise EmbeddingInputError("Search filter is invalid.")
        return selected

    def _float32_vector(self, vector: EmbeddingVector) -> tuple[float, ...]:
        values = tuple(array("f", vector.values))
        if vector.window_count < 1 or len(values) != self._runtime.provider.contract.dimensions:
            raise EmbeddingUnavailableError("Embedding provider returned malformed output.")
        if any(type(value) is not float or not math.isfinite(value) for value in values):
            raise EmbeddingUnavailableError("Embedding provider returned malformed output.")
        norm = math.sqrt(math.fsum(value * value for value in values))
        if norm == 0 or not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
            raise EmbeddingUnavailableError("Embedding provider returned malformed output.")
        return values

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise EmbeddingUnavailableError("Search timed out.")
