from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID

import pytest

from buildlens.application.context import (
    ContextConflictError,
    ContextService,
    ContextSizer,
    RerankerRuntime,
)
from buildlens.application.embeddings import ModelCacheStatus
from buildlens.domain.context import RerankerContract, RerankResult
from buildlens.domain.processing import Chunk, ChunkSpan, ProcessingRun
from buildlens.domain.retrieval import ComponentStatus, RetrievalHit, RetrievalResult


def chunk(identifier: int, content: str, *, part: int = 0, start: int = 0) -> Chunk:
    return Chunk(
        UUID(int=identifier),
        UUID(int=100),
        f"key-{identifier}",
        identifier,
        "unit",
        part,
        "python",
        content,
        hashlib.sha256(content.encode()).hexdigest(),
        ChunkSpan(start, start + len(content), 1, 1, start, start + len(content)),
        locator="src/example.py",
        artifact_id=UUID(int=200),
        run_id=UUID(int=300),
        snapshot_id=UUID(int=400),
    )


def run() -> ProcessingRun:
    now = datetime.now(UTC)
    return ProcessingRun(
        UUID(int=300), UUID(int=400), now, now, "complete", None, {}, {}, {}, revision=2
    )


def retrieval(chunks: list[Chunk]) -> RetrievalResult:
    hits = tuple(
        RetrievalHit(index, "lexical", 1 / index, value) for index, value in enumerate(chunks, 1)
    )
    return RetrievalResult(
        "lexical",
        "lexical",
        UUID(int=500),
        UUID(int=400),
        run(),
        None,
        ComponentStatus("completed", len(hits)),
        ComponentStatus("completed", len(hits)),
        ComponentStatus("not_requested"),
        len(hits),
        hits,
    )


class FakeRetrieval:
    def __init__(self, value: RetrievalResult) -> None:
        self.value = value

    def context_candidates(self, *args: object, **kwargs: object) -> RetrievalResult:
        return self.value


class FakeEvidenceSession:
    def __init__(self, values: list[Chunk], neighbours: tuple[Chunk, ...] = ()) -> None:
        self.values = {item.id: item for item in values + list(neighbours)}
        self._neighbours = neighbours

    def neighbours(self, seeds: list[Chunk], filters: object) -> tuple[Chunk, ...]:
        return self._neighbours

    def hydrate(self, chunk_ids: list[UUID]) -> dict[UUID, Chunk]:
        return {identifier: self.values[identifier] for identifier in chunk_ids}


class FakeEvidenceStore:
    def __init__(self, values: list[Chunk], neighbours: tuple[Chunk, ...] = ()) -> None:
        self.session_value = FakeEvidenceSession(values, neighbours)

    @contextmanager
    def session(self, *args: object, **kwargs: object) -> Iterator[FakeEvidenceSession]:
        yield self.session_value


class FakeReranker:
    def __init__(self, scores: dict[UUID, float] | None = None) -> None:
        self.scores = scores or {}
        self.calls = 0
        self.contract = RerankerContract(
            "fake",
            "1",
            "fake/model",
            "a" * 40,
            "fake/model",
            "a" * 40,
            "pytorch",
            "cpu",
            "float32",
            "pair.v1",
            512,
            128,
            "windows.v1",
            "max.v1",
            16,
            8,
            "test",
            "test",
        )

    def status(self) -> ModelCacheStatus:
        return ModelCacheStatus(True, "/fake")

    def score(
        self, query: str, candidates: list[Chunk], timeout_seconds: float
    ) -> list[RerankResult]:
        self.calls += 1
        return [RerankResult(item.id, float(self.scores[item.id]), 1, 0) for item in candidates]

    def close(self) -> None:
        pass


def service(
    values: list[Chunk],
    reranker: FakeReranker,
    *,
    neighbours: tuple[Chunk, ...] = (),
    limit: int = 32_000,
) -> ContextService:
    return ContextService(
        FakeRetrieval(retrieval(values)),  # type: ignore[arg-type]
        RerankerRuntime(reranker, Lock()),
        FakeEvidenceStore(values, neighbours),
        byte_limit=limit,
    )


def test_exact_utf8_sizing_includes_json_escaping_and_framing() -> None:
    value = chunk(1, 'é\n"')
    sizer = ContextSizer()
    encoded = sizer.serialize("λ", [value])
    assert len(encoded) == sizer.measure("λ", [value])
    assert b'\\n\\"' in encoded
    assert "é".encode() in encoded and "λ".encode() in encoded
    budget = sizer.budget("λ", [value], 10_000)
    assert budget.used == budget.framing_cost + budget.per_item_costs[0][1]


def test_retrieval_order_never_calls_reranker_and_expands_adjacent_part() -> None:
    seed = chunk(1, "alpha", part=0, start=0)
    neighbour = chunk(2, "beta", part=1, start=5)
    provider = FakeReranker()
    result = service([seed], provider, neighbours=(neighbour,)).build(
        UUID(int=500), UUID(int=400), "alpha", "lexical", "retrieval_order"
    )
    assert provider.calls == 0
    assert [item.chunk.id for item in result.items] == [seed.id, neighbour.id]
    assert result.items[1].expanded_from == (seed.id,)
    assert result.items[1].retrieval is None
    assert result.reranker_state == "not_requested"


def test_cross_encoder_orders_negative_scores_and_breaks_ties_by_retrieval_rank() -> None:
    first = chunk(1, "one", start=0)
    second = chunk(2, "two", start=3)
    third = chunk(3, "three", start=6)
    provider = FakeReranker({first.id: -2.0, second.id: -1.0, third.id: -1.0})
    result = service([first, second, third], provider).build(
        UUID(int=500), UUID(int=400), "query", "lexical", "local_cross_encoder"
    )
    assert [item.chunk.id for item in result.items] == [second.id, third.id, first.id]
    assert [item.rerank_rank for item in result.items] == [1, 2, 3]


def test_distinct_overlapping_observations_are_integrity_failure() -> None:
    first, second = chunk(1, "abcdef", start=0), chunk(2, "overlap", start=5)
    with pytest.raises(ContextConflictError, match="overlap"):
        service([first, second], FakeReranker()).build(
            UUID(int=500), UUID(int=400), "query", "lexical", "retrieval_order"
        )


def test_whole_item_packing_skips_a_nonfitting_rank_and_continues() -> None:
    first = chunk(1, "a" * 2_000, start=0)
    second = chunk(2, "b" * 2_500, start=2_000)
    third = chunk(3, "c" * 6_000, start=4_500)
    fourth = chunk(4, "d" * 800, start=10_500)
    query = "pack"
    limit = ContextSizer().measure(query, [first, second, fourth])
    result = service([first, second, third, fourth], FakeReranker(), limit=limit).build(
        UUID(int=500), UUID(int=400), query, "lexical", "retrieval_order"
    )
    assert [item.chunk.id for item in result.items] == [first.id, second.id, fourth.id]
    assert result.budget.used == limit
    assert result.budget.remaining == 0
    assert any(
        item.chunk_id == third.id and item.reason == "remaining_budget" for item in result.decisions
    )
