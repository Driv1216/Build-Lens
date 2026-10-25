"""Bounded Stage 9 evidence ranking, expansion, and whole-item packing."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Protocol
from uuid import UUID

from buildlens.application.embeddings import EmbeddingConflictError, ModelCacheStatus
from buildlens.application.observation import capture, emit
from buildlens.application.retrieval import RetrievalService
from buildlens.domain.context import (
    CONTEXT_BYTE_LIMIT,
    CONTEXT_ITEM_LIMIT,
    CONTEXT_RESPONSE_BYTE_LIMIT,
    CONTEXT_SOURCE_BYTE_LIMIT,
    EXPANDED_OBSERVATION_LIMIT,
    RERANK_CANDIDATE_DEPTH,
    SEED_COUNT,
    CandidateObservation,
    ContextBudget,
    ContextDecision,
    ContextEvidenceItem,
    ContextMeasurement,
    ContextPolicy,
    ContextResult,
    ContextRetrievalSummary,
    DecisionDisposition,
    RankingObservation,
    RankingPolicy,
    RerankerContract,
    RerankerState,
    RerankResult,
)
from buildlens.domain.embeddings import EmbeddingRun
from buildlens.domain.processing import Chunk, ProcessingRun
from buildlens.domain.retrieval import RetrievalFilters, RetrievalHit, RetrievalMode


class ContextBusyError(Exception):
    pass


class ContextUnavailableError(Exception):
    pass


class ContextConflictError(Exception):
    pass


class ContextInputError(ValueError):
    pass


class ContextLimitError(ValueError):
    pass


class Reranker(Protocol):
    @property
    def contract(self) -> RerankerContract: ...
    def status(self) -> ModelCacheStatus: ...
    def score(
        self, query: str, candidates: list[Chunk], timeout_seconds: float
    ) -> list[RerankResult]: ...
    def close(self) -> None: ...


@dataclass(slots=True)
class RerankerRuntime:
    provider: Reranker
    lock: Lock = field(default_factory=Lock)

    def close(self) -> None:
        self.provider.close()


class ContextEvidenceSession(Protocol):
    def neighbours(self, seeds: list[Chunk], filters: RetrievalFilters) -> tuple[Chunk, ...]: ...
    def hydrate(self, chunk_ids: list[UUID]) -> dict[UUID, Chunk]: ...


class ContextEvidenceStore(Protocol):
    def session(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        authority: ProcessingRun,
        embedding_run: EmbeddingRun | None,
        timeout_ms: int,
    ) -> AbstractContextManager[ContextEvidenceSession]: ...


class ContextSizer:
    """Measure the exact canonical JSON evidence payload, including framing."""

    measurement = ContextMeasurement()

    @staticmethod
    def _chunk(value: Chunk) -> dict[str, object]:
        return {
            "artifact_id": str(value.artifact_id),
            "artifact_processing_id": str(value.artifact_processing_id),
            "chunk_id": str(value.id),
            "content": value.content,
            "content_hash": value.content_hash,
            "heading_path": list(value.heading_path),
            "json_path": value.json_path,
            "kind": value.kind,
            "locator": value.locator,
            "ordinal": value.ordinal,
            "part_index": value.part_index,
            "span": {
                "end_column": value.span.end_column,
                "end_line": value.span.end_line,
                "end_offset": value.span.end_offset,
                "start_column": value.span.start_column,
                "start_line": value.span.start_line,
                "start_offset": value.span.start_offset,
            },
            "stable_key": value.stable_key,
            "symbol_id": str(value.symbol_id) if value.symbol_id else None,
            "symbol_identity_id": (
                str(value.symbol_identity_id) if value.symbol_identity_id else None
            ),
            "unit_anchor": value.unit_anchor,
        }

    def serialize(self, query: str, chunks: list[Chunk]) -> bytes:
        return json.dumps(
            {"evidence": [self._chunk(item) for item in chunks], "query": query},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def measure(self, query: str, chunks: list[Chunk]) -> int:
        return len(self.serialize(query, chunks))

    def budget(self, query: str, chunks: list[Chunk], limit: int) -> ContextBudget:
        framing = self.measure(query, [])
        previous = framing
        costs: list[tuple[UUID, int]] = []
        included: list[Chunk] = []
        for chunk in chunks:
            included.append(chunk)
            current = self.measure(query, included)
            costs.append((chunk.id, current - previous))
            previous = current
        return ContextBudget(
            self.measurement,
            limit,
            previous,
            framing,
            limit - previous,
            tuple(costs),
        )


@dataclass(slots=True)
class _RankedCandidate:
    hit: RetrievalHit
    rerank: RerankResult | None = None
    rerank_rank: int | None = None


@dataclass(slots=True)
class _Draft:
    chunk: Chunk
    candidate: _RankedCandidate | None
    seed: bool
    parents: set[UUID] = field(default_factory=set)


def _supported_parents(parents: Iterable[UUID], included_seeds: set[UUID]) -> set[UUID]:
    """Keep expansion tied to an admitted seed in both packing passes."""
    return set(parents) & included_seeds


class ContextService:
    def __init__(
        self,
        retrieval: RetrievalService,
        runtime: RerankerRuntime,
        store: ContextEvidenceStore,
        *,
        sizer: ContextSizer | None = None,
        request_timeout_seconds: float = 120,
        rerank_timeout_seconds: float = 45,
        byte_limit: int = CONTEXT_BYTE_LIMIT,
        item_limit: int = CONTEXT_ITEM_LIMIT,
        source_byte_limit: int = CONTEXT_SOURCE_BYTE_LIMIT,
        response_byte_limit: int = CONTEXT_RESPONSE_BYTE_LIMIT,
    ) -> None:
        if (
            min(
                request_timeout_seconds,
                rerank_timeout_seconds,
                byte_limit,
                item_limit,
                source_byte_limit,
                response_byte_limit,
            )
            <= 0
        ):
            raise ValueError("Context limits must be positive.")
        self._retrieval = retrieval
        self._runtime = runtime
        self._store = store
        self._sizer = sizer or ContextSizer()
        self._request_timeout = request_timeout_seconds
        self._rerank_timeout = rerank_timeout_seconds
        self._byte_limit = byte_limit
        self._item_limit = item_limit
        self._source_byte_limit = source_byte_limit
        self._response_byte_limit = response_byte_limit

    def status(self) -> tuple[ModelCacheStatus, RerankerContract]:
        return self._runtime.provider.status(), self._runtime.provider.contract

    def verify_result(
        self,
        result: ContextResult,
        items: tuple[ContextEvidenceItem, ...] | None = None,
        *,
        deadline: float | None = None,
    ) -> None:
        """Recheck publication authority and exact source records in a short session."""
        selected = result.items if items is None else items
        original = {item.chunk.id: item for item in result.items}
        if any(original.get(item.chunk.id) != item for item in selected):
            raise ContextConflictError("Supplied evidence is outside the context package.")
        self._final_verify(
            result.project_id,
            result.snapshot_id,
            result.processing_run,
            result.embedding_run,
            [_Draft(item.chunk, None, item.selected_as_seed) for item in selected],
            deadline or time.monotonic() + 10,
        )

    async def repack(
        self,
        result: ContextResult,
        fits: Callable[[tuple[ContextEvidenceItem, ...]], Awaitable[bool]],
    ) -> tuple[tuple[ContextEvidenceItem, ...], tuple[ContextDecision, ...]]:
        """Pack only existing whole evidence items, preserving seed support and order."""
        selected: list[ContextEvidenceItem] = []
        decisions: list[ContextDecision] = []
        surviving_seeds: set[UUID] = set()
        for item in result.items:
            chunk = item.chunk
            if not item.selected_as_seed and not _supported_parents(
                item.expanded_from, surviving_seeds
            ):
                reason = "parent_not_included"
            elif await fits((*selected, item)):
                selected.append(item)
                if item.selected_as_seed:
                    surviving_seeds.add(chunk.id)
                continue
            else:
                reason = "generation_budget"
            decisions.append(
                ContextDecision(
                    chunk.id,
                    chunk.stable_key,
                    str(chunk.locator),
                    "generation_packing",
                    "excluded",
                    reason,
                    item.expanded_from,
                    item.retrieval.rank if item.retrieval else None,
                    item.rerank_rank,
                    item.rerank_score,
                )
            )
        return tuple(selected), tuple(decisions)

    def build(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        query: str,
        retrieval_mode: RetrievalMode,
        ranking_policy: RankingPolicy,
        filters: RetrievalFilters | None = None,
    ) -> ContextResult:
        if ranking_policy not in {"local_cross_encoder", "retrieval_order"}:
            raise ContextInputError("Ranking policy is invalid.")
        if self._sizer.measure(query, []) > self._byte_limit:
            raise ContextInputError("Query cannot fit the context evidence allowance.")
        deadline = time.monotonic() + self._request_timeout
        selected_filters = filters or RetrievalFilters()
        try:
            retrieved = self._retrieval.context_candidates(
                project_id,
                snapshot_id,
                retrieval_mode,
                query,
                selected_filters,
            )
        except EmbeddingConflictError as error:
            raise ContextConflictError("Retrieval evidence changed.") from error
        emit("retrieval", retrieved)
        self._check_deadline(deadline)
        decisions: list[ContextDecision] = []
        candidates = self._deduplicate_and_validate(retrieved.hits, decisions)
        self._check_source_bound(item.hit.chunk for item in candidates)

        eligible: list[_RankedCandidate] = []
        for candidate in candidates:
            if self._sizer.measure(query, [candidate.hit.chunk]) > self._byte_limit:
                decisions.append(
                    self._decision(candidate, "filter", "excluded", "item_exceeds_budget")
                )
            else:
                eligible.append(candidate)

        reranker_state: RerankerState
        if ranking_policy == "retrieval_order":
            reranker_state = "not_requested"
            ranked = eligible
        elif not eligible:
            reranker_state = "not_needed"
            ranked = []
        else:
            reranker_state = "completed"
            ranked = self._rerank(query, eligible, deadline, decisions)

        observation = capture(
            "ranking",
            lambda: self._observe_ranking(retrieved.hits, eligible, ranked, decisions),
        )
        seeds = ranked[:SEED_COUNT]
        for candidate in ranked[SEED_COUNT:]:
            decisions.append(
                self._decision(candidate, "seed_selection", "excluded", "outside_seed_count")
            )

        neighbours, parent_edges = self._expand(
            project_id,
            snapshot_id,
            retrieved.processing_run,
            retrieved.embedding_run,
            seeds,
            selected_filters,
            deadline,
        )
        self._check_source_bound(
            [candidate.hit.chunk for candidate in candidates] + list(neighbours)
        )

        packed: list[_Draft] = []
        included_ids: set[UUID] = set()
        for candidate in seeds:
            draft = _Draft(candidate.hit.chunk, candidate, True)
            if self._try_pack(query, packed, draft):
                included_ids.add(draft.chunk.id)
                decisions.append(self._decision(candidate, "packing", "included", "included_seed"))
            else:
                reason = (
                    "item_count_limit" if len(packed) >= self._item_limit else "remaining_budget"
                )
                decisions.append(self._decision(candidate, "packing", "excluded", reason))

        direct = {candidate.hit.chunk.id: candidate for candidate in candidates}
        neighbour_order = sorted(
            neighbours,
            key=lambda chunk: (
                min(
                    (self._seed_rank(parent, seeds) for parent in parent_edges[chunk.id]),
                    default=SEED_COUNT + 1,
                ),
                chunk.part_index,
                chunk.stable_key,
                str(chunk.id),
            ),
        )[:EXPANDED_OBSERVATION_LIMIT]
        packed_by_id = {item.chunk.id: item for item in packed}
        for chunk in neighbour_order:
            parents = parent_edges[chunk.id]
            supported = _supported_parents(parents, included_ids)
            direct_candidate = direct.get(chunk.id)
            if not supported:
                decisions.append(
                    self._chunk_decision(
                        chunk,
                        direct_candidate,
                        "expansion",
                        "excluded",
                        "parent_not_included",
                        parents,
                    )
                )
                continue
            existing = packed_by_id.get(chunk.id)
            if existing:
                existing.parents.update(supported)
                continue
            draft = _Draft(chunk, direct_candidate, False, set(supported))
            if self._sizer.measure(query, [chunk]) > self._byte_limit:
                reason = "item_exceeds_budget"
            elif self._try_pack(query, packed, draft):
                included_ids.add(chunk.id)
                packed_by_id[chunk.id] = draft
                decisions.append(
                    self._chunk_decision(
                        chunk,
                        direct_candidate,
                        "packing",
                        "included",
                        "included_neighbour",
                        supported,
                    )
                )
                continue
            else:
                reason = (
                    "item_count_limit" if len(packed) >= self._item_limit else "remaining_budget"
                )
            decisions.append(
                self._chunk_decision(
                    chunk, direct_candidate, "packing", "excluded", reason, supported
                )
            )

        self._final_verify(
            project_id,
            snapshot_id,
            retrieved.processing_run,
            retrieved.embedding_run,
            packed,
            deadline,
        )
        chunks = [item.chunk for item in packed]
        budget = self._sizer.budget(query, chunks, self._byte_limit)
        costs = dict(budget.per_item_costs)
        items = tuple(self._evidence(item, costs[item.chunk.id]) for item in packed)
        summary = ContextRetrievalSummary(
            retrieved.requested_mode,
            retrieved.executed_mode,
            retrieved.returned_count,
            100,
            retrieved.final_candidate_cap_truncated
            or retrieved.exact.truncated
            or retrieved.lexical.truncated
            or retrieved.semantic.truncated,
            retrieved.exact.candidate_count,
            retrieved.lexical.candidate_count,
            retrieved.semantic.candidate_count,
        )
        result = ContextResult(
            project_id,
            snapshot_id,
            query,
            ranking_policy,
            ContextPolicy(
                byte_limit=self._byte_limit,
                item_limit=self._item_limit,
                request_deadline_seconds=self._request_timeout,
                rerank_deadline_seconds=self._rerank_timeout,
                hydrated_source_byte_limit=self._source_byte_limit,
                response_byte_limit=self._response_byte_limit,
            ),
            retrieved.processing_run,
            retrieved.embedding_run,
            summary,
            reranker_state,
            self._runtime.provider.contract if reranker_state == "completed" else None,
            items,
            tuple(decisions),
            budget,
        )
        if len(self._diagnostic_projection(result)) > self._response_byte_limit:
            raise ContextLimitError("Context response exceeds its serialized-size limit.")
        result = replace(result, ranking_observation=observation)
        emit("context", result)
        return result

    @staticmethod
    def _observe_ranking(
        hits: tuple[RetrievalHit, ...],
        eligible: list[_RankedCandidate],
        ranked: list[_RankedCandidate],
        decisions: list[ContextDecision],
    ) -> RankingObservation:
        # Copy actual output order; do not sort or reproduce ranking decisions.
        orders = {item.hit.chunk.id: i for i, item in enumerate(ranked, 1)}
        eligible_ids = {item.hit.chunk.id for item in eligible}
        candidates = {item.hit.chunk.id: item for item in eligible}
        entries = []
        for hit in hits:
            chunk = hit.chunk
            candidate = candidates.get(chunk.id)
            rerank = candidate.rerank if candidate else None
            semantic = hit.semantic
            entries.append(
                CandidateObservation(
                    chunk.id,
                    chunk.artifact_id,
                    chunk.artifact_processing_id,
                    chunk.symbol_id,
                    chunk.symbol_identity_id,
                    chunk.locator,
                    chunk.stable_key,
                    chunk.content_hash,
                    chunk.span,
                    hit.rank,
                    hit.score_kind,
                    hit.score,
                    hit.exact_rank,
                    hit.lexical.rank if hit.lexical else None,
                    hit.lexical.score if hit.lexical else None,
                    semantic.rank if semantic else None,
                    semantic.score if semantic else None,
                    chunk.id in eligible_ids,
                    tuple(
                        d.reason
                        for d in decisions
                        if d.chunk_id == chunk.id and d.disposition == "excluded"
                    ),
                    orders.get(chunk.id),
                    candidate.rerank_rank if candidate else None,
                    rerank.score if rerank else None,
                    rerank.window_count if rerank else None,
                    rerank.winning_window if rerank else None,
                    semantic.embedding.id if semantic and semantic.embedding else None,
                )
            )
        return RankingObservation("stage11.observation.v1", 100, 32, tuple(entries))

    def _rerank(
        self,
        query: str,
        candidates: list[_RankedCandidate],
        deadline: float,
        decisions: list[ContextDecision],
    ) -> list[_RankedCandidate]:
        admitted = candidates[:RERANK_CANDIDATE_DEPTH]
        for candidate in candidates[RERANK_CANDIDATE_DEPTH:]:
            decisions.append(
                self._decision(candidate, "rerank_admission", "excluded", "outside_rerank_depth")
            )
        if not self._runtime.lock.acquire(False):
            raise ContextBusyError("Another reranking operation is running.")
        try:
            timeout = min(self._rerank_timeout, max(0.001, deadline - time.monotonic()))
            values = self._runtime.provider.score(
                query, [candidate.hit.chunk for candidate in admitted], timeout
            )
        finally:
            self._runtime.lock.release()
        self._check_deadline(deadline)
        expected = {candidate.hit.chunk.id for candidate in admitted}
        if len(values) != len(admitted) or {value.chunk_id for value in values} != expected:
            raise ContextUnavailableError("Reranker returned malformed candidate associations.")
        by_id = {value.chunk_id: value for value in values}
        scored: list[_RankedCandidate] = []
        for candidate in admitted:
            value = by_id[candidate.hit.chunk.id]
            if value.exclusion_reason:
                if (
                    value.exclusion_reason != "reranker_input_limit"
                    or value.score is not None
                    or value.window_count != 0
                ):
                    raise ContextUnavailableError("Reranker returned malformed output.")
                candidate.rerank = value
                decisions.append(
                    self._decision(candidate, "reranking", "excluded", value.exclusion_reason)
                )
                continue
            if (
                type(value.score) is not float
                or not math.isfinite(value.score)
                or type(value.window_count) is not int
                or not 1 <= value.window_count <= 16
                or type(value.winning_window) is not int
                or value.winning_window is None
                or not 0 <= value.winning_window < value.window_count
            ):
                raise ContextUnavailableError("Reranker returned malformed output.")
            candidate.rerank = value
            scored.append(candidate)
        scored.sort(key=self._rerank_sort_key)
        for rank, candidate in enumerate(scored, 1):
            candidate.rerank_rank = rank
        return scored

    def _expand(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        authority: ProcessingRun,
        embedding_run: EmbeddingRun | None,
        seeds: list[_RankedCandidate],
        filters: RetrievalFilters,
        deadline: float,
    ) -> tuple[tuple[Chunk, ...], dict[UUID, set[UUID]]]:
        if not seeds:
            return (), {}
        timeout_ms = max(1, int((deadline - time.monotonic()) * 1000))
        try:
            with self._store.session(
                project_id, snapshot_id, authority, embedding_run, timeout_ms
            ) as evidence:
                neighbours = evidence.neighbours([item.hit.chunk for item in seeds], filters)
        except EmbeddingConflictError as error:
            raise ContextConflictError("Context evidence changed during expansion.") from error
        edges: dict[UUID, set[UUID]] = {}
        identities: dict[tuple[UUID, str, int], Chunk] = {}
        for neighbour in neighbours:
            identity = (
                neighbour.artifact_processing_id,
                neighbour.unit_anchor,
                neighbour.part_index,
            )
            previous = identities.get(identity)
            if previous is not None and previous.id != neighbour.id:
                raise ContextConflictError("Neighbour identity is ambiguous.")
            identities[identity] = neighbour
            parents = {
                seed.hit.chunk.id for seed in seeds if self._is_neighbour(seed.hit.chunk, neighbour)
            }
            if not parents:
                raise ContextConflictError("Neighbour identity is inconsistent.")
            edges.setdefault(neighbour.id, set()).update(parents)
        self._validate_distinct_ranges([item.hit.chunk for item in seeds] + list(neighbours))
        return tuple({item.id: item for item in neighbours}.values()), edges

    def _final_verify(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        authority: ProcessingRun,
        embedding_run: EmbeddingRun | None,
        packed: list[_Draft],
        deadline: float,
    ) -> None:
        self._check_deadline(deadline)
        timeout_ms = max(1, int((deadline - time.monotonic()) * 1000))
        try:
            with self._store.session(
                project_id, snapshot_id, authority, embedding_run, timeout_ms
            ) as evidence:
                observed = evidence.hydrate([item.chunk.id for item in packed])
        except EmbeddingConflictError as error:
            raise ContextConflictError(
                "Context evidence changed before final verification."
            ) from error
        if len(observed) != len(packed):
            raise ContextConflictError("Final context observations are unavailable.")
        for item in packed:
            if observed.get(item.chunk.id) != item.chunk:
                raise ContextConflictError("Final context observation changed.")
        self._validate_distinct_ranges([item.chunk for item in packed])
        self._check_deadline(deadline)

    def _deduplicate_and_validate(
        self, hits: tuple[RetrievalHit, ...], decisions: list[ContextDecision]
    ) -> list[_RankedCandidate]:
        unique: dict[UUID, _RankedCandidate] = {}
        for hit in hits:
            self._validate_chunk(hit.chunk)
            previous = unique.get(hit.chunk.id)
            if previous is not None:
                if previous.hit.chunk != hit.chunk:
                    raise ContextConflictError("Duplicate chunk references disagree.")
                decisions.append(
                    self._chunk_decision(
                        hit.chunk,
                        previous,
                        "deduplication",
                        "excluded",
                        "duplicate_reference",
                    )
                )
                continue
            unique[hit.chunk.id] = _RankedCandidate(hit)
        values = list(unique.values())
        self._validate_distinct_ranges([item.hit.chunk for item in values])
        return values

    @staticmethod
    def _validate_chunk(chunk: Chunk) -> None:
        import hashlib

        if (
            chunk.artifact_id is None
            or chunk.locator is None
            or chunk.run_id is None
            or chunk.snapshot_id is None
            or not chunk.content.strip()
            or chunk.span.start_offset < 0
            or chunk.span.end_offset <= chunk.span.start_offset
            or chunk.span.end_offset - chunk.span.start_offset != len(chunk.content)
            or chunk.span.start_line < 1
            or chunk.span.end_line < chunk.span.start_line
            or chunk.span.start_column < 0
            or chunk.span.end_column < 0
            or chunk.ordinal < 0
            or chunk.part_index < 0
            or hashlib.sha256(chunk.content.encode("utf-8")).hexdigest() != chunk.content_hash
        ):
            raise ContextConflictError("Candidate evidence failed integrity validation.")

    @staticmethod
    def _validate_distinct_ranges(chunks: list[Chunk]) -> None:
        groups: dict[UUID, list[Chunk]] = {}
        for chunk in {item.id: item for item in chunks}.values():
            groups.setdefault(chunk.artifact_processing_id, []).append(chunk)
        for values in groups.values():
            ordered = sorted(
                values, key=lambda item: (item.span.start_offset, item.span.end_offset)
            )
            for left, right in zip(ordered, ordered[1:], strict=False):
                if left.span.end_offset > right.span.start_offset:
                    raise ContextConflictError("Distinct chunk source ranges overlap.")

    @staticmethod
    def _is_neighbour(seed: Chunk, value: Chunk) -> bool:
        if (
            seed.id == value.id
            or seed.artifact_processing_id != value.artifact_processing_id
            or seed.unit_anchor != value.unit_anchor
            or seed.kind != value.kind
            or abs(seed.part_index - value.part_index) != 1
            or seed.symbol_id != value.symbol_id
            or seed.symbol_identity_id != value.symbol_identity_id
            or seed.heading_path != value.heading_path
            or seed.json_path != value.json_path
        ):
            return False
        before, after = (seed, value) if seed.part_index < value.part_index else (value, seed)
        return before.span.end_offset == after.span.start_offset

    def _try_pack(self, query: str, packed: list[_Draft], item: _Draft) -> bool:
        if len(packed) >= self._item_limit:
            return False
        proposed = [value.chunk for value in packed] + [item.chunk]
        if self._sizer.measure(query, proposed) > self._byte_limit:
            return False
        packed.append(item)
        return True

    @staticmethod
    def _seed_rank(chunk_id: UUID, seeds: list[_RankedCandidate]) -> int:
        return next(
            (index for index, item in enumerate(seeds, 1) if item.hit.chunk.id == chunk_id),
            SEED_COUNT + 1,
        )

    @staticmethod
    def _rerank_sort_key(item: _RankedCandidate) -> tuple[float, int, str, str]:
        if item.rerank is None or item.rerank.score is None:
            raise ContextUnavailableError("Reranker result disappeared during ordering.")
        return (
            -item.rerank.score,
            item.hit.rank,
            item.hit.chunk.stable_key,
            str(item.hit.chunk.id),
        )

    @staticmethod
    def _decision(
        candidate: _RankedCandidate,
        stage: str,
        disposition: DecisionDisposition,
        reason: str,
    ) -> ContextDecision:
        return ContextService._chunk_decision(
            candidate.hit.chunk, candidate, stage, disposition, reason
        )

    @staticmethod
    def _chunk_decision(
        chunk: Chunk,
        candidate: _RankedCandidate | None,
        stage: str,
        disposition: DecisionDisposition,
        reason: str,
        related: set[UUID] | None = None,
    ) -> ContextDecision:
        return ContextDecision(
            chunk.id,
            chunk.stable_key,
            chunk.locator or "",
            stage,
            disposition,
            reason,
            tuple(sorted(related or (), key=str)),
            candidate.hit.rank if candidate else None,
            candidate.rerank_rank if candidate else None,
            candidate.rerank.score if candidate and candidate.rerank else None,
        )

    @staticmethod
    def _evidence(value: _Draft, cost: int) -> ContextEvidenceItem:
        rerank = value.candidate.rerank if value.candidate else None
        return ContextEvidenceItem(
            value.chunk,
            value.candidate.hit if value.candidate else None,
            value.candidate is not None,
            value.seed,
            tuple(sorted(value.parents, key=str)),
            rerank.score if rerank else None,
            value.candidate.rerank_rank if value.candidate else None,
            rerank.window_count if rerank else None,
            rerank.winning_window if rerank else None,
            cost,
        )

    def _check_source_bound(self, chunks: Iterable[Chunk]) -> None:
        values = list(chunks)
        if sum(len(item.content.encode("utf-8")) for item in values) > self._source_byte_limit:
            raise ContextLimitError("Context source hydration exceeds its byte limit.")

    @staticmethod
    def _diagnostic_projection(result: ContextResult) -> bytes:
        # A conservative source-free response-bound check; evidence bytes are added exactly.
        diagnostics = json.dumps(
            {
                "decisions": [
                    {
                        "chunk_id": str(item.chunk_id),
                        "stable_key": item.stable_key,
                        "locator": item.locator,
                        "stage": item.stage,
                        "reason": item.reason,
                    }
                    for item in result.decisions
                ]
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return diagnostics + b"x" * result.budget.used

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise ContextUnavailableError("Context construction timed out.")
