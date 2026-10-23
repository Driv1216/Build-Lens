"""Stage 12 tiered scoring: derived projection, matched subsets and paired comparisons."""

from __future__ import annotations

from typing import Any

from buildlens.application.lab_evaluation import (
    _merge,
    flatten_derived,
    matched_cases,
    paired,
    summarize,
)


def _record(
    case: str, pipeline: str, value: float, *, available: bool = True, outcome: str = "success"
) -> dict[str, Any]:
    return {
        "case_id": case,
        "pipeline": pipeline,
        "category": "semantic",
        "answerable": True,
        "outcome": outcome,
        "targets_available": available,
        "timings_ms": [10.0],
        "tiers": {
            "approved": {"MRR@32": value},
            "span": {"context_coverage": value},
            "derived": {"eligible": False},
        },
    }


def test_span_merge_handles_overlap_and_gaps() -> None:
    assert _merge([(0, 5), (3, 8), (10, 12), (12, 15)]) == [(0, 8), (10, 15)]


def test_matched_subset_excludes_cases_any_pipeline_could_not_see() -> None:
    records = [
        _record("a", "native", 1.0),
        _record("a", "framework", 0.5),
        _record("b", "native", 1.0),
        _record("b", "framework", 0.0, available=False),
        _record("c", "native", 1.0),
        _record("c", "framework", 1.0, outcome="error"),
    ]
    assert matched_cases(records, ["native", "framework"]) == {"a"}
    summary = summarize(records, ["native", "framework"])
    assert summary["means"]["framework"]["span:context_coverage"]["n"] == 2
    assert summary["matched"]["means"]["framework"]["span:context_coverage"] == {
        "mean": 0.5,
        "n": 1,
    }


def test_paired_comparison_counts_wins_losses_ties_deterministically() -> None:
    records = [_record(c, "base", v) for c, v in (("a", 0.5), ("b", 0.5), ("c", 0.5))]
    records += [_record(c, "cand", v) for c, v in (("a", 1.0), ("b", 0.0), ("c", 0.5))]
    first = paired(records, "base", "cand", "span:context_coverage")
    assert (first["wins"], first["losses"], first["ties"], first["n"]) == (1, 1, 1, 3)
    assert first == paired(records, "base", "cand", "span:context_coverage")
    assert "not a significance test" in first["note"]


def test_derived_metrics_flatten_only_when_eligible() -> None:
    assert flatten_derived({"eligible": False}) == {}
    derived = {
        "eligible": True,
        "rr@32": 0.5,
        **{str(k): {"recall": 0.25, "precision": None} for k in (1, 5, 10, 32)},
    }
    flat = flatten_derived(derived)
    assert flat["derived_MRR@32"] == 0.5 and flat["derived_recall@5"] == 0.25
    assert "derived_precision@5" not in flat
