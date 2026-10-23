"""Deterministic offline rescoring, review packets, and paired comparisons."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from buildlens.application.evaluation_binding import definition_digest, validate_definitions
from buildlens.application.evaluation_metrics import score_result, semantic_metrics
from buildlens.domain.evaluation import (
    ARTIFACT_VERSION,
    CUTOFFS,
    METRIC_VERSION,
    CaseBinding,
    EvaluationDataset,
    EvaluationError,
    digest,
    primitive,
)
from buildlens.infrastructure.evaluation.local import (
    atomic_write,
    boundary_path,
    load_dataset,
    read_json,
    run_root,
    validate_review,
)


def load_run(path: Path) -> tuple[dict[str, Any], EvaluationDataset, list[dict[str, Any]]]:
    root = run_root(path)
    path = boundary_path(path, root, "runs")
    manifest = read_json(path / "manifest.json")
    dataset = load_dataset(path / "dataset.json")
    corpus = read_json(path / "corpus.json")
    if (
        manifest["artifact_version"] != ARTIFACT_VERSION
        or manifest["metric_version"] != METRIC_VERSION
        or digest(dataset) != manifest["dataset_digest"]
        or digest(corpus) != manifest["corpus_digest"]
    ):
        raise EvaluationError("RUN_MANIFEST_INTEGRITY_INVALID")
    records = []
    keys = set()
    files = sorted((path / "cases").glob("*.json"))
    for file in files:
        boundary_path(file, root, "runs")
        record = read_json(file)
        payload = {k: v for k, v in record.items() if k not in {"result_digest", "metrics"}}
        if (
            digest(payload) != record["result_digest"]
            or record["artifact_version"] != ARTIFACT_VERSION
        ):
            raise EvaluationError("CASE_RESULT_INTEGRITY_INVALID")
        key = (record["case_id"], record["variant"])
        if (
            key in keys
            or key[0] not in manifest["case_selection"]
            or key[1] not in manifest["variants"]
        ):
            raise EvaluationError("RUN_CASE_MEMBERSHIP_INVALID")
        keys.add(key)
        records.append(record)
    return manifest, dataset, records


def _binding(raw: dict[str, Any], case: Any) -> CaseBinding:
    return CaseBinding(
        case.id,
        {t.id: t for t in case.targets},
        raw["judged"],
        tuple(UUID(key) for key in raw["chunk_ids"]),
    )


def _revised_binding(original: CaseBinding, case: Any, corpus: dict[str, Any]) -> CaseBinding:
    artifacts = {a["locator"]: a for a in corpus["searchable_artifacts"]}
    judged = {}
    for judgment in case.judgments:
        selector = judgment.selector
        artifact = artifacts.get(selector.locator)
        matches = [
            row
            for row in corpus["chunks"]
            if row["locator"] == selector.locator
            and row["stable_key"] == selector.stable_key
            and row["content_hash"] == selector.content_hash
            and row["span"]["start_offset"] == selector.start_offset
            and row["span"]["end_offset"] == selector.end_offset
            and (
                case.filters.locator_prefix is None
                or row["locator"].startswith(case.filters.locator_prefix)
            )
            and (case.filters.kind is None or row.get("kind") == case.filters.kind)
            and (case.filters.language is None or row.get("language") == case.filters.language)
        ]
        if (
            not artifact
            or artifact["fingerprint"] != selector.artifact_fingerprint
            or len(matches) != 1
        ):
            raise EvaluationError("REVISED_JUDGMENT_BINDING_INVALID")
        judged[matches[0]["chunk_id"]] = judgment.relevant
    return CaseBinding(original.case_id, original.targets, judged, original.chunk_ids)


def revision_compatible(original: EvaluationDataset, revised: EvaluationDataset) -> None:
    validate_definitions(revised)
    if (
        original.id != revised.id
        or original.corpus_digest != revised.corpus_digest
        or original.rubric_version != revised.rubric_version
    ):
        raise EvaluationError("RESCORING_DATASET_INCOMPATIBLE")
    left, right = primitive(original.cases), primitive(revised.cases)
    for cases in (left, right):
        for case in cases:
            case.pop("judgments")
    if left != right:
        raise EvaluationError("RESCORING_CASE_DEFINITIONS_CHANGED")


def metric_values(metrics: dict[str, Any]) -> dict[str, float]:
    result = {}
    ranking = metrics.get("ranking")
    if ranking is not None:
        if ranking["rr@32"] is not None:
            result["MRR@32"] = ranking["rr@32"]
        for k in CUTOFFS:
            values = ranking[str(k)]
            for metric in ("recall", "precision", "judgment_coverage"):
                if values[metric] is not None:
                    result[f"{metric}@{k}"] = values[metric]
            for metric in ("coverage", "complete"):
                value = values["required_evidence"][metric]
                if value is not None:
                    result[f"evidence_{metric}@{k}"] = float(value)
    for stage in ("context", "generation_evidence", "cited_evidence"):
        values = metrics.get(stage)
        if values and values["coverage"] is not None:
            result[f"{stage}_coverage"] = values["coverage"]
    structural = metrics.get("structural_answer")
    if structural:
        result["answer_status_match"] = float(structural["status_matches"])
    return result


def _cell(values: list[float]) -> str:
    return (
        f"{sum(values) / len(values):.4f} (n={len(values)})" if values else "withheld / N/A (n=0)"
    )


def render(
    manifest: dict[str, Any], dataset: EvaluationDataset, records: list[dict[str, Any]]
) -> str:
    lines = [
        "# Native RAG evaluation",
        "",
        f"Run: `{manifest['id']}`. Status: **{manifest['status']}**.",
        f"Dataset: `{dataset.id}` / `{dataset.version}`. "
        f"Ground truth: **{dataset.review_status}**.",
        "",
        "Recall measures recovery of curated relevant chunks. "
        "Structural citation checks do not establish semantic support.",
        "Expanded neighbours count only toward delivered evidence. "
        "Reranker admission: **32 candidates**; seed limit: **8**.",
        "",
        "| Verification dimension | State |",
        "| --- | --- |",
    ]
    for dimension, state in manifest["verification_states"].items():
        lines.append(f"| {dimension} | {state} |")
    counts = Counter(r["outcome"] for r in records)
    unjudged = sum(bool(r["metrics"].get("provisional")) for r in records)
    unavailable = sum(
        r["metrics"].get("ranking") is None for r in records if r["outcome"] == "success"
    )
    lines += [
        "",
        f"Retained results: {len(records)}; invalid: {counts['invalid']}; "
        f"errors: {counts['error']}; unjudged/provisional rankings: {unjudged}; "
        f"unavailable successful rankings: {unavailable}.",
        "",
        "Human semantic review is recorded independently; missing reviews remain unreviewed.",
    ]
    if not manifest["comparison_valid"]:
        lines += [
            "",
            "**Authority changed: aggregate scores and paired comparisons are withheld.**",
        ]
    else:
        lines += [
            "",
            "## Category macro averages",
            "",
            "| Variant | Category | Metric | Mean and eligible denominator |",
            "| --- | --- | --- | --- |",
        ]
        category_means: dict[tuple[str, str], list[float]] = {}
        for variant in manifest["variants"]:
            for category in sorted({c.category for c in dataset.cases}):
                selected = [
                    r for r in records if r["variant"] == variant and r["category"] == category
                ]
                names = sorted(
                    {
                        "MRR@32",
                        "context_coverage",
                        "generation_evidence_coverage",
                        "cited_evidence_coverage",
                        *(
                            f"{name}@{k}"
                            for k in CUTOFFS
                            for name in (
                                "recall",
                                "precision",
                                "judgment_coverage",
                                "evidence_coverage",
                                "evidence_complete",
                            )
                        ),
                    }
                )
                for name in names:
                    values = [
                        v[name] for r in selected if name in (v := metric_values(r["metrics"]))
                    ]
                    lines.append(f"| {variant} | {category} | {name} | {_cell(values)} |")
                    if values:
                        category_means.setdefault((variant, name), []).append(
                            sum(values) / len(values)
                        )
        for (variant, name), values in sorted(category_means.items()):
            lines.append(f"| {variant} | category macro | {name} | {_cell(values)} categories |")
    lines += [
        "",
        "## Per-case results",
        "",
        "| Case | Variant | Outcome | MRR@32 | Recall@5 | "
        "Missing evidence groups@5 | Human review |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        ranking = record["metrics"].get("ranking") or {}
        at5 = ranking.get("5") or {}
        missing = (at5.get("required_evidence") or {}).get("missing", [])
        lines.append(
            f"| {record['case_id']} | {record['variant']} | {record['outcome']} "
            f"{record.get('error_code') or ''} | {ranking.get('rr@32', 'withheld')} | "
            f"{at5.get('recall', 'withheld')} | {', '.join(missing)} | "
            f"{'reviewed (see coverage)' if 'semantic' in record['metrics'] else 'unreviewed'} |"
        )
    return "\n".join(lines) + "\n"


def report(
    path: Path,
    *,
    revised: EvaluationDataset | None = None,
    reviews: Path | None = None,
    verification: Path | None = None,
) -> dict[str, Any]:
    manifest, dataset, records = load_run(path)
    root = run_root(path)
    if revised is not None:
        revision_compatible(dataset, revised)
        dataset = revised
    corpus = read_json(path / "corpus.json")
    verification_record = None
    if verification is not None:
        verification = boundary_path(verification, root, "runs")
        evidence = read_json(verification)
        if (
            evidence.get("version") != "stage11.verification.v1"
            or evidence.get("infrastructure_status") not in {"passed", "partial", "failed"}
            or evidence.get("record_digest")
            != digest({k: v for k, v in evidence.items() if k != "record_digest"})
        ):
            raise EvaluationError("VERIFICATION_RECORD_INVALID")
        verification_record = {
            "path": str(verification.relative_to(root)),
            "digest": evidence["record_digest"],
            "status": evidence["infrastructure_status"],
        }
    cases = {c.id: c for c in dataset.cases}
    review_count = 0
    for record in records:
        case = cases[record["case_id"]]
        if record["binding"]:
            binding = _revised_binding(_binding(record["binding"], case), case, corpus)
            record["metrics"] = score_result(
                case, binding, record["observations"], record["outcome"]
            )
        record["metrics"]["provisional"] = dataset.review_status != "approved" or bool(
            (record["metrics"].get("ranking") or {}).get("provisional")
        )
        review_path = (reviews / f"{case.id}.{record['variant']}.json") if reviews else None
        if review_path is not None and review_path.exists():
            review_path = boundary_path(review_path, root, "reviews")
            review = read_json(review_path)
            validate_review(review, record, case)
            record["metrics"]["semantic"] = semantic_metrics(
                review, record["observations"]["answer"]
            )
            record["human_review"] = review
            review_count += 1
        observations = record["observations"]
        packet = {
            "question": case.question,
            "expected_facets": case.required_facets,
            "forbidden_claims": case.forbidden_claims,
            "requirements": primitive(case.requirements),
            "answer": observations.get("answer"),
            "validated_answer_before_final_verification": observations.get(
                "validated_answer_before_final_verification"
            ),
            "outcome": record["outcome"],
            "error_code": record["error_code"],
            "final_supplied_evidence": observations.get("generation_evidence"),
            "cited_evidence": observations.get("cited_evidence"),
            "excerpts": observations.get("excerpts", {}),
            "expected_excerpts": record.get("expected_excerpts", {}),
            "result_digest": record["result_digest"],
            "rubric_version": dataset.rubric_version,
        }
        atomic_write(
            path / "review-packets" / f"{case.id}.{record['variant']}.json", packet, replace=True
        )
    scoring = {
        "metric_version": METRIC_VERSION,
        "dataset_digest": digest(dataset),
        "definition_digest": definition_digest(dataset),
        "judgments_digest": dataset.judgments_digest,
        "rubric_version": dataset.rubric_version,
        "dataset_version": dataset.version,
        "score_record_version": "stage11.score-reference.v1",
        # Native evidence lives once in immutable case artifacts. Duplicating all
        # excerpts here makes real-corpus scores exceed the bounded JSON reader.
        "records": [
            {
                key: record[key]
                for key in (
                    "case_id",
                    "variant",
                    "category",
                    "outcome",
                    "error_code",
                    "duration_seconds",
                    "result_digest",
                    "metrics",
                    "human_review",
                )
                if key in record
            }
            for record in records
        ],
        "review_count": review_count,
        "scoring_dataset": primitive(dataset),
        "verification_record": verification_record,
    }
    scoring["scores_digest"] = digest(scoring)
    display = {
        **manifest,
        "verification_states": {
            **manifest["verification_states"],
            "human_ground_truth": "approved"
            if dataset.review_status == "approved"
            else "pending human approval",
            "semantic_review": f"partial: {review_count} result reviews"
            if review_count
            else "unreviewed",
        },
    }
    if verification_record is not None:
        display["verification_states"]["infrastructure"] = (
            f"Recorded {verification_record['status']} (independent checks; "
            f"{verification_record['path']}; digest {verification_record['digest']})"
        )
    atomic_write(path / "scores.json", scoring, replace=True)
    atomic_write(path / "summary.md", render(display, dataset, records), replace=True, text=True)
    return scoring


def _load_scores(path: Path) -> dict[str, Any]:
    scores = cast(dict[str, Any], read_json(path / "scores.json"))
    if digest({k: v for k, v in scores.items() if k != "scores_digest"}) != scores.get(
        "scores_digest"
    ):
        raise EvaluationError("SCORES_INTEGRITY_INVALID")
    _, original, records = load_run(path)
    from buildlens.infrastructure.evaluation.local import dataset_from

    dataset = dataset_from(scores["scoring_dataset"])
    revision_compatible(original, dataset)
    if (
        digest(dataset) != scores["dataset_digest"]
        or dataset.judgments_digest != scores["judgments_digest"]
    ):
        raise EvaluationError("SCORES_DATASET_INTEGRITY_INVALID")
    retained = {(r["case_id"], r["variant"]): r["result_digest"] for r in records}
    scored = {(r["case_id"], r["variant"]): r["result_digest"] for r in scores["records"]}
    if scored != retained or len(scored) != len(scores["records"]):
        raise EvaluationError("SCORES_RESULT_MEMBERSHIP_INVALID")
    return scores


def compare(left: Path, right: Path) -> str:
    lm, _, _ = load_run(left)
    rm, _, _ = load_run(right)
    ls, rs = _load_scores(left), _load_scores(right)
    if not lm["comparison_valid"] or not rm["comparison_valid"]:
        raise EvaluationError("COMPARISON_AUTHORITY_INVALID")
    for key in ("corpus_digest", "case_selection", "filters"):
        if lm[key] != rm[key]:
            raise EvaluationError("COMPARISON_NOT_PAIRED")
    for key in ("metric_version", "dataset_digest", "judgments_digest", "rubric_version"):
        if ls[key] != rs[key]:
            raise EvaluationError("COMPARISON_SCORING_INCOMPATIBLE_RESCORE_BOTH")
    if ls["metric_version"] != METRIC_VERSION:
        raise EvaluationError("COMPARISON_METRIC_VERSION_INCOMPATIBLE")
    lines = [
        "# Paired native RAG comparison",
        "",
        f"Left: `{lm['id']}`; right: `{rm['id']}`.",
        "Differences are right minus left. "
        "No aggregate RAG quality score or required improvement threshold.",
        "",
        "## Recorded system/configuration differences",
        "",
    ]
    for key in ("answers", "variants", "retrieval_policy", "implementation", "execution_order"):
        if lm.get(key) != rm.get(key):
            import json

            if key == "implementation":
                for label, value in (("left", lm[key]), ("right", rm[key])):
                    lines.append(
                        f"- {label} evaluator source: `{value.get('source_digest', 'unknown')}`; "
                        f"lockfile: `{value.get('lockfile_digest', 'unknown')}`."
                    )
                    lines.append(
                        f"- {label} production configuration: "
                        f"`{json.dumps(value.get('configuration', {}), sort_keys=True)}`."
                    )
            else:
                lines.append(
                    f"- {key}: left `{json.dumps(lm.get(key), sort_keys=True)}`; "
                    f"right `{json.dumps(rm.get(key), sort_keys=True)}`."
                )
    for label, scores in (("left", ls), ("right", rs)):
        lines.append(
            f"- {label} outcomes: `{dict(Counter(r['outcome'] for r in scores['records']))}`"
        )
    lines += [
        "",
        "| Variant | Category | Metric | Paired difference and denominator |",
        "| --- | --- | --- | --- |",
    ]
    right_records = {(r["case_id"], r["variant"]): r for r in rs["records"]}
    pairs: dict[tuple[str, str, str], list[float]] = {}
    for left_record in ls["records"]:
        r = right_records.get((left_record["case_id"], left_record["variant"]))
        if r is None or left_record["outcome"] != "success" or r["outcome"] != "success":
            continue
        lv, rv = metric_values(left_record["metrics"]), metric_values(r["metrics"])
        for name in lv.keys() & rv.keys():
            pairs.setdefault((left_record["variant"], left_record["category"], name), []).append(
                rv[name] - lv[name]
            )
    for (variant, category, metric), values in sorted(pairs.items()):
        lines.append(f"| {variant} | {category} | {metric} | {_cell(values)} |")
    return "\n".join(lines) + "\n"
