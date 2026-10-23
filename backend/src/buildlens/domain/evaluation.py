"""Portable Stage 11 definitions and identities; no database or HTTP dependency."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from buildlens.domain.context import RankingPolicy
from buildlens.domain.embeddings import ChunkEmbedding, EmbeddingRun
from buildlens.domain.entities import Artifact, ProjectVersion
from buildlens.domain.processing import Chunk, ProcessingRun
from buildlens.domain.retrieval import RetrievalFilters, RetrievalMode

SCHEMA_VERSION = "stage11.dataset.v1"
METRIC_VERSION = "stage11.metrics.v1"
ARTIFACT_VERSION = "stage11.artifacts.v1"
RUBRIC_VERSION = "stage11.human-review.v1"
CATEGORIES = ("identifier", "semantic", "cross_file", "architecture", "unsupported")
VARIANTS: dict[str, tuple[RetrievalMode, RankingPolicy]] = {
    "vector": ("semantic", "retrieval_order"),
    "hybrid": ("hybrid", "retrieval_order"),
    "hybrid_reranked": ("hybrid", "local_cross_encoder"),
}
CUTOFFS = (1, 5, 10, 32)


def primitive(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return primitive(asdict(value))
    if isinstance(value, dict):
        return {str(k): primitive(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [primitive(v) for v in value]
    if isinstance(value, (UUID, datetime)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def canonical(value: Any) -> bytes:
    return json.dumps(
        primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def excerpt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EvaluationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EvidenceTarget:
    id: str
    locator: str
    artifact_fingerprint: str
    start_offset: int
    end_offset: int
    excerpt_hash: str
    label: str | None = None


@dataclass(frozen=True)
class EvidenceRequirement:
    id: str
    fact: str
    alternatives: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class ChunkSelector:
    locator: str
    artifact_fingerprint: str
    stable_key: str
    content_hash: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class RelevanceJudgment:
    selector: ChunkSelector
    relevant: bool
    reviewer: str
    rationale: str
    reviewed_at: str


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    question: str
    category: str
    filters: RetrievalFilters
    answerable: bool
    required_facets: tuple[str, ...]
    forbidden_claims: tuple[str, ...]
    targets: tuple[EvidenceTarget, ...]
    requirements: tuple[EvidenceRequirement, ...]
    judgments: tuple[RelevanceJudgment, ...]


@dataclass(frozen=True)
class EvaluationDataset:
    schema_version: str
    id: str
    version: str
    corpus_digest: str | None
    corpus_manifest: dict[str, Any] | None
    review_status: str
    approval: dict[str, str] | None
    rubric_version: str
    cases: tuple[EvaluationCase, ...]

    @property
    def judgments_digest(self) -> str:
        return digest([[case.id, case.judgments] for case in self.cases])


@dataclass(frozen=True)
class CorpusEvidence:
    snapshot: ProjectVersion
    artifacts: tuple[Artifact, ...]
    processing: ProcessingRun
    embedding: EmbeddingRun
    chunks: tuple[Chunk, ...]
    embeddings: tuple[ChunkEmbedding, ...]
    manifest: dict[str, Any]


@dataclass(frozen=True)
class CaseBinding:
    case_id: str
    targets: dict[str, EvidenceTarget]
    judged: dict[str, bool]
    chunk_ids: tuple[UUID, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnswerReview:
    result_digest: str
    reviewer: str
    rubric_version: str
    reviewed_at: str
    facets: tuple[dict[str, Any], ...]
    statements: tuple[dict[str, Any], ...]
    links: tuple[dict[str, Any], ...]
    correctness: str
    abstention: str
    rationale: str


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    variant: str
    category: str
    outcome: str
    result_digest: str
    observations: dict[str, Any]
    metrics: dict[str, Any]


@dataclass(frozen=True)
class EvaluationRun:
    id: str
    dataset_digest: str
    corpus_digest: str
    manifest: dict[str, Any]
