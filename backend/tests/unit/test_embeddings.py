from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from buildlens.application.embeddings import (
    EmbeddingBusyError,
    EmbeddingConflictError,
    EmbeddingPreparationRequiredError,
    EmbeddingProvider,
    EmbeddingRuntime,
    EmbeddingService,
    EmbeddingStore,
    EmbeddingUnavailableError,
    ModelCacheStatus,
)
from buildlens.application.processing import ProcessingService, ProcessingStatus
from buildlens.domain.embeddings import (
    ChunkEmbedding,
    EmbeddingContract,
    EmbeddingRun,
    EmbeddingVector,
    SemanticSearchFilters,
    SemanticSearchHit,
)
from buildlens.domain.processing import Chunk, ChunkSpan, ProcessingRun


def contract() -> EmbeddingContract:
    return EmbeddingContract(
        provider="fake",
        provider_version="1",
        model_id="test/model",
        model_revision="a" * 40,
        dimensions=2,
        backend="test",
        device="cpu",
        precision="float32",
        document_prompt="empty",
        query_prompt="empty",
        document_format="chunk-content.v1",
        query_format="query-text.v1",
        token_limit=256,
        window_policy="windows.v1",
        pooling="weighted.v1",
        normalization="l2.v1",
        sentence_transformers_version="5",
        transformers_version="4",
        torch_version="2",
    )


class FakeProvider:
    def __init__(self, active_contract: EmbeddingContract | None = None) -> None:
        self.contract = active_contract or contract()
        self.documents: list[list[str]] = []
        self.queries: list[str] = []
        self.fail = False
        self.bad_vector = False
        self.override_vectors: list[EmbeddingVector] | None = None
        self.query_vector = EmbeddingVector((0.8, 0.6), 1)
        self.on_documents: Callable[[], None] | None = None
        self.on_query: Callable[[], None] | None = None

    def status(self) -> ModelCacheStatus:
        return ModelCacheStatus(True, "/verified")

    def embed_documents(self, texts: list[str]) -> list[EmbeddingVector]:
        self.documents.append(list(texts))
        if self.on_documents:
            self.on_documents()
        if self.fail:
            raise EmbeddingUnavailableError
        if self.override_vectors is not None:
            return self.override_vectors
        if self.bad_vector:
            return [EmbeddingVector((2.0, 0.0), 1) for _ in texts]
        return [
            EmbeddingVector((1.0, 0.0), 1) if index == 0 else EmbeddingVector((0.0, 1.0), 2)
            for index, _ in enumerate(texts)
        ]

    def embed_query(self, text: str) -> EmbeddingVector:
        self.queries.append(text)
        if self.on_query:
            self.on_query()
        return self.query_vector

    def close(self) -> None:
        pass


class FakeProcessing:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.authority = run(len(chunks))
        self.chunks = [
            replace(
                value,
                run_id=self.authority.id,
                snapshot_id=self.authority.project_version_id,
            )
            for value in chunks
        ]

    def status(self, project_id: UUID, snapshot_id: UUID) -> ProcessingStatus:
        return ProcessingStatus(self.authority, self.authority, "fresh")

    def current_authority(self, project_id: UUID, snapshot_id: UUID) -> ProcessingRun:
        return self.authority

    def current_content_stats(
        self, project_id: UUID, snapshot_id: UUID, expected_run: ProcessingRun
    ) -> tuple[int, int]:
        self._same(expected_run)
        return len(self.chunks), sum(len(value.content.encode("utf-8")) for value in self.chunks)

    def current_content_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        expected_run: ProcessingRun,
        limit: int,
        offset: int,
    ) -> list[Chunk]:
        self._same(expected_run)
        return self.chunks[offset : offset + limit]

    def current_chunks(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        expected_run: ProcessingRun,
        chunk_ids: list[UUID],
    ) -> list[Chunk]:
        self._same(expected_run)
        values = {value.id: value for value in self.chunks}
        return [values[value] for value in chunk_ids if value in values]

    def _same(self, expected: ProcessingRun) -> None:
        if expected.id != self.authority.id:
            raise EmbeddingConflictError


