"""Versioned JSON definitions and atomic local artifacts inside the corpus boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from buildlens.application.evaluation_binding import validate_definitions
from buildlens.domain.evaluation import (
    ARTIFACT_VERSION,
    RUBRIC_VERSION,
    ChunkSelector,
    EvaluationCase,
    EvaluationDataset,
    EvaluationError,
    EvidenceRequirement,
    EvidenceTarget,
    RelevanceJudgment,
    canonical,
)
from buildlens.domain.retrieval import RetrievalFilters


def boundary_path(path: Path, root: Path, area: str) -> Path:
    root = root.resolve(strict=True)
    boundary = root / ".buildlens-eval" / area
    if boundary.resolve() != boundary:
        raise EvaluationError("BOUNDARY_SYMLINK_REJECTED")
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(boundary) or resolved == boundary:
        raise EvaluationError("PATH_OUTSIDE_EVALUATION_BOUNDARY")
    return resolved


def run_root(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    for parent in absolute.parents:
        if parent.name == ".buildlens-eval":
            boundary_path(absolute, parent.parent, "runs")
            return parent.parent
    raise EvaluationError("PATH_OUTSIDE_EVALUATION_BOUNDARY")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise EvaluationError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise EvaluationError("ARTIFACT_INPUT_INVALID")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(EvaluationError("NONFINITE_JSON")),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvaluationError("ARTIFACT_JSON_INVALID") from error


def dataset_from(value: dict[str, Any]) -> EvaluationDataset:
    try:
        cases = []
        for raw in value["cases"]:
            raw = dict(raw)
            raw["filters"] = RetrievalFilters(**raw["filters"])
            raw["targets"] = tuple(EvidenceTarget(**t) for t in raw["targets"])
            raw["requirements"] = tuple(
                EvidenceRequirement(r["id"], r["fact"], tuple(tuple(a) for a in r["alternatives"]))
                for r in raw["requirements"]
            )
            raw["judgments"] = tuple(
                RelevanceJudgment(
                    ChunkSelector(**j["selector"]),
                    j["relevant"],
                    j["reviewer"],
                    j["rationale"],
                    j["reviewed_at"],
                )
                for j in raw["judgments"]
            )
            raw["required_facets"] = tuple(raw["required_facets"])
            raw["forbidden_claims"] = tuple(raw["forbidden_claims"])
            if type(raw["answerable"]) is not bool:
                raise EvaluationError("ANSWERABILITY_INVALID")
            cases.append(EvaluationCase(**raw))
        fields = dict(value)
        fields["cases"] = tuple(cases)
        dataset = EvaluationDataset(**fields)
        validate_definitions(dataset)
        return dataset
    except (KeyError, TypeError, AttributeError) as error:
        raise EvaluationError("DEFINITION_SCHEMA_INVALID") from error


def load_dataset(path: Path) -> EvaluationDataset:
    return dataset_from(read_json(path))


def atomic_write(path: Path, value: Any, *, replace: bool = False, text: bool = False) -> None:
    """Exclusive publication by hard link, or atomic replacement of derived/status files."""
    if path.is_symlink() or path.parent.is_symlink():
        raise EvaluationError("ARTIFACT_SYMLINK_REJECTED")
    payload = value.encode("utf-8") if text else canonical(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # Existing records are never overwritten.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class LocalRunStore:
    def __init__(self, destination: Path) -> None:
        self.destination = destination

    def create(
        self, manifest: dict[str, Any], dataset: EvaluationDataset, corpus: dict[str, Any]
    ) -> None:
        self.destination.mkdir(parents=True, exist_ok=False)
        self.write("manifest.json", {"artifact_version": ARTIFACT_VERSION, **manifest})
        self.write("dataset.json", dataset)
        self.write("corpus.json", corpus)

    def write(self, name: str, value: Any, *, replace: bool = False) -> None:
        target = self.destination / name
        if not target.resolve().is_relative_to(self.destination.resolve()):
            raise EvaluationError("ARTIFACT_PATH_INVALID")
        atomic_write(target, value, replace=replace)

    def finish(self, manifest: dict[str, Any]) -> None:
        self.write(
            "manifest.json", {"artifact_version": ARTIFACT_VERSION, **manifest}, replace=True
        )


def validate_review(review: dict[str, Any], result: dict[str, Any], case: EvaluationCase) -> None:
    if (
        review.get("result_digest") != result["result_digest"]
        or review.get("rubric_version") != RUBRIC_VERSION
        or not all(
            isinstance(review.get(k), str) and review[k].strip()
            for k in ("reviewer", "reviewed_at", "rationale")
        )
    ):
        raise EvaluationError("ANSWER_REVIEW_PROVENANCE_INVALID")
    answer = result["observations"].get("answer")
    if answer is None:
        raise EvaluationError("ANSWER_REVIEW_HAS_NO_ANSWER")
    allowed = {"supported", "partially_supported", "unsupported", "contradicted"}
    statement_ids = set()
    for judgment in review.get("statements", []):
        index = judgment.get("index")
        if (
            type(index) is not int
            or index in statement_ids
            or not 0 <= index < len(answer["statements"])
            or judgment.get("judgment") not in allowed
            or type(judgment.get("substantive")) is not bool
            or type(judgment.get("sufficient_cited_support")) is not bool
            or judgment.get("support_kind") not in {"appropriate", "inappropriate"}
            or not judgment.get("rationale")
        ):
            raise EvaluationError("STATEMENT_REVIEW_INVALID")
        statement_ids.add(index)
    link_ids = set()
    for judgment in review.get("links", []):
        index, evidence = judgment.get("statement_index"), judgment.get("evidence_id")
        if (
            type(index) is not int
            or not 0 <= index < len(answer["statements"])
            or evidence not in answer["statements"][index]["evidence_ids"]
            or (index, evidence) in link_ids
            or judgment.get("judgment") not in allowed
            or not judgment.get("rationale")
        ):
            raise EvaluationError("CITATION_LINK_REVIEW_INVALID")
        link_ids.add((index, evidence))
    facet_ids = set()
    for facet in review.get("facets", []):
        index = facet.get("index")
        if (
            type(index) is not int
            or not 0 <= index < len(case.required_facets)
            or index in facet_ids
            or facet.get("judgment") not in {"met", "partial", "missing", "incorrect"}
            or not facet.get("rationale")
        ):
            raise EvaluationError("FACET_REVIEW_INVALID")
        facet_ids.add(index)
    if review.get("correctness") not in {"correct", "partial", "incorrect"} or review.get(
        "abstention"
    ) not in {"appropriate", "inappropriate", "not_applicable"}:
        raise EvaluationError("ANSWER_REVIEW_INVALID")
