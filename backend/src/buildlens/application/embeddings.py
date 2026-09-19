"""Explicit persistent preparation and exact pgvector semantic search."""

from __future__ import annotations

import hashlib
import math
import time
from array import array
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from buildlens.application.processing import (
    CurrentContentUnavailableError,
    ProcessingConflictError,
    ProcessingService,
)
from buildlens.domain.embeddings import (
    ChunkEmbedding,
    EmbeddingContract,
    EmbeddingExecutionMode,
    EmbeddingRun,
    EmbeddingVector,
    SemanticSearchFilters,
    SemanticSearchHit,
    embedding_key,
    membership_digest,
)
from buildlens.domain.processing import Chunk, ProcessingRun


class EmbeddingBusyError(Exception):
    pass


class EmbeddingUnavailableError(Exception):
    pass


class EmbeddingPreparationRequiredError(Exception):
    pass


class EmbeddingConflictError(Exception):
    pass


class EmbeddingNotFoundError(Exception):
    pass


class EmbeddingLimitError(ValueError):
    pass


class EmbeddingInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ModelCacheStatus:
    available: bool
    path: str
    reason: str | None = None


class EmbeddingProvider(Protocol):
    @property
    def contract(self) -> EmbeddingContract: ...
    def status(self) -> ModelCacheStatus: ...
    def embed_documents(self, texts: list[str]) -> list[EmbeddingVector]: ...
    def embed_query(self, text: str) -> EmbeddingVector: ...
    def close(self) -> None: ...


