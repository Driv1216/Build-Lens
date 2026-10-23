"""Exact frozen-corpus binding. No source relocation or relevance inference."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, cast
from uuid import UUID

from buildlens.domain.eligibility import POLICY_VERSION, exclusion_reason
from buildlens.domain.evaluation import (
    CATEGORIES,
    RUBRIC_VERSION,
    SCHEMA_VERSION,
    CaseBinding,
    CorpusEvidence,
    EvaluationCase,
    EvaluationDataset,
    EvaluationError,
    EvidenceTarget,
    digest,
    excerpt_hash,
    primitive,
)
from buildlens.domain.processing import Chunk


def definition_digest(dataset: EvaluationDataset) -> str:
    value = primitive(dataset)
    value.pop("approval")
    value.pop("review_status")
    return digest(value)


def validate_definitions(dataset: EvaluationDataset, *, benchmark: bool = True) -> None:
    if dataset.schema_version != SCHEMA_VERSION or dataset.rubric_version != RUBRIC_VERSION:
        raise EvaluationError("DEFINITION_VERSION_INCOMPATIBLE")
    ids = [case.id for case in dataset.cases]
    if len(ids) != len(set(ids)) or not ids:
        raise EvaluationError("CASE_MEMBERSHIP_INVALID")
    if benchmark and (
        len(ids) != 30
        or any(
            sum(case.category == category for case in dataset.cases) != 6 for category in CATEGORIES
        )
    ):
        raise EvaluationError("BENCHMARK_REQUIRES_SIX_PER_CATEGORY")
    for name in (dataset.id, dataset.version, *ids):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", name):
            raise EvaluationError("DEFINITION_ID_INVALID")
    if dataset.review_status not in {"draft", "approved"}:
        raise EvaluationError("REVIEW_STATUS_INVALID")
    if dataset.review_status == "approved":
        approval = dataset.approval or {}
        if (
            not all(approval.get(k) for k in ("reviewer", "reviewed_at", "rationale"))
            or approval.get("definition_digest") != definition_digest(dataset)
            or approval.get("judgments_digest") != dataset.judgments_digest
        ):
            raise EvaluationError("HUMAN_APPROVAL_INVALID")
    elif dataset.approval is not None:
        raise EvaluationError("DRAFT_CANNOT_CLAIM_APPROVAL")
    for case in dataset.cases:
        if (
            case.category not in CATEGORIES
            or not case.question.strip()
            or len(case.question.encode()) > 4096
            or case.answerable != (case.category != "unsupported")
        ):
            raise EvaluationError("CASE_DEFINITION_INVALID")
        targets = {target.id: target for target in case.targets}
        if len(targets) != len(case.targets):
            raise EvaluationError("DUPLICATE_TARGET")
        for target in case.targets:
            path = PurePosixPath(target.locator)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or exclusion_reason(target.locator, False)
                or type(target.start_offset) is not int
                or type(target.end_offset) is not int
                or not 0 <= target.start_offset < target.end_offset
                or target.end_offset - target.start_offset > 2 * 1024 * 1024
                or not re.fullmatch(r"[0-9a-f]{64}", target.artifact_fingerprint)
                or not re.fullmatch(r"[0-9a-f]{64}", target.excerpt_hash)
            ):
                raise EvaluationError("TARGET_INVALID")
        if case.answerable and (not case.requirements or not case.required_facets):
            raise EvaluationError("ANSWERABLE_REQUIREMENTS_MISSING")
        group_ids = [r.id for r in case.requirements]
        if len(group_ids) != len(set(group_ids)):
            raise EvaluationError("DUPLICATE_REQUIREMENT")
        for requirement in case.requirements:
            if (
                not requirement.fact
                or not requirement.alternatives
                or any(
                    not alt or len(alt) != len(set(alt)) or not set(alt) <= targets.keys()
                    for alt in requirement.alternatives
                )
            ):
                raise EvaluationError("REQUIREMENT_INVALID")
        if (
            dataset.review_status == "approved"
            and case.answerable
            and not any(j.relevant for j in case.judgments)
        ):
            raise EvaluationError("APPROVED_RELEVANCE_MISSING")
        selectors = [digest(j.selector) for j in case.judgments]
        if len(selectors) != len(set(selectors)):
            raise EvaluationError("DUPLICATE_JUDGMENT")
        if any(
            type(j.relevant) is not bool
            or not all((j.reviewer.strip(), j.rationale.strip(), j.reviewed_at.strip()))
            for j in case.judgments
        ):
            raise EvaluationError("JUDGMENT_PROVENANCE_MISSING")


def check_corpus(dataset: EvaluationDataset, corpus: CorpusEvidence) -> None:
    snapshot = corpus.snapshot
    if (
        snapshot.policy_version != POLICY_VERSION
        or snapshot.legacy_unverified
        or snapshot.scan_status != "complete"
        or snapshot.content_manifest is None
        or snapshot.canonical_root is None
        or (snapshot.eligibility_provenance or {}).get("policy_version") != POLICY_VERSION
    ):
        raise EvaluationError("EXCLUSION_PROVENANCE_INCOMPATIBLE")
    for artifact in corpus.artifacts:
        if ".buildlens-eval" in (part.lower() for part in PurePosixPath(artifact.locator).parts):
            if (
                artifact.metadata is None
                or artifact.metadata.inventory_status != "excluded"
                or "evaluation_data" not in artifact.metadata.exclusion_reasons
            ):
                raise EvaluationError("CORPUS_CONTAMINATED")
    if any(exclusion_reason(chunk.locator or "", False) for chunk in corpus.chunks):
        raise EvaluationError("CORPUS_CONTAMINATED")
    ids = {chunk.id for chunk in corpus.chunks}
    if len(ids) != len(corpus.chunks):
        raise EvaluationError("DUPLICATE_CORPUS_OBSERVATION")
    embedded = {row.chunk_id for row in corpus.embeddings}
    if len(embedded) != len(corpus.embeddings) or embedded != ids:
        raise EvaluationError("EMBEDDING_MEMBERSHIP_INCOMPATIBLE")
    if dataset.corpus_manifest is None or dataset.corpus_digest is None:
        raise EvaluationError("CORPUS_NOT_PINNED")
    if (
        digest(dataset.corpus_manifest) != dataset.corpus_digest
        or digest(corpus.manifest) != dataset.corpus_digest
    ):
        raise EvaluationError("CORPUS_MANIFEST_INCOMPATIBLE")


def eligible(case: EvaluationCase, chunk: Chunk, corpus: CorpusEvidence) -> bool:
    filters = case.filters
    artifact = next((a for a in corpus.artifacts if a.id == chunk.artifact_id), None)
    return bool(
        artifact
        and artifact.metadata
        and (
            filters.locator_prefix is None
            or (chunk.locator or "").startswith(filters.locator_prefix)
        )
        and (filters.language is None or artifact.metadata.language == filters.language)
        and (filters.kind is None or chunk.kind == filters.kind)
    )


def target_excerpt(target: EvidenceTarget, chunks: list[Chunk]) -> str | None:
    """Gap-free union of exact source slices; inconsistent overlap is invalid."""
    chars: dict[int, str] = {}
    for chunk in chunks:
        start = max(chunk.span.start_offset, target.start_offset)
        end = min(chunk.span.end_offset, target.end_offset)
        for offset in range(start, end):
            char = chunk.content[offset - chunk.span.start_offset]
            if offset in chars and chars[offset] != char:
                raise EvaluationError("TARGET_OVERLAP_INCONSISTENT")
            chars[offset] = char
    if len(chars) != target.end_offset - target.start_offset:
        return None
    return "".join(chars[offset] for offset in range(target.start_offset, target.end_offset))


def bind_case(case: EvaluationCase, corpus: CorpusEvidence) -> CaseBinding:
    artifacts = {a.locator: a for a in corpus.artifacts}
    if len(artifacts) != len(corpus.artifacts):
        raise EvaluationError("ARTIFACT_LOCATOR_AMBIGUOUS")
    targets = {}
    bound_ids: set[UUID] = set()
    judged: dict[str, bool] = {}
    for target in case.targets:
        artifact = artifacts.get(target.locator)
        if (
            artifact is None
            or artifact.content_fingerprint is None
            or artifact.content_fingerprint.digest != target.artifact_fingerprint
        ):
            raise EvaluationError("TARGET_MISSING_OR_CHANGED")
        chunks = [
            c for c in corpus.chunks if c.artifact_id == artifact.id and eligible(case, c, corpus)
        ]
        excerpt = target_excerpt(target, chunks)
        if excerpt is None or excerpt_hash(excerpt) != target.excerpt_hash:
            raise EvaluationError("TARGET_MISSING_CHANGED_OR_FILTERED")
        targets[target.id] = target
        bound_ids.update(
            c.id
            for c in chunks
            if c.span.start_offset < target.end_offset and target.start_offset < c.span.end_offset
        )
    for judgment in case.judgments:
        s = judgment.selector
        artifact = artifacts.get(s.locator)
        if (
            artifact is None
            or artifact.content_fingerprint is None
            or artifact.content_fingerprint.digest != s.artifact_fingerprint
        ):
            raise EvaluationError("JUDGMENT_TARGET_CHANGED")
        matches = [
            c
            for c in corpus.chunks
            if c.artifact_id == artifact.id
            and c.stable_key == s.stable_key
            and c.content_hash == s.content_hash
            and c.span.start_offset == s.start_offset
            and c.span.end_offset == s.end_offset
            and eligible(case, c, corpus)
        ]
        if len(matches) != 1:
            raise EvaluationError("JUDGMENT_TARGET_MISSING_AMBIGUOUS_OR_FILTERED")
        judged[str(matches[0].id)] = judgment.relevant
    return CaseBinding(case.id, targets, judged, tuple(sorted(bound_ids, key=str)))


def chunk_metadata(chunk: Chunk, corpus: CorpusEvidence) -> dict[str, Any]:
    artifact = next(a for a in corpus.artifacts if a.id == chunk.artifact_id)
    result = cast(dict[str, Any], primitive(chunk))
    result.pop("content")
    result["chunk_id"] = result.pop("id")
    if artifact.content_fingerprint is None:
        raise EvaluationError("ARTIFACT_FINGERPRINT_MISSING")
    result["artifact_fingerprint"] = artifact.content_fingerprint.digest
    return result
