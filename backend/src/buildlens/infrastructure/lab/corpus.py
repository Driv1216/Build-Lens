"""Corpus Gateway: the only way BuildLens evidence enters a framework worker.

* `native_chunks`: authoritative Stage 11 corpus load (freshness, contract, ownership,
  embedding membership) -> one document per BuildLens chunk with full provenance.
* stored vectors: read-only from the active embedding run, each row re-verified against
  its chunk (input hash = sha256(content), contract digest, dimensions, finite, unit norm)
  before it may be reused; authority is re-checked around the read.
* `artifact_sources`: whole artifacts read through the supervised secure reader (no-follow,
  eligibility, size limits), checked against the snapshot fingerprint, decoded exactly as
  processing decodes them, and secret-screened. Framework file loaders never see the
  repository.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import select

from buildlens.application.source_ports import SourceReadError
from buildlens.domain.eligibility import exclusion_reason
from buildlens.domain.evaluation import CorpusEvidence
from buildlens.domain.lab import LabError, sha256_hex
from buildlens.domain.processing import Chunk
from buildlens.domain.source import ContentRejectedError, decode_source, screen_content
from buildlens.infrastructure.evaluation.corpus import SqlAlchemyEvaluationCorpus
from buildlens.infrastructure.persistence.models import ChunkEmbeddingRecord, ProjectVersionRecord
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader

MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 96 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LabDocument:
    ref: str
    text: str
    metadata: dict[str, Any]
    vector: tuple[float, ...] | None = None

    def wire(self, include_vector: bool) -> dict[str, Any]:
        value: dict[str, Any] = {"ref": self.ref, "text": self.text, "metadata": self.metadata}
        if include_vector and self.vector is not None:
            value["vector"] = list(self.vector)
        return value


@dataclass(slots=True)
class GatewayReport:
    mode: str
    documents: int = 0
    bytes: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    vectors_verified: int = 0

    def skip(self, code: str) -> None:
        self.skipped[code] = self.skipped.get(code, 0) + 1


def chunk_document(
    chunk: Chunk, corpus: CorpusEvidence, languages: dict[UUID, str | None]
) -> LabDocument:
    artifact = next(a for a in corpus.artifacts if a.id == chunk.artifact_id)
    fingerprint = artifact.content_fingerprint.digest if artifact.content_fingerprint else None
    return LabDocument(
        ref=str(chunk.id),
        text=chunk.content,
        metadata={
            "chunk_id": str(chunk.id),
            "stable_key": chunk.stable_key,
            "locator": chunk.locator,
            "artifact_fingerprint": fingerprint,
            "start_offset": chunk.span.start_offset,
            "end_offset": chunk.span.end_offset,
            "kind": chunk.kind,
            "language": languages.get(chunk.artifact_id) if chunk.artifact_id else None,
            "heading_path": list(chunk.heading_path),
            "json_path": chunk.json_path,
            "unit_anchor": chunk.unit_anchor,
            "part_index": chunk.part_index,
            "ordinal": chunk.ordinal,
            "artifact_processing_id": str(chunk.artifact_processing_id),
        },
    )


class CorpusGateway:
    def __init__(self, session_factory: Any, reader: Any | None = None) -> None:
        self._session_factory = session_factory
        self._evaluation = SqlAlchemyEvaluationCorpus(session_factory)
        self._reader = reader

    # ------------------------------------------------------------------ authority

    def load(self, project_id: UUID, snapshot_id: UUID) -> CorpusEvidence:
        return self._evaluation.load(project_id, snapshot_id)

    def verify(self, corpus: CorpusEvidence) -> None:
        try:
            self._evaluation.verify(corpus)
        except ValueError as error:
            raise LabError("LAB_CORPUS_CHANGED") from error

    # ------------------------------------------------------------------ native chunks

    @staticmethod
    def languages(corpus: CorpusEvidence) -> dict[UUID, str | None]:
        return {a.id: a.metadata.language if a.metadata else None for a in corpus.artifacts}

    def native_documents(
        self, corpus: CorpusEvidence, filters: dict[str, str | None]
    ) -> tuple[list[LabDocument], GatewayReport]:
        report = GatewayReport("native_chunks")
        languages = self.languages(corpus)
        documents = []
        for chunk in sorted(corpus.chunks, key=lambda c: (c.locator or "", c.ordinal, str(c.id))):
            if not _matches(
                chunk.locator, languages.get(chunk.artifact_id or UUID(int=0)), chunk.kind, filters
            ):
                report.skip("FILTERED")
                continue
            if hashlib.sha256(chunk.content.encode()).hexdigest() != chunk.content_hash:
                raise LabError("LAB_CHUNK_INTEGRITY_INVALID")
            documents.append(chunk_document(chunk, corpus, languages))
            report.bytes += len(chunk.content.encode())
        report.documents = len(documents)
        return documents, report

    def stored_vectors(self, corpus: CorpusEvidence) -> dict[str, tuple[float, ...]]:
        """chunk_id -> verified stored vector from the active embedding run."""
        chunks = {c.id: c for c in corpus.chunks}
        run = corpus.embedding
        expected_dimensions = run.contract.dimensions
        result: dict[str, tuple[float, ...]] = {}
        with self._session_factory() as session:
            active = session.execute(
                select(ProjectVersionRecord.active_embedding_run_id).where(
                    ProjectVersionRecord.id == corpus.snapshot.id
                )
            ).scalar_one_or_none()
            if active != run.id:
                raise LabError("LAB_CORPUS_CHANGED")
            rows = session.execute(
                select(
                    ChunkEmbeddingRecord.chunk_id,
                    ChunkEmbeddingRecord.input_hash,
                    ChunkEmbeddingRecord.contract_digest,
                    ChunkEmbeddingRecord.dimensions,
                    ChunkEmbeddingRecord.embedding,
                ).where(ChunkEmbeddingRecord.embedding_run_id == run.id)
            ).all()
        for chunk_id, input_hash, contract_digest, dimensions, embedding in rows:
            chunk = chunks.get(chunk_id)
            values = tuple(float(v) for v in embedding)
            norm = math.sqrt(sum(v * v for v in values))
            if (
                chunk is None
                or input_hash != chunk.content_hash
                or input_hash != sha256_hex(chunk.content)
                or contract_digest != run.contract.digest
                or dimensions != expected_dimensions
                or len(values) != expected_dimensions
                or not all(math.isfinite(v) for v in values)
                or abs(norm - 1.0) > 1e-4
            ):
                raise LabError("LAB_STORED_VECTOR_INVALID")
            result[str(chunk_id)] = values
        if set(result) != {str(c) for c in chunks}:
            raise LabError("LAB_STORED_VECTOR_MEMBERSHIP_INVALID")
        self.verify(corpus)
        return result

    # ------------------------------------------------------------------ artifact sources

    def artifact_documents(
        self,
        corpus: CorpusEvidence,
        filters: dict[str, str | None],
        *,
        include_unsupported: bool,
        timeout_seconds: float = 3.0,
    ) -> tuple[list[LabDocument], GatewayReport]:
        report = GatewayReport("artifact_sources")
        snapshot = corpus.snapshot
        if not snapshot.canonical_root:
            raise LabError("LAB_SNAPSHOT_ROOT_MISSING")
        observed = snapshot.root_observation
        expected = (int(str(observed["device"])), int(str(observed["inode"]))) if observed else None
        outcomes = {
            row["artifact_id"]: row["outcome"] for row in corpus.manifest["processing_artifacts"]
        }
        reader = self._reader or SupervisedSourceReader(timeout=timeout_seconds)
        documents: list[LabDocument] = []
        consumed = 0
        try:
            for artifact in sorted(corpus.artifacts, key=lambda a: a.locator):
                meta = artifact.metadata
                outcome = outcomes.get(str(artifact.id))
                if meta is None or artifact.content_fingerprint is None:
                    continue
                if outcome not in {"processed", "empty"}:
                    if not (include_unsupported and outcome == "unsupported"):
                        report.skip(f"PROCESSING_{str(outcome).upper()}")
                        continue
                if (
                    meta.inventory_status != "included"
                    or meta.pruned
                    or meta.exclusion_reasons
                    or exclusion_reason(artifact.locator, False)
                ):
                    report.skip("NOT_ELIGIBLE")
                    continue
                if not _matches(artifact.locator, meta.language, None, filters):
                    report.skip("FILTERED")
                    continue
                if consumed >= MAX_TOTAL_SOURCE_BYTES:
                    report.skip("TOTAL_SOURCE_LIMIT")
                    continue
                try:
                    raw = reader.read(
                        snapshot.canonical_root,
                        artifact.locator,
                        min(MAX_ARTIFACT_BYTES, MAX_TOTAL_SOURCE_BYTES - consumed),
                        expected,
                    ).data
                except SourceReadError as error:
                    report.skip(error.code)
                    continue
                consumed += len(raw)
                if hashlib.sha256(raw).hexdigest() != artifact.content_fingerprint.digest:
                    report.skip("SOURCE_DIFFERS_FROM_SNAPSHOT")
                    continue
                try:
                    text, encoding = decode_source(raw, artifact.locator.endswith(".py"))
                    screen_content(text)
                except ContentRejectedError:
                    report.skip("SECRET_SCREEN_REJECTED")
                    continue
                except (UnicodeDecodeError, SyntaxError, LookupError):
                    report.skip("DECODE_FAILED")
                    continue
                if not text:
                    report.skip("EMPTY")
                    continue
                documents.append(
                    LabDocument(
                        ref=str(artifact.id),
                        text=text,
                        metadata={
                            "artifact_id": str(artifact.id),
                            "locator": artifact.locator,
                            "artifact_fingerprint": artifact.content_fingerprint.digest,
                            "language": meta.language,
                            "suffix": PurePosixPath(artifact.locator).suffix.lower(),
                            "encoding": encoding,
                            "processing_outcome": outcome,
                        },
                    )
                )
                report.bytes += len(raw)
        finally:
            if self._reader is None:
                reader.close()
        report.documents = len(documents)
        return documents, report


def _matches(
    locator: str | None, language: str | None, kind: str | None, filters: dict[str, str | None]
) -> bool:
    prefix = filters.get("locator_prefix")
    if prefix and not (locator or "").startswith(prefix):
        return False
    if filters.get("language") and language != filters["language"]:
        return False
    if filters.get("kind") and kind is not None and kind != filters["kind"]:
        return False
    return True