class FakeStore:
    """Test-only in-memory oracle; production search is always PostgreSQL/pgvector."""

    def __init__(self, processing: FakeProcessing) -> None:
        self.processing = processing
        self.active_run: EmbeddingRun | None = None
        self.latest_run: EmbeddingRun | None = None
        self.embedding_rows: list[ChunkEmbedding] = []

    def latest(self, project_id: UUID, snapshot_id: UUID) -> EmbeddingRun | None:
        return self.latest_run

    def active(self, project_id: UUID, snapshot_id: UUID) -> EmbeddingRun | None:
        return self.active_run

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[EmbeddingRun], int]:
        values = [self.latest_run] if self.latest_run else []
        return values[offset : offset + limit], len(values)

    def get(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> EmbeddingRun | None:
        for value in (self.active_run, self.latest_run):
            if value and value.id == run_id:
                return value
        return None

    def page_rows(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID, limit: int, offset: int
    ) -> tuple[list[ChunkEmbedding], int]:
        values = [row for row in self.embedding_rows if row.embedding_run_id == run_id]
        return values[offset : offset + limit], len(values)

    def reusable(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        logical_keys: list[str],
        contract: EmbeddingContract,
        processing_contract: dict[str, object],
    ) -> dict[str, ChunkEmbedding]:
        requested = set(logical_keys)
        return {
            row.logical_embedding_key: row
            for row in self.embedding_rows
            if row.logical_embedding_key in requested and row.values is not None
        }

    def publish(
        self, run: EmbeddingRun, rows: list[ChunkEmbedding], authority: ProcessingRun
    ) -> EmbeddingRun:
        revision = (self.latest_run.revision if self.latest_run else 0) + 1
        persisted = replace(run, revision=revision, chunk_count=len(rows))
        self.embedding_rows.extend(rows)
        self.active_run = self.latest_run = persisted
        return persisted

    def save_failure(self, run: EmbeddingRun) -> EmbeddingRun:
        revision = (self.latest_run.revision if self.latest_run else 0) + 1
        persisted = replace(run, revision=revision)
        self.latest_run = persisted
        return persisted

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
        chunks = {value.id: value for value in self.processing.chunks}
        hits: list[SemanticSearchHit] = []
        for row in self.embedding_rows:
            chunk_value = chunks.get(row.chunk_id)
            if row.embedding_run_id != run.id or row.values is None or chunk_value is None:
                continue
            if filters.locator_prefix and not (chunk_value.locator or "").startswith(
                filters.locator_prefix
            ):
                continue
            if filters.kind and chunk_value.kind != filters.kind:
                continue
            score = math.fsum(
                left * right for left, right in zip(row.values, query_vector, strict=True)
            )
            hits.append(SemanticSearchHit(score, row, chunk_value))
        hits.sort(key=lambda item: (-item.score, item.chunk.stable_key, str(item.chunk.id)))
        return hits[:top_k]


def run(chunk_count: int) -> ProcessingRun:
    now = datetime.now(UTC)
    return ProcessingRun(
        id=uuid4(),
        project_version_id=uuid4(),
        started_at=now,
        completed_at=now,
        outcome="complete",
        structural_analysis_id=None,
        processing_contract={"processor": "v1"},
        limits={},
        coverage={"chunks": chunk_count, "gaps": 0},
        revision=1,
    )


def chunk(content: str, stable_key: str, ordinal: int) -> Chunk:
    return Chunk(
        id=uuid4(),
        artifact_processing_id=uuid4(),
        stable_key=stable_key,
        ordinal=ordinal,
        unit_anchor="unit",
        part_index=0,
        kind="source",
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        span=ChunkSpan(0, len(content), 1, 1, 0, len(content)),
        locator=f"{ordinal}.py",
        artifact_id=uuid4(),
    )


def service(
    processing: FakeProcessing, provider: FakeProvider | None = None
) -> tuple[EmbeddingService, EmbeddingRuntime, FakeProvider, FakeStore]:
    selected = provider or FakeProvider()
    runtime = EmbeddingRuntime(cast(EmbeddingProvider, selected))
    store = FakeStore(processing)
    return (
        EmbeddingService(cast(ProcessingService, processing), runtime, cast(EmbeddingStore, store)),
        runtime,
        selected,
        store,
    )


def test_preparation_embeds_exact_content_and_search_returns_exact_provenance() -> None:
    values = [chunk("alpha β\n", "b", 1), chunk("second\n", "a", 2)]
    processing = FakeProcessing(values)
    embeddings, _, provider, _ = service(processing)
    project_id, snapshot_id = uuid4(), processing.authority.project_version_id

    embedding_run = embeddings.prepare(project_id, snapshot_id)

    assert provider.documents == [["alpha β\n", "second\n"]]
    assert embedding_run.chunk_count == 2
    assert embedding_run.coverage["embedded_chunks"] == 2
    result = embeddings.search(project_id, snapshot_id, "  find alpha  ", 2)
    assert provider.queries == ["find alpha"]
    assert [value.chunk.content for value in result.hits] == ["alpha β\n", "second\n"]
    assert result.hits[0].score == pytest.approx(0.8)
    assert embeddings.prepare(project_id, snapshot_id) is embedding_run
    assert len(provider.documents) == 1


def test_equal_scores_use_stable_chunk_key_then_chunk_id() -> None:
    values = [chunk("first", "z", 1), chunk("second", "a", 2)]
    processing = FakeProcessing(values)
    embeddings, _, provider, _ = service(processing)
    provider.query_vector = EmbeddingVector((2**-0.5, 2**-0.5), 1)
    project_id, snapshot_id = uuid4(), processing.authority.project_version_id
    embeddings.prepare(project_id, snapshot_id)
    result = embeddings.search(project_id, snapshot_id, "query", 2)
    assert [value.chunk.stable_key for value in result.hits] == ["a", "z"]


def test_bad_content_hash_is_rejected_before_provider_invocation() -> None:
    value = replace(chunk("exact", "a", 1), content_hash="0" * 64)
    processing = FakeProcessing([value])
    embeddings, _, provider, _ = service(processing)
    with pytest.raises(EmbeddingConflictError):
        embeddings.prepare(uuid4(), processing.authority.project_version_id)
    assert provider.documents == []


def test_authority_change_makes_embedding_evidence_unavailable() -> None:
    processing = FakeProcessing([chunk("exact", "a", 1)])
    embeddings, _, _, _ = service(processing)
    project_id, snapshot_id = uuid4(), processing.authority.project_version_id
    embeddings.prepare(project_id, snapshot_id)
    processing.authority = replace(processing.authority, id=uuid4(), revision=2)
    with pytest.raises(EmbeddingPreparationRequiredError):
        embeddings.search(project_id, snapshot_id, "query")


def test_authority_change_during_preparation_discards_candidate() -> None:
    processing = FakeProcessing([chunk("exact", "a", 1)])
    embeddings, _, provider, store = service(processing)
    provider.on_documents = lambda: setattr(
        processing,
        "authority",
        replace(processing.authority, id=uuid4(), revision=2),
    )
    with pytest.raises(EmbeddingConflictError):
        embeddings.prepare(uuid4(), processing.authority.project_version_id)
    assert store.active_run is None


def test_authority_change_during_search_returns_conflict() -> None:
    processing = FakeProcessing([chunk("exact", "a", 1)])
    embeddings, _, provider, _ = service(processing)
    project_id, snapshot_id = uuid4(), processing.authority.project_version_id
    embeddings.prepare(project_id, snapshot_id)
    provider.on_query = lambda: setattr(
        processing,
        "authority",
        replace(processing.authority, id=uuid4(), revision=2),
    )
    with pytest.raises(EmbeddingConflictError):
        embeddings.search(project_id, snapshot_id, "query")


def test_busy_operation_is_rejected_without_waiting() -> None:
    processing = FakeProcessing([])
    embeddings, runtime, _, _ = service(processing)
    assert runtime.lock.acquire(False)
    try:
        with pytest.raises(EmbeddingBusyError):
            embeddings.prepare(uuid4(), processing.authority.project_version_id)
    finally:
        runtime.lock.release()


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [EmbeddingVector((1.0,), 1)],
        [EmbeddingVector((float("nan"), 0.0), 1)],
        [EmbeddingVector((float("inf"), 0.0), 1)],
        [EmbeddingVector((0.0, 0.0), 1)],
        [EmbeddingVector((2.0, 0.0), 1)],
    ],
)
def test_provider_output_validation_rejects_malformed_vectors(
    vectors: list[EmbeddingVector],
) -> None:
    processing = FakeProcessing([chunk("exact", "a", 1)])
    embeddings, _, provider, _ = service(processing)
    provider.override_vectors = vectors
    with pytest.raises(EmbeddingUnavailableError):
        embeddings.prepare(uuid4(), processing.authority.project_version_id)


def test_every_contract_field_participates_in_digest() -> None:
    baseline = contract()
    for value in fields(baseline):
        original = getattr(baseline, value.name)
        changed = original + 1 if isinstance(original, int) else f"{original}-changed"
        updates = cast(Any, {value.name: changed})
        assert replace(baseline, **updates).digest != baseline.digest


def test_empty_current_run_builds_searchable_empty_embedding_run() -> None:
    processing = FakeProcessing([])
    embeddings, _, provider, _ = service(processing)
    project_id, snapshot_id = uuid4(), processing.authority.project_version_id
    embedding_run = embeddings.prepare(project_id, snapshot_id)
    result = embeddings.search(project_id, snapshot_id, "anything")
    assert embedding_run.chunk_count == 0
    assert result.hits == ()
    assert provider.documents == []


@pytest.mark.parametrize(
    ("query", "top_k"),
    [("   ", 10), ("x" * 4097, 10), ("query", 0), ("query", 51)],
)
def test_search_rejects_query_and_top_k_boundaries(query: str, top_k: int) -> None:
    processing = FakeProcessing([])
    embeddings, _, _, _ = service(processing)
    with pytest.raises(ValueError):
        embeddings.search(uuid4(), processing.authority.project_version_id, query, top_k)
