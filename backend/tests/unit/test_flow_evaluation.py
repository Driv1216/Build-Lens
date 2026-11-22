"""The evaluator must not turn missing/changed evidence or unknowns into a passing result."""

from __future__ import annotations

import copy
import random
from typing import Any

import pytest

from buildlens.application.flow_evaluation import (
    FACETS,
    LABELS_VERSION,
    PACKET_VERSION,
    SYSTEM_VERSION,
    FlowEvaluationError,
    bound,
    latency_summary,
    lcs_length,
    power_calculation,
    preregistration,
    score,
    validate_labels,
)


def fixture() -> tuple[dict[str, Any], ...]:
    prereg = preregistration([{"name": "demo", "tree_digest": "frozen"}], "owner instruction")
    calls = [
        {
            "id": str(i),
            "language": "python" if i < 150 else "typescript",
            "corpus": "demo",
            "form": "direct",
        }
        for i in range(300)
    ]
    packet = bound(
        {
            "version": PACKET_VERSION,
            "preregistration_digest": prereg["digest"],
            "calls": calls,
            "cases": [{"id": "C1"}],
            "queries": [{"id": "Q1"}],
        }
    )
    expected: dict[str, list[Any]] = {f: [] for f in FACETS}
    expected["path"] = ["a", "b", "a"]
    labels = bound(
        {
            "version": LABELS_VERSION,
            "packet_digest": packet["digest"],
            "basis": "ai_assisted",
            "reviewer": "AI, not human",
            "calls": [
                {
                    "id": c["id"],
                    "reviewed": True,
                    "state": "internal",
                    "targets": ["b"],
                    "reason": "source definition",
                }
                for c in calls
            ],
            "cases": [
                {
                    "id": "C1",
                    "reviewed": True,
                    "reason": "source journey",
                    "expected": expected,
                    "forbidden": ["decoy"],
                }
            ],
            "queries": [
                {"id": "Q1", "reviewed": True, "reason": "entry source", "targets": ["entry"]}
            ],
        }
    )
    observations = bound(
        {
            "version": SYSTEM_VERSION,
            "packet_digest": packet["digest"],
            "labels_digest": labels["digest"],
            "calls": [{"id": c["id"], "targets": ["b"], "extracted": True} for c in calls],
            "cases": [{"id": "C1", **expected}],
            "queries": [{"id": "Q1", "targets": ["other", "entry"]}],
        }
    )
    return prereg, packet, labels, observations


def test_correct_results_keep_ai_provenance_and_ordered_path_metrics() -> None:
    result = score(*fixture())
    assert result["status"] == "passed"
    assert result["independent_human_validation"] is False
    assert result["S0"]["whole_path_exact"]["value"] == 1
    assert result["S0"]["guard_status"] == "inconclusive"
    assert result["S1"]["MRR"] == 0.5
    assert lcs_length(["a", "a"], ["a"]) == 1


@pytest.mark.parametrize("record", [0, 1, 2, 3])
def test_any_record_tamper_refuses(record: int) -> None:
    values = list(fixture())
    values[record]["tamper"] = True
    with pytest.raises(FlowEvaluationError, match="DIGEST"):
        score(*values)


def test_missing_duplicate_unreviewed_and_wrong_packet_labels_refuse() -> None:
    _, packet, original, _ = fixture()
    for mutation in ("missing", "duplicate", "unreviewed", "packet", "blank"):
        labels = copy.deepcopy(original)
        if mutation == "missing":
            labels["calls"].pop()
        elif mutation == "duplicate":
            labels["calls"].append(labels["calls"][0])
        elif mutation == "unreviewed":
            labels["calls"][0]["reviewed"] = False
        elif mutation == "packet":
            labels["packet_digest"] = "different"
        else:
            labels["calls"][0]["reason"] = ""
        with pytest.raises(FlowEvaluationError):
            validate_labels(packet, bound(labels))


def test_unknowns_wrong_targets_and_missing_extraction_stay_in_denominators() -> None:
    pre, packet, labels, observed = fixture()
    for row in labels["calls"]:
        row.update(state="unknown", targets=[])
    labels = bound(labels)
    observed["labels_digest"] = labels["digest"]
    observed["calls"][0]["extracted"] = False
    observed = bound(observed)
    result = score(pre, packet, labels, observed)
    assert result["S3"]["python"]["precision"]["value"] == 0
    assert result["S3"]["python"]["overclaims"] == 150
    assert result["S3"]["python"]["recall"]["value"] is None
    assert result["S3"]["python"]["coverage"]["numerator"] == 149
    assert result["status"] != "passed"


def test_system_cannot_drop_bad_units_or_add_unbound_units() -> None:
    pre, packet, labels, observed = fixture()
    observed["calls"].pop()
    with pytest.raises(FlowEvaluationError, match="MEMBERSHIP"):
        score(pre, packet, labels, bound(observed))


def test_forbidden_link_and_omitted_branch_cannot_pass_exact_path() -> None:
    pre, packet, labels, observed = fixture()
    observed["cases"][0]["path"].append("decoy")
    labels["cases"][0]["expected"]["guards"] = [["b", "is_none", True]]
    labels = bound(labels)
    observed["labels_digest"] = labels["digest"]
    result = score(pre, packet, labels, bound(observed))
    assert result["status"] == "failed"
    case = result["S0"]["cases"][0]
    assert case["forbidden_emissions"] == ["decoy"]
    assert case["facets"]["guards"]["recall"]["value"] == 0
    assert result["S0"]["facets"]["guards"]["recall"]["denominator"] == 1
    assert result["S0"]["guard_status"] == "inconclusive"


def test_performance_requires_sample_count_inputs_and_stationarity() -> None:
    rng = random.Random(15)
    values = [rng.uniform(0.10, 0.11) for _ in range(59)]
    good = latency_summary(values, distinct_inputs=20)
    assert good["status"] == "passed" and good["confidence"] >= 0.95
    assert good["p95_upper95"] == max(values)
    assert latency_summary(values[:58], distinct_inputs=20)["status"] == "inconclusive"
    assert latency_summary(values, distinct_inputs=19)["status"] == "inconclusive"
    assert latency_summary(list(range(59)), distinct_inputs=20)["status"] == "not_stationary"
    assert latency_summary(list(range(59)), distinct_inputs=20)["p95_upper95"] is None
    assert latency_summary([], distinct_inputs=20)["status"] == "inconclusive"
    with pytest.raises(FlowEvaluationError):
        latency_summary([float("nan")], distinct_inputs=20)
    power = power_calculation()
    assert power["power"] > 0.8 and power["null_upper_tail"] < 0.05