class EmbeddingStore(Protocol):
    def latest(self, project_id: UUID, snapshot_id: UUID) -> EmbeddingRun | None: ...
    def active(self, project_id: UUID, snapshot_id: UUID) -> EmbeddingRun | None: ...
    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[EmbeddingRun], int]: ...
    def get(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> EmbeddingRun | None: ...
    def page_rows(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID, limit: int, offset: int
    ) -> tuple[list[ChunkEmbedding], int]: ...
    def reusable(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        logical_keys: list[str],
        contract: EmbeddingContract,
        processing_contract: dict[str, object],
    ) -> dict[str, ChunkEmbedding]: ...
    def publish(
        self, run: EmbeddingRun, rows: list[ChunkEmbedding], authority: ProcessingRun
    ) -> EmbeddingRun: ...
    def save_failure(self, run: EmbeddingRun) -> EmbeddingRun: ...
    def search(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run: EmbeddingRun,
        query_vector: tuple[float, ...],
        top_k: int,
        filters: SemanticSearchFilters,
        timeout_ms: int,
    ) -> list[SemanticSearchHit]: ...


@dataclass(slots=True)
class EmbeddingRuntime:
    provider: EmbeddingProvider
    lock: Lock = field(default_factory=Lock)

    def close(self) -> None:
        self.provider.close()


@dataclass(frozen=True, slots=True)
class EmbeddingStatus:
    model: ModelCacheStatus
    contract: EmbeddingContract
    prepared: bool
    search_available: bool
    freshness_reasons: tuple[str, ...]
    latest_attempt: EmbeddingRun | None
    active_run: EmbeddingRun | None
    active_processing_run_id: UUID | None


@dataclass(frozen=True, slots=True)
class SemanticSearchResult:
    embedding_run: EmbeddingRun
    hits: tuple[SemanticSearchHit, ...]


class EmbeddingService:
    def __init__(
        self,
        processing: ProcessingService,
        runtime: EmbeddingRuntime,
        store: EmbeddingStore,
        *,
        max_chunks: int = 20_000,
        max_source_bytes: int = 64 * 1024 * 1024,
        content_page_size: int = 128,
        max_windows_per_chunk: int = 32,
        max_windows: int = 100_000,
        max_collection_bytes: int = 64 * 1024 * 1024,
        preparation_timeout_seconds: float = 300,
        search_timeout_seconds: float = 15,
    ) -> None:
        limits = (
            max_chunks,
            max_source_bytes,
            content_page_size,
            max_windows_per_chunk,
            max_windows,
            max_collection_bytes,
            preparation_timeout_seconds,
            search_timeout_seconds,
        )
        if min(limits) <= 0:
            raise ValueError("Embedding limits must be positive.")
        self._processing, self._runtime, self._store = processing, runtime, store
        self._max_chunks, self._max_source_bytes = max_chunks, max_source_bytes
        self._page_size = content_page_size
        self._max_windows_per_chunk, self._max_windows = max_windows_per_chunk, max_windows
        self._max_collection_bytes = max_collection_bytes
        self._preparation_timeout, self._search_timeout = (
            preparation_timeout_seconds,
            search_timeout_seconds,
        )

    def status(self, project_id: UUID, snapshot_id: UUID) -> EmbeddingStatus:
        processing = self._processing.status(project_id, snapshot_id)
        authority = processing.active_run
        reasons = list(processing.freshness_reasons)
        model = self._runtime.provider.status()
        latest, active = (
            self._store.latest(project_id, snapshot_id),
            self._store.active(project_id, snapshot_id),
        )
        prepared = bool(
            authority
            and not reasons
            and active
            and active.outcome == "complete"
            and not active.superseded
            and not active.quarantined
            and active.processing_run_id == authority.id
            and active.processing_revision == authority.revision
            and active.processing_contract == authority.processing_contract
            and active.contract.digest == self._runtime.provider.contract.digest
        )
        if active and not prepared:
            if authority and (
                active.processing_run_id != authority.id
                or active.processing_revision != authority.revision
                or active.processing_contract != authority.processing_contract
            ):
                reasons.append("PROCESSING_AUTHORITY_CHANGED")
            if active.contract.digest != self._runtime.provider.contract.digest:
                reasons.append("EMBEDDING_CONTRACT_CHANGED")
            if active.quarantined:
                reasons.append("EMBEDDING_EVIDENCE_QUARANTINED")
        return EmbeddingStatus(
            model=model,
            contract=self._runtime.provider.contract,
            prepared=prepared,
            search_available=prepared and model.available,
            freshness_reasons=tuple(dict.fromkeys(reasons)),
            latest_attempt=latest,
            active_run=active,
            active_processing_run_id=authority.id if authority else None,
        )

    def prepare(self, project_id: UUID, snapshot_id: UUID) -> EmbeddingRun:
        if not self._runtime.lock.acquire(False):
            raise EmbeddingBusyError("Another embedding operation is running.")
        started_at = datetime.now(UTC)
        authority: ProcessingRun | None = None
        candidate_id = uuid4()
        rows: list[ChunkEmbedding] = []
        generated = reused = window_count = input_bytes = 0
        try:
            deadline = time.monotonic() + self._preparation_timeout
            authority = self._processing.current_authority(project_id, snapshot_id)
            active, latest = (
                self._store.active(project_id, snapshot_id),
                self._store.latest(project_id, snapshot_id),
            )
            contract = self._runtime.provider.contract
            if (
                active
                and active.outcome == "complete"
                and not active.superseded
                and not active.quarantined
                and active.processing_run_id == authority.id
                and active.processing_revision == authority.revision
                and active.processing_contract == authority.processing_contract
                and active.contract.digest == contract.digest
            ):
                return active
            chunk_count, source_bytes = self._processing.current_content_stats(
                project_id, snapshot_id, authority
            )
            if chunk_count > self._max_chunks:
                raise EmbeddingLimitError("The current run exceeds the embedding chunk limit.")
            if source_bytes > self._max_source_bytes:
                raise EmbeddingLimitError("The current run exceeds the embedding input-byte limit.")
            offset = 0
            while offset < chunk_count:
                self._check_deadline(deadline, "Embedding preparation timed out.")
                page = self._processing.current_content_page(
                    project_id, snapshot_id, authority, self._page_size, offset
                )
                if not page:
                    raise EmbeddingConflictError("Chunk membership changed during preparation.")
                keys: list[str] = []
                for chunk in page:
                    self._validate_provenance(chunk, snapshot_id, authority)
                    observed_hash = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
                    if observed_hash != chunk.content_hash:
                        raise EmbeddingConflictError(
                            "Persisted chunk content failed integrity checks."
                        )
                    keys.append(embedding_key(contract.digest, chunk.stable_key, observed_hash))
                reusable = self._store.reusable(
                    project_id, snapshot_id, keys, contract, authority.processing_contract
                )
                misses = [
                    chunk for chunk, key in zip(page, keys, strict=True) if key not in reusable
                ]
                generated_vectors: dict[UUID, EmbeddingVector] = {}
                if misses:
                    if not self._runtime.provider.status().available:
                        raise EmbeddingUnavailableError(
                            "The pinned local embedding model is unavailable."
                        )
                    self._require_authority(project_id, snapshot_id, authority)
                    vectors = self._runtime.provider.embed_documents(
                        [item.content for item in misses]
                    )
                    self._validate_batch(vectors, len(misses))
                    generated_vectors = dict(
                        zip((item.id for item in misses), vectors, strict=True)
                    )
                for chunk, key in zip(page, keys, strict=True):
                    mode: EmbeddingExecutionMode
                    source = reusable.get(key)
                    if source:
                        if source.values is None:
                            raise EmbeddingConflictError(
                                "Reusable embedding vector is unavailable."
                            )
                        vector = EmbeddingVector(source.values, source.window_count)
                        mode, source_id = "reused", source.id
                        reused += 1
                    else:
                        vector = generated_vectors[chunk.id]
                        mode, source_id = "generated", None
                        generated += 1
                    normalized = self._float32_vector(vector)
                    if normalized.window_count > self._max_windows_per_chunk:
                        raise EmbeddingLimitError("A chunk exceeds the embedding window limit.")
                    window_count += normalized.window_count
                    input_bytes += len(chunk.content.encode("utf-8"))
                    if window_count > self._max_windows:
                        raise EmbeddingLimitError("The preparation exceeds the total window limit.")
                    rows.append(
                        ChunkEmbedding(
                            uuid4(),
                            candidate_id,
                            project_id,
                            snapshot_id,
                            authority.id,
                            chunk.artifact_processing_id,
                            chunk.id,
                            chunk.stable_key,
                            chunk.content_hash,
                            key,
                            contract.digest,
                            contract.dimensions,
                            normalized.window_count,
                            mode,
                            source_id,
                            normalized.values,
                        )
                    )
                offset += len(page)
                self._check_candidate_size(rows)
            if len(rows) != chunk_count:
                raise EmbeddingConflictError("Chunk membership changed during preparation.")
            self._require_authority(project_id, snapshot_id, authority)
            now = datetime.now(UTC)
            candidate = EmbeddingRun(
                candidate_id,
                project_id,
                snapshot_id,
                authority.id,
                authority.revision,
                dict(authority.processing_contract),
                contract,
                0,
                started_at,
                now,
                "complete",
                False,
                False,
                active.id if active else None,
                latest.revision if latest else 0,
                membership_digest(rows),
                {
                    "max_chunks": self._max_chunks,
                    "max_source_bytes": self._max_source_bytes,
                    "max_windows_per_chunk": self._max_windows_per_chunk,
                    "max_windows": self._max_windows,
                    "max_candidate_bytes": self._max_collection_bytes,
                },
                {
                    **authority.coverage,
                    "embedded_chunks": chunk_count,
                    "generated_chunks": generated,
                    "reused_chunks": reused,
                    "embedding_windows": window_count,
                    "embedding_input_bytes": input_bytes,
                },
                (),
                chunk_count,
            )
            return self._store.publish(candidate, rows, authority)
        except (CurrentContentUnavailableError, ProcessingConflictError) as error:
            if authority is not None:
                self._record_failure(
                    project_id,
                    snapshot_id,
                    authority,
                    candidate_id,
                    started_at,
                    rows,
                    generated,
                    reused,
                    window_count,
                    input_bytes,
                    "PROCESSING_AUTHORITY_CHANGED",
                )
            raise EmbeddingConflictError("Current processing evidence is unavailable.") from error
        except EmbeddingConflictError:
            if authority is not None:
                self._record_failure(
                    project_id,
                    snapshot_id,
                    authority,
                    candidate_id,
                    started_at,
                    rows,
                    generated,
                    reused,
                    window_count,
                    input_bytes,
                    "EMBEDDING_CONFLICT",
                )
            raise
        except (EmbeddingUnavailableError, EmbeddingLimitError) as error:
            if authority is not None:
                self._record_failure(
                    project_id,
                    snapshot_id,
                    authority,
                    candidate_id,
                    started_at,
                    rows,
                    generated,
                    reused,
                    window_count,
                    input_bytes,
                    type(error).__name__,
                )
            raise
        except SQLAlchemyError:
            if authority is not None:
                self._record_failure(
                    project_id,
                    snapshot_id,
                    authority,
                    candidate_id,
                    started_at,
                    rows,
                    generated,
                    reused,
                    window_count,
                    input_bytes,
                    "PERSISTENCE_FAILED",
                )
            raise
        finally:
            self._runtime.lock.release()

    def search(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        query: str,
        top_k: int = 10,
        filters: SemanticSearchFilters | None = None,
    ) -> SemanticSearchResult:
        normalized = self._validate_query(query, top_k)
        selected_filters = filters or SemanticSearchFilters()
        self._validate_filters(selected_filters)
        if not self._runtime.lock.acquire(False):
            raise EmbeddingBusyError("Another embedding operation is running.")
        try:
            deadline = time.monotonic() + self._search_timeout
            authority = self._processing.current_authority(project_id, snapshot_id)
            run = self._store.active(project_id, snapshot_id)
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
            self._check_deadline(deadline, "Semantic search timed out.")
            query_vector = self._float32_vector(self._runtime.provider.embed_query(normalized))
            timeout_ms = max(1, int((deadline - time.monotonic()) * 1000))
            hits = self._store.search(
                project_id,
                snapshot_id,
                run,
                query_vector.values,
                top_k,
                selected_filters,
                timeout_ms,
            )
            for hit in hits:
                self._validate_provenance(hit.chunk, snapshot_id, authority)
                if (
                    hit.chunk.id != hit.embedding.chunk_id
                    or hit.chunk.stable_key != hit.embedding.stable_chunk_key
                    or hit.chunk.content_hash != hit.embedding.input_hash
                    or hashlib.sha256(hit.chunk.content.encode("utf-8")).hexdigest()
                    != hit.embedding.input_hash
                ):
                    raise EmbeddingConflictError("Selected chunk provenance changed during search.")
            self._require_authority(project_id, snapshot_id, authority)
            self._check_deadline(deadline, "Semantic search timed out.")
            return SemanticSearchResult(run, tuple(hits))
        except (CurrentContentUnavailableError, ProcessingConflictError) as error:
            raise EmbeddingConflictError(
                "Current processing evidence changed during search."
            ) from error
        finally:
            self._runtime.lock.release()

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[EmbeddingRun], int]:
        self._processing.require_snapshot(project_id, snapshot_id)
        return self._store.history(project_id, snapshot_id, limit, offset)

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> EmbeddingRun:
        self._processing.require_snapshot(project_id, snapshot_id)
        value = self._store.get(project_id, snapshot_id, run_id)
        if value is None:
            raise EmbeddingNotFoundError("Embedding run was not found.")
        return value

    def rows(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID, limit: int, offset: int
    ) -> tuple[list[ChunkEmbedding], int]:
        self.run(project_id, snapshot_id, run_id)
        return self._store.page_rows(project_id, snapshot_id, run_id, limit, offset)

    def _record_failure(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        authority: ProcessingRun,
        candidate_id: UUID,
        started_at: datetime,
        rows: list[ChunkEmbedding],
        generated: int,
        reused: int,
        windows: int,
        input_bytes: int,
        diagnostic: str,
    ) -> None:
        try:
            active, latest = (
                self._store.active(project_id, snapshot_id),
                self._store.latest(project_id, snapshot_id),
            )
            self._store.save_failure(
                EmbeddingRun(
                    candidate_id,
                    project_id,
                    snapshot_id,
                    authority.id,
                    authority.revision,
                    dict(authority.processing_contract),
                    self._runtime.provider.contract,
                    0,
                    started_at,
                    datetime.now(UTC),
                    "failed",
                    False,
                    False,
                    active.id if active else None,
                    latest.revision if latest else 0,
                    membership_digest(rows),
                    {},
                    {
                        "generated_chunks": generated,
                        "reused_chunks": reused,
                        "embedding_windows": windows,
                        "embedding_input_bytes": input_bytes,
                    },
                    (diagnostic,),
                    0,
                )
            )
        except Exception:
            # The original source-free error is still authoritative; never claim this persisted.
            return

    def _require_authority(
        self, project_id: UUID, snapshot_id: UUID, expected: ProcessingRun
    ) -> None:
        current = self._processing.current_authority(project_id, snapshot_id)
        if (
            current.id != expected.id
            or current.revision != expected.revision
            or current.processing_contract != expected.processing_contract
        ):
            raise EmbeddingConflictError("Current processing evidence changed.")

    def _validate_batch(self, vectors: list[EmbeddingVector], expected: int) -> None:
        if len(vectors) != expected:
            raise EmbeddingUnavailableError("Embedding provider returned malformed output.")
        for vector in vectors:
            self._validate_vector(vector)

    def _float32_vector(self, vector: EmbeddingVector) -> EmbeddingVector:
        self._validate_vector(vector)
        converted = EmbeddingVector(tuple(array("f", vector.values)), vector.window_count)
        self._validate_vector(converted)
        return converted

    def _validate_vector(self, vector: EmbeddingVector) -> None:
        if (
            vector.window_count < 1
            or len(vector.values) != self._runtime.provider.contract.dimensions
        ):
            raise EmbeddingUnavailableError("Embedding provider returned malformed output.")
        if any(type(value) is not float or not math.isfinite(value) for value in vector.values):
            raise EmbeddingUnavailableError("Embedding provider returned malformed output.")
        norm = math.sqrt(math.fsum(value * value for value in vector.values))
        if norm == 0 or not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
            raise EmbeddingUnavailableError("Embedding provider returned malformed output.")

    @staticmethod
    def _validate_provenance(chunk: Chunk, snapshot_id: UUID, authority: ProcessingRun) -> None:
        if (
            chunk.artifact_id is None
            or chunk.locator is None
            or chunk.run_id != authority.id
            or chunk.snapshot_id != snapshot_id
        ):
            raise EmbeddingConflictError("Chunk provenance is incomplete or incorrectly scoped.")

    def _check_candidate_size(self, rows: list[ChunkEmbedding]) -> None:
        size = sum(
            (len(row.values or ()) * 4 + len(row.stable_chunk_key.encode()) + 320) for row in rows
        )
        if size > self._max_collection_bytes:
            raise EmbeddingLimitError("The prepared candidate exceeds its memory budget.")

    @staticmethod
    def _validate_query(query: str, top_k: int) -> str:
        normalized = query.strip()
        if not normalized:
            raise EmbeddingInputError("Query must not be empty.")
        if len(normalized.encode("utf-8")) > 4096:
            raise EmbeddingInputError("Query exceeds 4,096 UTF-8 bytes.")
        if not 1 <= top_k <= 50:
            raise EmbeddingInputError("top_k must be between 1 and 50.")
        return normalized

    @staticmethod
    def _validate_filters(filters: SemanticSearchFilters) -> None:
        for value, limit in (
            (filters.locator_prefix, 2048),
            (filters.language, 64),
            (filters.kind, 32),
        ):
            if value is not None and (not value or len(value.encode("utf-8")) > limit):
                raise EmbeddingInputError("Semantic-search filter is invalid.")

    @staticmethod
    def _check_deadline(deadline: float, message: str) -> None:
        if time.monotonic() >= deadline:
            raise EmbeddingUnavailableError(message)
