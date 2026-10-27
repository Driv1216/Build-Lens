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


# ------------------------------------------------------------------ composite evidence

SOURCE = "def a():\n    return 1\n\n\ndef b():\n    return 2\n\nSECRET_UNDELIVERED = 3\n"


def _composite(text: str, constituents: list[tuple[int, int]] | None, **overrides: Any) -> Any:
    from buildlens.domain.lab import sha256_hex

    detail: dict[str, Any] = {"anchor_method": "composite"}
    if constituents is not None:
        detail["constituents"] = [
            {"start_offset": s, "end_offset": e, "text_sha256": sha256_hex(SOURCE[s:e])}
            for s, e in constituents
        ]
    item = {
        "ref": "w1",
        "rank": 1,
        "text_sha256": sha256_hex(text),
        "text_bytes": len(text.encode()),
        "locator": "m.py",
        "span": {"start_offset": 0, "end_offset": len(SOURCE)},  # bounding span: never trusted
        "anchored": True,
        "chunk_id": None,
        "detail": detail,
        **overrides,
    }
    return item, {item["text_sha256"]: text}


def _verify(item: dict[str, Any], texts: dict[str, str]) -> tuple[dict[str, Any], dict[str, int]]:
    from buildlens.application.lab import EvidenceMaps, LabService

    maps = EvidenceMaps({}, {}, {"m.py": SOURCE}, {"m.py": "fp"})
    verification = {
        "rebound_chunks": 0,
        "verified_slices": 0,
        "rejected_anchors": 0,
        "composite": 0,
    }
    return LabService._verify_item(dict(item), maps, texts, verification), verification


A, B = (0, 21), (24, 45)  # "def a()…1" and "def b()…2"; "\n\n\n" between them


def test_composite_without_constituents_is_not_anchored() -> None:
    """Defect: a bounding span alone was accepted and covered undelivered source text."""
    item, texts = _composite("def a():\n    return 1", None)
    verified, verification = _verify(item, texts)
    assert verified["anchored"] is False and verified["span"] is None
    assert verification["rejected_anchors"] == 1 and verification["composite"] == 0


def test_composite_credits_only_verified_constituents() -> None:
    window = SOURCE[A[0] : A[1]] + " " + SOURCE[B[0] : B[1]]  # LlamaIndex-style " " join
    item, texts = _composite(window, [A, B, (47, 70)])  # third: never delivered
    verified, verification = _verify(item, texts)
    # Whitespace-only gaps (source "\n\n\n", delivered " ") join; undelivered text is excluded.
    assert verified["spans"] == [{"start_offset": 0, "end_offset": 45}]
    assert verified["span"] == {"start_offset": 0, "end_offset": 45}
    assert verified["detail"]["constituents_verified"] == 2
    assert verified["detail"]["constituents_rejected"] == 1
    assert verification["composite"] == 1


def test_composite_rejects_forged_hashes_and_non_whitespace_gaps() -> None:
    from buildlens.domain.lab import sha256_hex

    window = SOURCE[A[0] : A[1]] + " … " + SOURCE[B[0] : B[1]]
    item, texts = _composite(window, [A, B])
    item["detail"]["constituents"][1]["text_sha256"] = sha256_hex("forged")
    verified, _ = _verify(item, texts)
    assert verified["spans"] == [{"start_offset": 0, "end_offset": 21}]  # forged one dropped
    item, texts = _composite(window, [A, B])
    verified, _ = _verify(item, texts)  # separator is not whitespace: separate segments
    assert verified["spans"] == [
        {"start_offset": 0, "end_offset": 21},
        {"start_offset": 24, "end_offset": 45},
    ]
    item, texts = _composite(window, [A, B])
    verified, verification = _verify(item, {})  # delivered text missing: nothing verifiable
    assert verified["anchored"] is False and verification["rejected_anchors"] == 1


def test_coverage_counts_only_verified_composite_segments() -> None:
    from buildlens.application.evaluation_metrics import target_covered
    from buildlens.application.lab_evaluation import rows_for
    from buildlens.domain.evaluation import EvidenceTarget

    window = SOURCE[A[0] : A[1]] + " … " + SOURCE[B[0] : B[1]]
    item, texts = _composite(window, [A, B])
    verified, _ = _verify(item, texts)
    verified["artifact_fingerprint"] = "fp"

    class _Corpus:
        chunks: list[Any] = []

    rows, _ = rows_for([verified], _Corpus())  # type: ignore[arg-type]

    def target(start: int, end: int) -> EvidenceTarget:
        return EvidenceTarget("t", "m.py", "fp", start, end, "excerpt-hash")

    assert target_covered(target(2, 20), rows) and target_covered(target(25, 40), rows)
    assert not target_covered(target(10, 30), rows)  # crosses the unverified gap
    assert not target_covered(target(50, 60), rows)  # inside the old bounding span only
