"""Read-only frozen evidence adapter using existing publication-authority gates."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from buildlens.application.context import ContextService
from buildlens.domain.eligibility import exclusion_reason
from buildlens.domain.embeddings import ChunkEmbedding, membership_digest
from buildlens.domain.evaluation import CorpusEvidence, EvaluationError, digest, primitive
from buildlens.domain.processing import ArtifactProcessing, Chunk
from buildlens.domain.source import processing_contract
from buildlens.infrastructure.persistence.embeddings import SqlAlchemyEmbeddingStore
from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore
from buildlens.infrastructure.persistence.retrieval import SqlAlchemyContextEvidenceStore


class SqlAlchemyEvaluationCorpus:
    def __init__(self, session_factory: Any) -> None:
        self.processing = SqlAlchemyProcessingStore(session_factory)
        self.embedding = SqlAlchemyEmbeddingStore(session_factory)
        self.authority = SqlAlchemyContextEvidenceStore(session_factory)

    def load(self, project_id: UUID, snapshot_id: UUID) -> CorpusEvidence:
        snapshot = self.processing.get_snapshot(project_id, snapshot_id)
        processing = self.processing.active(project_id, snapshot_id)
        embedding = self.embedding.active(project_id, snapshot_id)
        if snapshot is None or processing is None or embedding is None:
            raise EvaluationError("CORPUS_PREREQUISITES_MISSING")
        if processing.processing_contract != processing_contract():
            raise EvaluationError("PROCESSING_CONTRACT_INCOMPATIBLE")
        artifacts = tuple(self.processing.artifacts(project_id, snapshot_id))
        artifact_by_id = {artifact.id: artifact for artifact in artifacts}
        processing_artifacts: list[ArtifactProcessing] = []
        processing_offset = 0
        while True:
            page, total = self.processing.page_artifacts(
                project_id, snapshot_id, processing.id, 500, processing_offset
            )
            processing_artifacts.extend(page)
            processing_offset += len(page)
            if processing_offset >= total:
                break
            if not page or processing_offset > 100_000:
                raise EvaluationError("CORPUS_INSPECTION_LIMIT")
        for observed in processing_artifacts:
            if observed.outcome not in {"processed", "empty"}:
                continue
            if exclusion_reason(observed.locator, False):
                raise EvaluationError("CORPUS_CONTAMINATED")
            artifact = artifact_by_id.get(observed.artifact_id)
            if (
                artifact is None
                or artifact.content_fingerprint is None
                or artifact.content_fingerprint.digest != observed.input_fingerprint
                or artifact.locator != observed.locator
                or artifact.metadata is None
                or artifact.metadata.inventory_status != "included"
                or artifact.metadata.pruned
                or artifact.metadata.exclusion_reasons
            ):
                raise EvaluationError("PROCESSING_ARTIFACT_INTEGRITY_INVALID")
        count, byte_count = self.processing.content_stats(project_id, snapshot_id, processing.id)
        if count > 100_000 or byte_count > 128 * 1024 * 1024:
            raise EvaluationError("CORPUS_INSPECTION_LIMIT")
        chunks: list[Chunk] = []
        embeddings: list[ChunkEmbedding] = []
        for offset in range(0, count, 500):
            chunks.extend(
                self.processing.page_chunk_content(
                    project_id, snapshot_id, processing.id, 500, offset
                )
            )
        offset = 0
        while True:
            embedding_page, total = self.embedding.page_rows(
                project_id, snapshot_id, embedding.id, 500, offset
            )
            embeddings.extend(embedding_page)
            offset += len(embedding_page)
            if offset >= total:
                break
            if not embedding_page or offset > 100_000:
                raise EvaluationError("CORPUS_INSPECTION_LIMIT")
        if len(chunks) != count:
            raise EvaluationError("CORPUS_MEMBERSHIP_CHANGED")
        # Same shared-lock authority/freshness gates as production. No vector reads.
        with self.authority.session(
            project_id, snapshot_id, processing, embedding, 30_000
        ) as evidence:
            for offset in range(0, len(chunks), 500):
                chunk_page = chunks[offset : offset + 500]
                hydrated = evidence.hydrate([c.id for c in chunk_page])
                if any(hydrated.get(c.id) != c for c in chunk_page):
                    raise EvaluationError("CORPUS_MEMBERSHIP_CHANGED")
                for checked_chunk in chunk_page:
                    ContextService._validate_chunk(checked_chunk)
        by_embedding_chunk = {row.chunk_id: row for row in embeddings}
        if len(by_embedding_chunk) != len(embeddings) or by_embedding_chunk.keys() != {
            c.id for c in chunks
        }:
            raise EvaluationError("EMBEDDING_MEMBERSHIP_INCOMPATIBLE")
        # Production publishes this digest in processing locator/ordinal order;
        # the metadata paging adapter returns stable-key order instead.
        if (
            membership_digest([by_embedding_chunk[c.id] for c in chunks])
            != embedding.membership_digest
        ):
            raise EvaluationError("EMBEDDING_MEMBERSHIP_CHANGED")
        by_id = {c.id: c for c in chunks}
        for row in embeddings:
            chunk = by_id.get(row.chunk_id)
            if (
                chunk is None
                or row.input_hash != chunk.content_hash
                or row.contract_digest != embedding.contract.digest
                or row.processing_run_id != processing.id
                or row.artifact_processing_id != chunk.artifact_processing_id
                or row.stable_chunk_key != chunk.stable_key
            ):
                raise EvaluationError("EMBEDDING_INTEGRITY_INVALID")
        fingerprints = {
            a.id: a.content_fingerprint.digest if a.content_fingerprint else None for a in artifacts
        }
        manifest = {
            "version": "stage11.corpus.v1",
            "project_id": str(project_id),
            "snapshot_id": str(snapshot_id),
            "inventory_policy_version": snapshot.policy_version,
            "eligibility_provenance": primitive(snapshot.eligibility_provenance),
            "content_manifest": primitive(snapshot.content_manifest),
            "processing_id": str(processing.id),
            "processing_revision": processing.revision,
            "processing_contract": processing.processing_contract,
            "processing_artifacts": [
                {
                    "id": str(row.id),
                    "artifact_id": str(row.artifact_id),
                    "locator": row.locator,
                    "outcome": row.outcome,
                    "input_fingerprint": row.input_fingerprint,
                    "processor_contract": primitive(row.processor_contract),
                    "stored_chunk_count": row.stored_chunk_count,
                }
                for row in processing_artifacts
            ],
            "inventory_membership_digest": digest(
                [
                    {
                        "id": str(artifact.id),
                        "locator": artifact.locator,
                        "metadata": primitive(artifact.metadata),
                        "fingerprint": primitive(artifact.content_fingerprint),
                    }
                    for artifact in artifacts
                ]
            ),
            "embedding_observations": [
                {
                    "id": str(row.id),
                    "chunk_id": str(row.chunk_id),
                    "stable_chunk_key": row.stable_chunk_key,
                    "input_hash": row.input_hash,
                    "logical_embedding_key": row.logical_embedding_key,
                    "contract_digest": row.contract_digest,
                    "dimensions": row.dimensions,
                    "window_count": row.window_count,
                    "execution_mode": row.execution_mode,
                    "reuse_source_embedding_id": str(row.reuse_source_embedding_id)
                    if row.reuse_source_embedding_id
                    else None,
                }
                for row in embeddings
            ],
            "embedding_id": str(embedding.id),
            "embedding_revision": embedding.revision,
            "embedding_contract": primitive(embedding.contract),
            "embedding_membership_digest": embedding.membership_digest,
            "searchable_artifacts": sorted(
                [
                    {
                        "artifact_id": str(a.id),
                        "locator": a.locator,
                        "fingerprint": fingerprints[a.id],
                    }
                    for a in artifacts
                    if a.id in {c.artifact_id for c in chunks}
                ],
                key=lambda row: str(row["locator"]),
            ),
            "chunks": sorted(
                [
                    {
                        "chunk_id": str(c.id),
                        "artifact_id": str(c.artifact_id),
                        "artifact_processing_id": str(c.artifact_processing_id),
                        "locator": c.locator,
                        "stable_key": c.stable_key,
                        "kind": c.kind,
                        "language": next(
                            a.metadata.language
                            for a in artifacts
                            if a.id == c.artifact_id and a.metadata
                        ),
                        "content_hash": c.content_hash,
                        "symbol_id": str(c.symbol_id) if c.symbol_id else None,
                        "symbol_identity_id": str(c.symbol_identity_id)
                        if c.symbol_identity_id
                        else None,
                        "span": primitive(c.span),
                    }
                    for c in chunks
                ],
                key=lambda row: row["chunk_id"],
            ),
        }
        return CorpusEvidence(
            snapshot, artifacts, processing, embedding, tuple(chunks), tuple(embeddings), manifest
        )

    def verify(self, corpus: CorpusEvidence) -> None:
        # Evaluator pre/post checks may read authority; observation never does.
        current = self.load(corpus.embedding.project_id, corpus.snapshot.id)
        if digest(current.manifest) != digest(corpus.manifest):
            raise EvaluationError("RUN_AUTHORITY_CHANGED")
