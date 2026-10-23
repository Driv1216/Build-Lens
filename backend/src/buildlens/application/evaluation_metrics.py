"""Deterministic, stage-named structural measurements, never semantic grading."""

from __future__ import annotations

from typing import Any

from buildlens.domain.evaluation import (
    CUTOFFS,
    CaseBinding,
    EvaluationCase,
    EvaluationError,
    EvidenceTarget,
    primitive,
)


def target_covered(target: EvidenceTarget, rows: list[dict[str, Any]]) -> bool:
    spans = sorted(
        (row["span"]["start_offset"], row["span"]["end_offset"])
        for row in rows
        if row["locator"] == target.locator
        and row.get("artifact_fingerprint") == target.artifact_fingerprint
    )
    cursor = target.start_offset
    for start, end in spans:
        if start > cursor:
            break
        cursor = max(cursor, end)
        if cursor >= target.end_offset:
            return True
    return False


def coverage(
    case: EvaluationCase, binding: CaseBinding, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    covered = {name for name, target in binding.targets.items() if target_covered(target, rows)}
    satisfied = [
        r.id for r in case.requirements if any(set(alt) <= covered for alt in r.alternatives)
    ]
    missing = [r.id for r in case.requirements if r.id not in satisfied]
    count = len(case.requirements)
    return {
        "coverage": len(satisfied) / count if count else None,
        "complete": not missing if count else None,
        "satisfied": satisfied,
        "missing": missing,
        "denominator": count,
    }


def ranking_metrics(
    case: EvaluationCase, binding: CaseBinding, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    ids = [row["chunk_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise EvaluationError("DUPLICATE_RANKING_OBSERVATION")
    relevant = {key for key, relevant in binding.judged.items() if relevant}
    result: dict[str, Any] = {
        "provisional": any(key not in binding.judged for key in ids)
        or (case.answerable and not relevant),
        "reviewed_relevant_count": len(relevant),
        "returned_count": len(ids),
    }
    result["rr@32"] = (
        next((1 / rank for rank, key in enumerate(ids[:32], 1) if key in relevant), 0.0)
        if case.answerable and relevant
        else None
    )
    result["recall_status"] = (
        "eligible"
        if case.answerable and relevant
        else "ground_truth_missing"
        if case.answerable
        else "not_applicable"
    )
    for k in CUTOFFS:
        observed = ids[:k]
        hits = len(relevant & set(observed))
        judged_count = sum(key in binding.judged for key in observed)
        result[str(k)] = {
            "recall": hits / len(relevant) if case.answerable and relevant else None,
            "precision": hits / k if case.answerable and judged_count == len(observed) else None,
            "judgment_coverage": judged_count / len(observed) if observed else 1.0,
            "judged_positions": judged_count,
            "returned_positions": len(observed),
            "required_evidence": coverage(case, binding, rows[:k]),
        }
    return result


def score_result(
    case: EvaluationCase, binding: CaseBinding, observations: dict[str, Any], outcome: str
) -> dict[str, Any]:
    if outcome != "success":
        return {"eligible": False, "outcome": outcome}
    result: dict[str, Any] = {"eligible": True}
    for stage in ("retrieval", "ranking"):
        rows = observations.get(stage)
        result[stage] = ranking_metrics(case, binding, rows) if rows is not None else None
    for stage in ("context", "generation_evidence", "cited_evidence"):
        rows = observations.get(stage)
        result[stage] = coverage(case, binding, rows) if rows is not None else None
    answer = observations.get("answer")
    if answer is not None:
        evidence = observations.get("generation_evidence") or []
        evidence_by_id = {row["evidence_id"]: row for row in evidence}
        links = [
            (i, key)
            for i, statement in enumerate(answer["statements"])
            for key in statement["evidence_ids"]
        ]
        citations = observations.get("cited_evidence") or []
        structural = {
            "expected_status": "answered" if case.answerable else "insufficient_evidence",
            "status_matches": answer["status"]
            == ("answered" if case.answerable else "insufficient_evidence"),
            "output_validation": "passed",  # Only validated production results are projected.
            "citation_membership": all(key in evidence_by_id for _, key in links)
            and all(evidence_by_id.get(row["evidence_id"]) == row for row in citations),
            "abstention_structure": (not answer["statements"] and not citations)
            if answer["status"] == "insufficient_evidence"
            else None,
            "generation_skipped_empty_context": answer["generation_state"] == "skipped_no_evidence"
            if not observations.get("context")
            else None,
            "seed_limit_losses": max(0, len(observations.get("ranking") or []) - 8),
            "packing_losses": sum(
                decision["stage"] == "packing" and decision["disposition"] == "excluded"
                for decision in observations.get("context_decisions") or []
            ),
            "generation_repacking_losses": len(observations.get("context") or []) - len(evidence),
        }
        result["structural_answer"] = structural
    return result


def semantic_metrics(review: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """Binary fully-supported links, with partial support retained in raw review."""
    links = review["links"]
    statements = review["statements"]
    substantive = [s for s in statements if s["substantive"]]
    possible_links = sum(len(s["evidence_ids"]) for s in answer["statements"])
    return {
        "citation_precision": sum(link["judgment"] == "supported" for link in links) / len(links)
        if links
        else None,
        "citation_completeness": sum(s["sufficient_cited_support"] for s in substantive)
        / len(substantive)
        if substantive
        else None,
        "reviewed_links": len(links),
        "total_links": possible_links,
        "reviewed_statements": len(statements),
        "total_statements": len(answer["statements"]),
        "substantive_denominator": len(substantive),
        "correctness": review["correctness"],
        "abstention": review["abstention"],
        "facets": primitive(review["facets"]),
    }
