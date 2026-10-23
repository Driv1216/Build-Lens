"""Stage 12 laboratory use cases (framework-free; no FastAPI/SQLAlchemy imports).

The service never trusts a worker's claims about evidence:
* items naming a BuildLens chunk are re-bound to the authoritative chunk (locator,
  fingerprint, span) and their text hash must equal the stored content hash;
* items anchored inside an artifact are re-sliced from the Gateway's source text and the
  slice hash must equal the item hash (composite spans are bounds-checked and marked);
* strict vector reuse must show zero bridge inference AND every stored vector digest
  must match what the framework's own persisted store contains;
* packing is recomputed here and compared with the worker's packing.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from buildlens.domain.evaluation import CorpusEvidence, digest
from buildlens.domain.lab import (
    LAB_CONTRACT_VERSION,
    PIPELINES,
    ExperimentConfig,
    LabError,
    LabItem,
    lab_digest,
    pack_context,
    sha256_hex,
)

BUILD_METHODS = frozenset({"embed_documents", "embed_query"})
QUERY_METHODS = frozenset(
    {
        "embed_query",
        "embed_documents",
        "rerank",
        "rerank_chunks",
        "native_candidates",
        "neighbours",
        "tool_call",
        "generate",
    }
)
DOCUMENT_BATCH = 256
DOCUMENT_BATCH_BYTES = 4 * 1024 * 1024


class LabCorpus(Protocol):
    def load(self, project_id: UUID, snapshot_id: UUID) -> CorpusEvidence: ...
    def verify(self, corpus: CorpusEvidence) -> None: ...
    def native_documents(
        self, corpus: CorpusEvidence, filters: dict[str, str | None]
    ) -> tuple[list[Any], Any]: ...
    def stored_vectors(self, corpus: CorpusEvidence) -> dict[str, tuple[float, ...]]: ...
    def artifact_documents(
        self, corpus: CorpusEvidence, filters: dict[str, str | None], *, include_unsupported: bool
    ) -> tuple[list[Any], Any]: ...


class FrameworkWorker(Protocol):
    hello: dict[str, Any] | None
    spawn_ms: float | None
    peak_rss: int | None

    def request(
        self,
        op: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
        callbacks: Any = None,
        allowed_methods: frozenset[str] = frozenset(),
        max_calls: int = 0,
    ) -> Any: ...


class WorkerPool(Protocol):
    def get(self, family: str) -> FrameworkWorker: ...
    def index_directory_root(self) -> str: ...


class Store(Protocol):
    def index_dir(self, index_id: str, create: bool = False) -> Any: ...
    def write_index(self, index_id: str, name: str, value: Any) -> None: ...
    def read_index(self, index_id: str, name: str) -> Any: ...
    def index_exists(self, index_id: str) -> bool: ...


class Bridge(Protocol):
    ledger: Any

    def __call__(self, method: str, params: dict[str, Any], deadline: float) -> Any: ...


BridgeFactory = Callable[..., Bridge]


class NativeRunner(Protocol):
    def run(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        question: str,
        params: dict[str, Any],
        filters: dict[str, str | None],
        corpus: CorpusEvidence | None,
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class CorpusSession:
    corpus: CorpusEvidence
    digest: str
    loaded_at: float
    vectors: dict[str, tuple[float, ...]] | None = None


@dataclass(slots=True)
class EvidenceMaps:
    chunks: dict[str, Any]
    fingerprints: dict[str, str | None]
    source_by_locator: dict[str, str]
    fingerprint_by_locator: dict[str, str | None]

    @staticmethod
    def of(corpus: CorpusEvidence, sources: dict[str, str]) -> EvidenceMaps:
        fingerprints = {
            str(a.id): a.content_fingerprint.digest if a.content_fingerprint else None
            for a in corpus.artifacts
        }
        return EvidenceMaps(
            chunks={str(c.id): c for c in corpus.chunks},
            fingerprints=fingerprints,
            source_by_locator={
                a.locator: sources[str(a.id)] for a in corpus.artifacts if str(a.id) in sources
            },
            fingerprint_by_locator={a.locator: fingerprints[str(a.id)] for a in corpus.artifacts},
        )


def float32_digest(values: tuple[float, ...] | list[float]) -> str:
    return sha256_hex(struct.pack(f"<{len(values)}f", *values))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LabService:
    def __init__(
        self,
        *,
        corpus: LabCorpus,
        workers: WorkerPool,
        store: Store,
        bridges: BridgeFactory,
        native: NativeRunner | None,
        build_timeout_seconds: float = 900,
        query_timeout_seconds: float = 120,
        max_index_documents: int = 50_000,
        max_index_bytes: int = 256 * 1024 * 1024,
        generation_authorized: bool = False,
    ) -> None:
        self.corpus = corpus
        self.workers = workers
        self.store = store
        self.bridges = bridges
        self.native = native
        self.build_timeout = build_timeout_seconds
        self.query_timeout = query_timeout_seconds
        self.max_documents = max_index_documents
        self.max_bytes = max_index_bytes
        self.generation_authorized = generation_authorized
        self._sessions: dict[tuple[UUID, UUID], CorpusSession] = {}
        self._loaded: dict[str, set[str]] = {}  # family -> index ids loaded in the worker
        self._sources: dict[str, dict[str, str]] = {}  # index id -> artifact ref -> text

    # ------------------------------------------------------------------ corpus

    def corpus_session(
        self, project_id: UUID, snapshot_id: UUID, *, refresh: bool = False
    ) -> CorpusSession:
        key = (project_id, snapshot_id)
        session = self._sessions.get(key)
        if session is None or refresh:
            corpus = self.corpus.load(project_id, snapshot_id)
            session = CorpusSession(corpus, digest(corpus.manifest), time.monotonic())
            self._sessions[key] = session
        return session

    def verify_corpus(self, project_id: UUID, snapshot_id: UUID) -> CorpusSession:
        session = self.corpus_session(project_id, snapshot_id)
        self.corpus.verify(session.corpus)
        return session

    # ------------------------------------------------------------------ build

    def index_identity(
        self, config: ExperimentConfig, session: CorpusSession, versions: dict[str, Any]
    ) -> str:
        return lab_digest(
            {
                "contract": LAB_CONTRACT_VERSION,
                "ingestion": config.ingestion_identity(),
                "corpus_digest": session.digest,
                "framework_versions": versions,
            }
        )

    def _documents(
        self, config: ExperimentConfig, session: CorpusSession
    ) -> tuple[list[Any], dict[str, Any]]:
        if config.corpus_mode == "native_chunks":
            documents, report = self.corpus.native_documents(session.corpus, config.filters)
            if config.embedding_source in {"reuse-stored", "reuse-or-compute"}:
                if session.vectors is None:
                    session.vectors = self.corpus.stored_vectors(session.corpus)
                documents = [
                    type(d)(d.ref, d.text, d.metadata, session.vectors[d.ref]) for d in documents
                ]
        else:
            documents, report = self.corpus.artifact_documents(
                session.corpus, config.filters, include_unsupported=config.include_unsupported
            )
        total = sum(len(d.text.encode()) for d in documents)
        if len(documents) > self.max_documents or total > self.max_bytes:
            raise LabError("LAB_BUDGET_EXCEEDED", "Index exceeds configured document/byte caps")
        return documents, {
            "mode": report.mode,
            "documents": len(documents),
            "bytes": total,
            "skipped": dict(sorted(report.skipped.items())),
        }

    def build(
        self, config: ExperimentConfig, project_id: UUID, snapshot_id: UUID
    ) -> dict[str, Any]:
        spec = PIPELINES[config.pipeline]
        if spec.family == "native":
            return {"index_id": None, "pipeline": config.pipeline, "native": True}
        session = self.verify_corpus(project_id, snapshot_id)
        worker = self.workers.get(spec.family)
        if worker.hello is None:
            worker.request("drop", {"index_id": ""}, timeout_seconds=60)
        versions = dict((worker.hello or {}).get("versions") or {})
        index_id = self.index_identity(config, session, versions)
        if index_id in self._loaded.get(spec.family, set()) and self.store.index_exists(index_id):
            manifest = self.store.read_index(index_id, "index.json")
            return {**manifest, "reused_loaded_index": True}
        started = time.perf_counter()
        if spec.consumes_documents:
            documents, gateway = self._documents(config, session)
        else:
            documents, gateway = [], {"mode": "none", "documents": 0, "bytes": 0, "skipped": {}}
        directory = self.store.index_dir(index_id, create=True)
        include_vectors = spec.consumes_documents and config.embedding_source in {
            "reuse-stored",
            "reuse-or-compute",
        }
        bridge = self.bridges(
            corpus=session.corpus,
            project_id=project_id,
            snapshot_id=snapshot_id,
            expected_contract_digest=session.corpus.embedding.contract.digest
            if include_vectors
            else None,
            fake=config.embedding_source == "deterministic-fake",
        )
        worker.request(
            "build_begin",
            {
                "index_id": index_id,
                "config": config.as_dict(),
                "directory": str(directory),
                "corpus_mode": config.corpus_mode,
                "embedding_source": config.embedding_source,
            },
            timeout_seconds=60,
        )
        batch: list[dict[str, Any]] = []
        size = 0
        for document in documents:
            wire = document.wire(include_vectors)
            weight = len(document.text.encode()) + (
                len(document.vector or ()) * 24 if include_vectors else 0
            )
            if batch and (len(batch) >= DOCUMENT_BATCH or size + weight > DOCUMENT_BATCH_BYTES):
                worker.request("add_documents", {"documents": batch}, timeout_seconds=120)
                batch, size = [], 0
            batch.append(wire)
            size += weight
        if batch:
            worker.request("add_documents", {"documents": batch}, timeout_seconds=120)
        call = worker.request(
            "build_commit",
            {},
            timeout_seconds=self.build_timeout,
            callbacks=bridge,
            allowed_methods=BUILD_METHODS,
            max_calls=200_000,
        )
        manifest = dict(call.value)
        records = manifest.pop("embedding_records", [])
        vector_digests = manifest.pop("vector_digests", {})
        verification = self._verify_reuse(
            config, session, documents, manifest, vector_digests, bridge.ledger.as_dict()
        )
        sources = (
            {d.ref: d.text for d in documents} if config.corpus_mode != "native_chunks" else {}
        )
        self._sources[index_id] = sources
        result = {
            "index_id": index_id,
            "pipeline": config.pipeline,
            "config": config.as_dict(),
            "config_digest": config.digest,
            "corpus_digest": session.digest,
            "built_at": _now(),
            "framework_versions": versions,
            "gateway": gateway,
            "nodes": manifest.get("nodes"),
            "worker_manifest": manifest,
            "embedding": manifest.get("embedding"),
            "reuse_verification": verification,
            "bridge": bridge.ledger.as_dict(),
            "build_ms": round((time.perf_counter() - started) * 1000, 1),
            "worker_ms": round(call.elapsed_ms, 1),
            "bridge_ms": round(call.bridge_ms, 1),
            "worker_spawn_ms": worker.spawn_ms,
            "worker_peak_rss": worker.peak_rss,
            "steps": call.steps,
            "guard_denials": call.guard_denials,
        }
        self.store.write_index(index_id, "index.json", result)
        self.store.write_index(index_id, "embedding-transformations.json", {"records": records})
        if sources:
            self.store.write_index(index_id, "sources.json", sources)
        self._loaded.setdefault(spec.family, set()).add(index_id)
        return result

    def _verify_reuse(
        self,
        config: ExperimentConfig,
        session: CorpusSession,
        documents: list[Any],
        manifest: dict[str, Any],
        digests: dict[str, str],
        ledger: dict[str, Any],
    ) -> dict[str, Any]:
        if config.embedding_source not in {"reuse-stored", "reuse-or-compute"} or not documents:
            return {"applicable": False, "computed_via_bridge": ledger["texts_embedded"]}
        expected = {d.ref: float32_digest(d.vector) for d in documents if d.vector is not None}
        checked = [ref for ref in expected if ref in digests]
        mismatched = [ref for ref in checked if digests[ref] != expected[ref]]
        embedding = manifest.get("embedding") or {}
        result = {
            "applicable": True,
            "source": config.embedding_source,
            "stored_vectors": len(expected),
            "framework_vectors_compared": len(checked),
            "digest_mismatches": len(mismatched),
            "worker_reused": embedding.get("reused"),
            "worker_computed": embedding.get("computed_via_bridge"),
            "bridge_texts_embedded": ledger["texts_embedded"],
            "transformations": embedding.get("transformations"),
        }
        strict = config.embedding_source == "reuse-stored"
        genuine = (
            ledger["texts_embedded"] == 0
            and not mismatched
            and len(checked) == len(expected)
            and embedding.get("computed_via_bridge") == 0
        )
        result["verified_genuine_reuse"] = genuine
        if strict and not genuine:
            raise LabError(
                "LAB_VECTOR_REUSE_UNVERIFIED",
                f"reuse verification failed: {sorted(result.items())}",
            )
        # reuse-or-compute: vectors for transformed embed text are legitimately recomputed,
        # so differing digests are reported (never silently presented as reuse).
        result["vectors_differing_from_stored"] = len(mismatched)
        return result

    # ------------------------------------------------------------------ query

    def query(
        self,
        config: ExperimentConfig,
        project_id: UUID,
        snapshot_id: UUID,
        question: str,
        *,
        reload_from_storage: bool = False,
    ) -> dict[str, Any]:
        if not question.strip() or len(question.encode()) > 4096:
            raise LabError("LAB_QUESTION_INVALID")
        spec = PIPELINES[config.pipeline]
        session = self.corpus_session(project_id, snapshot_id)
        started = time.perf_counter()
        if spec.family == "native":
            if self.native is None:
                raise LabError("LAB_NATIVE_UNAVAILABLE")
            raw = self.native.run(
                project_id, snapshot_id, question, config.params, config.filters, session.corpus
            )
            result = {
                "pipeline": config.pipeline,
                "config_digest": config.digest,
                "question": question,
                "retrieved": [i.as_dict() for i in raw["retrieved"]],
                "ranked": [i.as_dict() for i in raw["ranked"]],
                "context": [i.as_dict() for i in raw["context"]],
                "packing": {"version": "stage9.context.v1", "note": "native canonical-JSON budget"},
                "steps": raw["steps"],
                "answer": None,
                "info": raw["info"],
                "timings": {
                    **raw["timings"],
                    "total_ms": round((time.perf_counter() - started) * 1000, 3),
                },
                "texts": raw["texts"],
                "verification": {"native": True},
            }
            return result
        manifest = self.build(config, project_id, snapshot_id)
        index_id = manifest["index_id"]
        worker = self.workers.get(spec.family)
        bridge = self.bridges(
            corpus=session.corpus,
            project_id=project_id,
            snapshot_id=snapshot_id,
            expected_contract_digest=session.corpus.embedding.contract.digest
            if config.embedding_source in {"reuse-stored", "reuse-or-compute"}
            else None,
            fake=config.embedding_source == "deterministic-fake",
            tool_budget_bytes=config.params.get("evidence_budget_bytes", 64_000),
            tool_top_k=config.params.get("tool_top_k", 10),
        )
        try:
            call = worker.request(
                "query",
                {
                    "index_id": index_id,
                    "question": question,
                    "config": config.as_dict(),
                    "reload_from_storage": reload_from_storage,
                },
                timeout_seconds=self.query_timeout,
                callbacks=bridge,
                allowed_methods=QUERY_METHODS
                if self.generation_authorized
                else QUERY_METHODS - {"generate"},
                max_calls=500,
            )
        except LabError as error:
            if error.code != "LAB_INDEX_NOT_LOADED":
                raise
            self._loaded.get(spec.family, set()).discard(index_id)
            self.build(config, project_id, snapshot_id)
            call = worker.request(
                "query",
                {"index_id": index_id, "question": question, "config": config.as_dict()},
                timeout_seconds=self.query_timeout,
                callbacks=bridge,
                allowed_methods=QUERY_METHODS - {"generate"},
                max_calls=500,
            )
        value = call.value
        sources = self._sources.get(index_id)
        if sources is None and config.corpus_mode != "native_chunks":
            sources = self.store.read_index(index_id, "sources.json")
            self._sources[index_id] = sources
        verification: dict[str, Any] = {
            "rebound_chunks": 0,
            "verified_slices": 0,
            "rejected_anchors": 0,
            "composite": 0,
        }
        texts: dict[str, str] = dict(value.get("texts") or {})
        maps = EvidenceMaps.of(session.corpus, sources or {})
        lists = {
            name: [
                self._verify_item(dict(raw), maps, texts, verification)
                for raw in value.get(name) or []
            ]
            for name in ("retrieved", "ranked", "candidates")
        }
        packed, packing = pack_context([LabItem.from_dict(c) for c in lists["candidates"]])
        packing["matches_worker"] = [p.ref for p in packed] == list(value.get("packed_refs") or [])
        by_ref = {c["ref"]: c for c in lists["candidates"]}
        context = [dict(by_ref[p.ref], rank=i) for i, p in enumerate(packed, 1)]
        return {
            "pipeline": config.pipeline,
            "config_digest": config.digest,
            "index_id": index_id,
            "question": question,
            "retrieved": lists["retrieved"],
            "ranked": lists["ranked"],
            "context": context,
            "packing": packing,
            "steps": call.steps,
            "answer": value.get("answer"),
            "info": value.get("info") or {},
            "embedding": value.get("embedding"),
            "bridge": bridge.ledger.as_dict(),
            "guard_denials": call.guard_denials,
            "timings": {
                "total_ms": round((time.perf_counter() - started) * 1000, 3),
                "worker_ms": round(call.elapsed_ms, 3),
                "bridge_ms": round(call.bridge_ms, 3),
            },
            "texts": texts,
            "verification": verification,
        }

    @staticmethod
    def _verify_item(
        raw: dict[str, Any],
        maps: EvidenceMaps,
        texts: dict[str, str],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        chunk_id = raw.get("chunk_id")
        detail = dict(raw.get("detail") or {})
        if chunk_id:
            chunk = maps.chunks.get(chunk_id)
            if chunk is None:
                raise LabError("LAB_ITEM_OUTSIDE_CORPUS")
            if not detail.get("reference_only") and raw.get("text_sha256") != chunk.content_hash:
                # A framework returned a chunk id with different text: never score it as that chunk.
                detail["chunk_text_mismatch"] = True
                raw.update({"chunk_id": None, "anchored": False, "span": None, "detail": detail})
                verification["rejected_anchors"] += 1
                return raw
            raw.update(
                {
                    "text_sha256": chunk.content_hash,
                    "text_bytes": len(chunk.content.encode()),
                    "locator": chunk.locator,
                    "artifact_fingerprint": maps.fingerprints.get(str(chunk.artifact_id)),
                    "span": {
                        "start_offset": chunk.span.start_offset,
                        "end_offset": chunk.span.end_offset,
                    },
                    "anchored": True,
                }
            )
            texts.setdefault(chunk.content_hash, chunk.content)
            detail["anchor_method"] = "native"
            raw["detail"] = detail
            verification["rebound_chunks"] += 1
            return raw
        span = raw.get("span")
        if raw.get("anchored") and span:
            source = maps.source_by_locator.get(str(raw.get("locator")))
            start, end = span["start_offset"], span["end_offset"]
            if source is None or not (0 <= start < end <= len(source)):
                raw["anchored"], raw["span"] = False, None
                verification["rejected_anchors"] += 1
            elif detail.get("anchor_method") == "composite":
                verification["composite"] += 1
            elif sha256_hex(source[start:end]) != raw.get("text_sha256"):
                raw["anchored"], raw["span"] = False, None
                verification["rejected_anchors"] += 1
            else:
                verification["verified_slices"] += 1
            if source is not None:
                raw["artifact_fingerprint"] = maps.fingerprint_by_locator.get(
                    str(raw.get("locator"))
                )
        raw["detail"] = detail
        return raw

    def drop_loaded(self) -> None:
        self._loaded.clear()
