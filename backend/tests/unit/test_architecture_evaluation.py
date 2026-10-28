"""Stage 14 Milestone D: the evaluation scorer cannot pass by abstaining or under-predicting."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from buildlens.application.architecture_evaluation import (
    API_LABELS_VERSION,
    API_PACKET_VERSION,
    MODULE_UNITS_VERSION,
    EvaluationError,
    classify,
    preregistration_template,
    require_approved,
    score_api,
    score_module_units,
    sink_sample,
    summarise,
    wilson,
)
from buildlens.domain.source import digest

TREE = "a" * 64


def approved() -> dict[str, Any]:
    value = preregistration_template()
    value["status"] = "approved"
    value["approved_by"] = "owner"
    value["corpus"] |= {"commit": "fdc6761", "tree_sha256": TREE}
    value["oracle"] |= {
        "python_roots": ["src"],
        "typescript_options": {"moduleResolution": "bundler"},
        "typescript_version": "5.7.3",
    }
    return value


def test_preregistration_must_be_approved_complete_and_unchanged() -> None:
    with pytest.raises(EvaluationError, match="NOT_APPROVED"):
        require_approved(preregistration_template())
    value = approved()
    assert require_approved(value) == digest(value)
    changed = copy.deepcopy(value)
    changed["outcomes"]["abstained"] = "anything goes"
    with pytest.raises(EvaluationError, match="TEXT_MISMATCH"):
        require_approved(changed)
    loose = copy.deepcopy(value)
    loose["suites"]["self_module"]["precision_min"] = None
    with pytest.raises(EvaluationError, match="INCOMPLETE"):
        require_approved(loose)
    unbound = copy.deepcopy(value)
    unbound["corpus"]["tree_sha256"] = None
    with pytest.raises(EvaluationError, match="INCOMPLETE"):
        require_approved(unbound)


def test_classification_and_intervals() -> None:
    module = {"label": "module", "targets": ["a.py"]}
    assert classify(module, {"label": "module", "targets": ["a.py"]}, True) == "emitted_correct"
    assert classify(module, {"label": "module", "targets": ["b.py"]}, True) == "emitted_incorrect"
    assert classify(module, {"label": "external"}, True) == "emitted_incorrect"
    assert classify(module, {"label": "unknown"}, True) == "abstained"
    assert classify({"label": "none"}, {"label": "module", "targets": ["a.py"]}, True) == (
        "emitted_incorrect"
    )
    assert classify({"label": "unknown"}, {"label": "unknown"}, True) == "correct_abstention"
    assert classify(module, {"label": "module", "targets": ["a.py"]}, False) == "ineligible"
    assert wilson(0, 0) is None
    low, high = wilson(99, 100) or [0, 0]
    assert 0.94 < low < 0.99 < high <= 1.0


def _units(outputs: list[dict[str, Any]], labels: list[dict[str, Any]]) -> dict[str, Any]:
    oracle = {
        "corpus_tree_sha256": TREE,
        "units": [
            {"unit": f"u{i}", "locator": "a.py", "language": "python", **label}
            for i, label in enumerate(labels)
        ],
    }
    system = {
        "version": MODULE_UNITS_VERSION,
        "corpus_tree_sha256": TREE,
        "coverage": {"a.py": "analysed", "web/x.ts": "analysed"},
        "units": [
            {"unit": f"u{i}", "locator": "a.py", "system": out} for i, out in enumerate(outputs)
        ],
    }
    return {"oracle": oracle, "system": system}


def test_empty_or_abstaining_output_fails() -> None:
    labels = [{"label": "module", "targets": ["b.py"]}] * 3
    silent = _units([{"label": "unknown"}] * 3, labels)
    result = score_module_units(approved(), silent["oracle"], silent["system"], {
        f"u{i}": {"code": "DOCUMENTED_LIMITATION"} for i in range(3)
    })  # fmt: skip
    # Python has eligible units but TypeScript has none: the suite cannot pass.
    assert result["overall"] == "inconclusive"
    by_name = {row["name"]: row for row in result["criteria"]}
    assert by_name["recall_min"]["value"] == 0.0 and not by_name["recall_min"]["passed"]
    assert by_name["precision_min"]["value"] is None and not by_name["precision_min"]["passed"]


def test_unexplained_disagreements_and_alias_edges_fail_the_gates() -> None:
    data = _units(
        [{"label": "module", "targets": ["b.py"]}, {"label": "module", "targets": ["c.py"]}],
        [{"label": "module", "targets": ["b.py"]}, {"label": "module", "targets": ["b.py"]}],
    )
    data["oracle"]["units"].append(
        {"unit": "t0", "locator": "web/x.ts", "language": "typescript", "alias": True,
         "label": "unknown", "targets": []}
    )  # fmt: skip
    data["system"]["units"].append(
        {"unit": "t0", "locator": "web/x.ts", "system": {"label": "module", "targets": ["y.ts"]}}
    )
    result = score_module_units(approved(), data["oracle"], data["system"])
    gates = {row["name"]: row for row in result["criteria"]}
    assert gates["unexplained_disagreements_max"]["value"] == 2  # u1 and the alias unit
    assert gates["alias_resolved_edges_max"]["value"] == 1
    assert result["overall"] == "failed"
    # An explanation never changes the metrics: a defect still costs precision.
    explained = score_module_units(
        approved(), data["oracle"], data["system"], {"u1": {"code": "SYSTEM_DEFECT"}}
    )
    assert explained["summary"]["precision"]["value"] == 0.5
    assert explained["system_defects"] == 1


def test_wrong_corpus_is_refused() -> None:
    data = _units([], [])
    data["system"]["corpus_tree_sha256"] = "b" * 64
    with pytest.raises(EvaluationError, match="SYSTEM_CORPUS_MISMATCH"):
        score_module_units(approved(), data["oracle"], data["system"])


def _packet() -> tuple[dict[str, Any], dict[str, Any]]:
    prereg = approved()
    packet: dict[str, Any] = {
        "version": API_PACKET_VERSION,
        "preregistration_digest": digest(prereg),
        "sites": [{"unit": "S001", "locator": "web/a.ts", "line": 1},
                  {"unit": "S002", "locator": "web/a.ts", "line": 2}],
        "endpoints": [{"unit": "E001", "locator": "app.py", "function": "f", "excerpt": []}],
        "sink_sample": {"web/a.ts": []},
    }  # fmt: skip
    packet["packet_digest"] = digest(packet)
    sealed: dict[str, Any] = {
        "version": API_PACKET_VERSION,
        "packet_digest": packet["packet_digest"],
        "sites": {
            "S001": {"branches": [{"outcome": "conditional_candidate", "candidates": ["E001"],
                                   "destination": "local_loopback"}]},
            "S002": {"branches": [{"outcome": "no_candidate", "candidates": [],
                                   "destination": "same_origin_relative"}]},
        },
        "endpoints": {"E001": {"methods": ["GET"], "paths": ["/x"]}},
        "sample_sites": {"web/a.ts": [1, 2]},
    }  # fmt: skip
    sealed["sealed_digest"] = digest({k: v for k, v in sealed.items() if k != "sealed_digest"})
    return packet, sealed


def _labels(packet: dict[str, Any], second: list[str]) -> dict[str, Any]:
    return {
        "version": API_LABELS_VERSION,
        "packet_digest": packet["packet_digest"],
        "reviewer": "owner",
        "sites": {
            "S001": {"statically_determinable": True, "targets": ["E001"]},
            "S002": {"statically_determinable": True, "targets": second},
        },
        "endpoints": {"E001": {"methods": ["GET"], "paths": ["/x"]}},
        "sink_sample": {"web/a.ts": [1, 2, 7]},
    }


def test_api_scoring_is_mechanical_and_refuses_tampering() -> None:
    packet, sealed = _packet()
    result = score_api(approved(), packet, sealed, _labels(packet, ["E001"]))
    assert result["summary"]["recall"]["value"] == 0.5
    assert result["summary"]["abstention_rate"]["value"] == 0.5
    assert result["overall"] == "failed"  # recall 0.5 < 0.80 and abstention 0.5 > 0.20
    assert result["sink_recall"]["value"] == round(2 / 3, 6)
    assert result["endpoints"]["paths_equal"]["value"] == 1.0
    tampered = copy.deepcopy(packet)
    tampered["sites"].pop()
    with pytest.raises(EvaluationError, match="PACKET_CHANGED"):
        score_api(approved(), tampered, sealed, _labels(packet, []))
    forged = copy.deepcopy(sealed)
    forged["sites"]["S002"]["branches"][0]["outcome"] = "conditional_candidate"
    with pytest.raises(EvaluationError, match="SEALED_CHANGED"):
        score_api(approved(), packet, forged, _labels(packet, []))
    incomplete = _labels(packet, [])
    del incomplete["sites"]["S002"]
    with pytest.raises(EvaluationError, match="LABELS_INCOMPLETE"):
        score_api(approved(), packet, sealed, incomplete)
    other = approved()
    other["suites"]["self_api"]["recall_min"] = 0.1
    with pytest.raises(EvaluationError, match="PACKET_CHANGED"):
        score_api(other, packet, sealed, _labels(packet, []))


def test_sink_sample_is_seeded_and_stratified() -> None:
    files = [f"web/{d}/f{i}.ts" for d in ("api", "pages", "components") for i in range(5)]
    files += ["web/api/x.test.ts", "app/main.py"]
    first = sink_sample(files, 14, 6)
    assert first == sink_sample(reversed(files), 14, 6)
    assert len(first) == 6 and not any(".test." in f or f.endswith(".py") for f in first)
    assert {f.split("/")[1] for f in first} == {"api", "pages", "components"}


def test_summary_counts_every_outcome() -> None:
    units = [
        {"outcome": "emitted_correct", "oracle": {"label": "module"}, "system": {}},
        {"outcome": "ineligible", "oracle": {"label": "module"}, "system": {}},
        {"outcome": "abstained", "oracle": {"label": "module", "targets": ["a"]},
         "system": {"candidates": ["a", "b"]}},
    ]  # fmt: skip
    summary = summarise(units)
    assert summary["coverage"]["value"] == round(2 / 3, 6)
    assert summary["ambiguous_set_recall"]["value"] == 1.0
