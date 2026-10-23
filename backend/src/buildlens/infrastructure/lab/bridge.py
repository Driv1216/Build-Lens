"""Capability bridge: the parent-side implementation of every worker callback.

Workers hold no credentials and no database/repository access. Whatever a framework needs
from BuildLens (embeddings, reranking, native retrieval, read-only tools, generation) is a
callback served here, under BuildLens's own locks, contracts, budgets and authority.
Every transfer is size-checked; any violation is an allow-listed error code.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from buildlens.application.context import ContextService, RerankerRuntime
from buildlens.application.embeddings import EmbeddingRuntime
from buildlens.application.retrieval import RetrievalService
from buildlens.domain.evaluation import CorpusEvidence
from buildlens.domain.lab import LabError, sha256_hex
from buildlens.domain.processing import Chunk, ChunkSpan
from buildlens.domain.retrieval import RetrievalFilters, RetrievalHit

MAX_EMBED_TEXTS = 64
MAX_TEXT_BYTES = 256 * 1024
MAX_BATCH_BYTES = 4 * 1024 * 1024
MAX_QUERY_BYTES = 4096
MAX_RERANK_ITEMS = 64
MAX_CANDIDATE_CONTENT_BYTES = 4 * 1024 * 1024
TOOL_NAMES = ("buildlens_search", "buildlens_read_chunk", "buildlens_context")
_NAMESPACE = uuid.UUID("6f2b3b1e-6a5f-4d0c-9a51-1c2b5d1e0c12")


@dataclass(slots=True)
class BridgeLedger:
    calls: dict[str, int] = field(default_factory=dict)
    texts_embedded: int = 0
    queries_embedded: int = 0
    rerank_items: int = 0
    tool_bytes: int = 0
    seconds: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": dict(sorted(self.calls.items())),
            "texts_embedded": self.texts_embedded,
            "queries_embedded": self.queries_embedded,
            "rerank_items": self.rerank_items,
            "tool_bytes": self.tool_bytes,
            "seconds": {k: round(v, 4) for k, v in sorted(self.seconds.items())},
        }


def _text(params: dict[str, Any], key: str, limit: int) -> str:
    value = params.get(key)
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise LabError("LAB_TRANSFER_INVALID", f"{key} must be a string of at most {limit} bytes")
    return value


def lab_chunk(ref: str, text: str, ordinal: int) -> Chunk:
    """A transient Chunk for reranking framework-produced nodes (never persisted)."""
    return Chunk(
        id=uuid.uuid5(_NAMESPACE, ref),
        artifact_processing_id=uuid.uuid5(_NAMESPACE, "lab"),
        stable_key=sha256_hex(ref),
        ordinal=ordinal,
        unit_anchor="lab",
        part_index=0,
        kind="lab",
        content=text,
        content_hash=sha256_hex(text),
        span=ChunkSpan(0, len(text), 1, 1, 0, 0),
    )


def hit_row(hit: RetrievalHit) -> dict[str, Any]:
    chunk = hit.chunk
    return {
        "chunk_id": str(chunk.id),
        "rank": hit.rank,
        "score": hit.score,
        "score_kind": hit.score_kind,
        "content": chunk.content,
        "locator": chunk.locator,
        "start_offset": chunk.span.start_offset,
        "end_offset": chunk.span.end_offset,
        "stable_key": chunk.stable_key,
        "kind": chunk.kind,
        "exact_rank": hit.exact_rank,
        "lexical_rank": hit.lexical.rank if hit.lexical else None,
        "semantic_rank": hit.semantic.rank if hit.semantic else None,
        "lexical_rrf": hit.lexical_rrf_contribution,
        "semantic_rrf": hit.semantic_rrf_contribution,
    }


class CapabilityBridge:
    def __init__(
        self,
        *,
        embedding: EmbeddingRuntime,
        reranker: RerankerRuntime | None = None,
        retrieval: RetrievalService | None = None,
        context: ContextService | None = None,
        project_id: UUID | None = None,
        snapshot_id: UUID | None = None,
        corpus: CorpusEvidence | None = None,
        expected_contract_digest: str | None = None,
        tool_budget_bytes: int = 64_000,
        tool_top_k: int = 10,
        generation_authorized: bool = False,
    ) -> None:
        self.embedding = embedding
        self.reranker = reranker
        self.retrieval = retrieval
        self.context = context
        self.project_id = project_id
        self.snapshot_id = snapshot_id
        self.corpus = corpus
        self.chunks = {str(c.id): c for c in corpus.chunks} if corpus else {}
        self.expected_contract_digest = expected_contract_digest
        self.tool_budget_bytes = tool_budget_bytes
        self.tool_top_k = tool_top_k
        self.generation_authorized = generation_authorized
        self.ledger = BridgeLedger()

    # The supervisor calls the bridge as `callbacks(method, params, deadline)`.
    def __call__(self, method: str, params: dict[str, Any], deadline: float) -> Any:
        if time.monotonic() >= deadline:
            raise LabError("LAB_WORKER_TIMEOUT")
        handler = getattr(self, "_" + method, None)
        if handler is None:
            raise LabError("LAB_CALLBACK_NOT_ALLOWED", method)
        self.ledger.calls[method] = self.ledger.calls.get(method, 0) + 1
        started = time.perf_counter()
        try:
            return handler(params, deadline)
        finally:
            self.ledger.seconds[method] = self.ledger.seconds.get(method, 0.0) + (
                time.perf_counter() - started
            )

    # ------------------------------------------------------------------ embeddings

    def _check_contract(self) -> None:
        if (
            self.expected_contract_digest is not None
            and self.embedding.provider.contract.digest != self.expected_contract_digest
        ):
            raise LabError(
                "LAB_EMBEDDING_CONTRACT_MISMATCH",
                "Live embedding provider differs from the stored embedding run contract",
            )

    def _embed_documents(self, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        texts = params.get("texts")
        if not isinstance(texts, list) or not 1 <= len(texts) <= MAX_EMBED_TEXTS:
            raise LabError("LAB_TRANSFER_INVALID", "texts must hold 1-64 strings")
        total = 0
        for text in texts:
            if not isinstance(text, str) or len(text.encode()) > MAX_TEXT_BYTES:
                raise LabError("LAB_TRANSFER_INVALID", "text too large")
            total += len(text.encode())
        if total > MAX_BATCH_BYTES:
            raise LabError("LAB_TRANSFER_INVALID", "batch too large")
        self._check_contract()
        if not self.embedding.lock.acquire(blocking=False):
            raise LabError("LAB_EMBEDDING_BUSY")
        try:
            vectors = self.embedding.provider.embed_documents(list(texts))
        finally:
            self.embedding.lock.release()
        self.ledger.texts_embedded += len(texts)
        return {"vectors": [list(v.values) for v in vectors]}

    def _embed_query(self, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        text = _text(params, "text", MAX_QUERY_BYTES)
        self._check_contract()
        if not self.embedding.lock.acquire(blocking=False):
            raise LabError("LAB_EMBEDDING_BUSY")
        try:
            vector = self.embedding.provider.embed_query(text)
        finally:
            self.embedding.lock.release()
        self.ledger.queries_embedded += 1
        return {"vector": list(vector.values)}

    # ------------------------------------------------------------------ reranking

    def _score(self, query: str, chunks: list[Chunk], deadline: float) -> list[Any]:
        if self.reranker is None:
            raise LabError("LAB_RERANKER_UNAVAILABLE")
        if not self.reranker.lock.acquire(blocking=False):
            raise LabError("LAB_RERANKER_BUSY")
        try:
            timeout = max(0.5, deadline - time.monotonic())
            return self.reranker.provider.score(query, chunks, timeout)
        finally:
            self.reranker.lock.release()

    def _rerank(self, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        query = _text(params, "query", MAX_QUERY_BYTES)
        items = params.get("items")
        if not isinstance(items, list) or len(items) > MAX_RERANK_ITEMS:
            raise LabError("LAB_TRANSFER_INVALID", "items must hold at most 64 entries")
        chunks = [
            lab_chunk(str(item["ref"]), _text(item, "text", MAX_TEXT_BYTES), index)
            for index, item in enumerate(items)
        ]
        refs = {c.id: str(item["ref"]) for c, item in zip(chunks, items, strict=True)}
        results = self._score(query, chunks, deadline) if chunks else []
        self.ledger.rerank_items += len(chunks)
        return {
            "contract": self.reranker.provider.contract.model_id if self.reranker else None,
            "scores": [
                {
                    "ref": refs[r.chunk_id],
                    "score": r.score,
                    "window_count": r.window_count,
                    "excluded": r.exclusion_reason,
                }
                for r in results
            ],
        }

    def _rerank_chunks(self, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        query = _text(params, "query", MAX_QUERY_BYTES)
        ids = params.get("chunk_ids")
        if not isinstance(ids, list) or len(ids) > MAX_RERANK_ITEMS:
            raise LabError("LAB_TRANSFER_INVALID")
        chunks = [self._chunk(str(i)) for i in ids]
        results = self._score(query, chunks, deadline) if chunks else []
        self.ledger.rerank_items += len(chunks)
        return {
            "scores": [
                {"chunk_id": str(r.chunk_id), "score": r.score, "excluded": r.exclusion_reason}
                for r in results
            ]
        }

    # ------------------------------------------------------------------ native evidence

    def _chunk(self, chunk_id: str) -> Chunk:
        chunk = self.chunks.get(chunk_id)
        if chunk is None:
            raise LabError("LAB_CHUNK_OUTSIDE_CORPUS")
        return chunk

    def _filters(self, params: dict[str, Any]) -> RetrievalFilters:
        raw = params.get("filters") or {}
        return RetrievalFilters(raw.get("locator_prefix"), raw.get("language"), raw.get("kind"))

    def _native_candidates(self, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        if self.retrieval is None or self.project_id is None or self.snapshot_id is None:
            raise LabError("LAB_NATIVE_RETRIEVAL_UNAVAILABLE")
        query = _text(params, "query", MAX_QUERY_BYTES)
        mode = params.get("mode", "hybrid")
        if mode not in {"exact", "lexical", "semantic", "hybrid"}:
            raise LabError("LAB_TRANSFER_INVALID", "mode")
        result = self.retrieval.context_candidates(
            self.project_id, self.snapshot_id, mode, query, self._filters(params)
        )
        if self.corpus is not None and (
            result.processing_run.id != self.corpus.processing.id
            or (
                result.embedding_run is not None
                and result.embedding_run.id != self.corpus.embedding.id
            )
        ):
            raise LabError("LAB_CORPUS_CHANGED")
        rows = [hit_row(hit) for hit in result.hits]
        if sum(len(r["content"].encode()) for r in rows) > MAX_CANDIDATE_CONTENT_BYTES:
            raise LabError("LAB_TRANSFER_TOO_LARGE")
        return {
            "requested_mode": result.requested_mode,
            "executed_mode": result.executed_mode,
            "hits": rows,
            "truncated": result.final_candidate_cap_truncated,
            "components": {
                name: {"state": c.state, "candidates": c.candidate_count, "truncated": c.truncated}
                for name, c in (
                    ("exact", result.exact),
                    ("lexical", result.lexical),
                    ("semantic", result.semantic),
                )
            },
        }

    def _neighbours(self, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        """One-hop adjacent split parts of the same logical unit (frozen corpus)."""
        ids = params.get("chunk_ids")
        if not isinstance(ids, list) or len(ids) > 32:
            raise LabError("LAB_TRANSFER_INVALID")
        seeds = [self._chunk(str(i)) for i in ids]
        result = []
        for seed in seeds:
            for other in self.chunks.values():
                if (
                    other.artifact_processing_id == seed.artifact_processing_id
                    and other.unit_anchor == seed.unit_anchor
                    and abs(other.part_index - seed.part_index) == 1
                ):
                    result.append({"seed": str(seed.id), "chunk_id": str(other.id)})
        return {"neighbours": sorted(result, key=lambda r: (r["seed"], r["chunk_id"]))}

    # ------------------------------------------------------------------ tools (read-only)

    def _spend(self, payload: dict[str, Any]) -> dict[str, Any]:
        size = len(repr(payload).encode())
        if self.ledger.tool_bytes + size > self.tool_budget_bytes:
            raise LabError("LAB_TOOL_BUDGET_EXCEEDED")
        self.ledger.tool_bytes += size
        return payload

    def _tool_call(self, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("args") or {}
        if name not in TOOL_NAMES or not isinstance(args, dict):
            raise LabError("LAB_TOOL_NOT_ALLOWED")
        if name == "buildlens_read_chunk":
            chunk = self._chunk(str(args.get("chunk_id", "")))
            return self._spend(
                {
                    "chunk_id": str(chunk.id),
                    "locator": chunk.locator,
                    "start_offset": chunk.span.start_offset,
                    "end_offset": chunk.span.end_offset,
                    "content": chunk.content,
                }
            )
        query = _text(args, "query", MAX_QUERY_BYTES)
        if name == "buildlens_search":
            if self.retrieval is None or self.project_id is None or self.snapshot_id is None:
                raise LabError("LAB_NATIVE_RETRIEVAL_UNAVAILABLE")
            mode = args.get("mode", "hybrid")
            if mode not in {"exact", "lexical", "semantic", "hybrid"}:
                raise LabError("LAB_TRANSFER_INVALID", "mode")
            top_k = min(int(args.get("top_k", self.tool_top_k)), self.tool_top_k)
            result = self.retrieval.search(
                self.project_id, self.snapshot_id, mode, query, max(1, top_k)
            )
            return self._spend(
                {
                    "hits": [
                        {
                            "chunk_id": str(h.chunk.id),
                            "rank": h.rank,
                            "locator": h.chunk.locator,
                            "score": h.score,
                            "preview": h.chunk.content[:300],
                        }
                        for h in result.hits
                        if not self.chunks or str(h.chunk.id) in self.chunks
                    ]
                }
            )
        if self.context is None or self.project_id is None or self.snapshot_id is None:
            raise LabError("LAB_NATIVE_CONTEXT_UNAVAILABLE")
        built = self.context.build(
            self.project_id, self.snapshot_id, query, "hybrid", "retrieval_order"
        )
        return self._spend(
            {
                "items": [
                    {
                        "chunk_id": str(i.chunk.id),
                        "locator": i.chunk.locator,
                        "content": i.chunk.content,
                    }
                    for i in built.items
                ]
            }
        )

    # ------------------------------------------------------------------ generation

    def _generate(self, params: dict[str, Any], deadline: float) -> dict[str, Any]:
        if not self.generation_authorized:
            raise LabError(
                "LAB_GENERATION_NOT_AUTHORIZED",
                "Real generation requires an explicit authorization record and flag",
            )
        raise LabError("LAB_GENERATION_NOT_IMPLEMENTED")
