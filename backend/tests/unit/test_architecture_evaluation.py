"""Stage 14 Milestone D: the evaluation scorer cannot pass by abstaining or under-predicting."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from buildlens.application.architecture_evaluation import (
    API_LABELS_VERSION,
    API_PACKET_VERSION,
    MODULE_UNITS_VERSION,
    EvaluationError,
    classify,
    labels_template,
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
    """A synthetic packet and sealed answers (never the real, sealed evaluation answers)."""
    prereg = approved()
    packet: dict[str, Any] = {
        "version": API_PACKET_VERSION,
        "preregistration_digest": digest(prereg),
        "corpus_tree_sha256": TREE,
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
            "S001": {"method": "GET", "branches": [{"outcome": "conditional_candidate",
                     "candidates": ["E001"], "destination": "local_loopback"}]},
            "S002": {"method": "GET", "branches": [{"outcome": "no_candidate", "candidates": [],
                     "destination": "same_origin_relative"}]},
        },
        "endpoints": {"E001": {"methods": ["GET"], "paths": ["/x"]}},
        "sample_sites": {"web/a.ts": [1, 2]},
    }  # fmt: skip
    _reseal(sealed)
    return packet, sealed


def _reseal(sealed: dict[str, Any]) -> None:
    sealed["sealed_digest"] = digest({k: v for k, v in sealed.items() if k != "sealed_digest"})


def _labels(packet: dict[str, Any], second: list[str]) -> dict[str, Any]:
    return {
        "version": API_LABELS_VERSION,
        "packet_digest": packet["packet_digest"],
        "reviewer": "owner",
        "sites": {
            "S001": {"statically_determinable": True, "method": "GET", "targets": ["E001"]},
            "S002": {"statically_determinable": second != ["opaque"], "targets": second},
        },
        "endpoints": {"E001": {"determinable": True, "methods": ["GET"], "paths": ["/x"]}},
        "sink_sample": {"web/a.ts": {"reviewed": True, "request_lines": [1, 2, 7]}},
    }


def test_api_scoring_is_mechanical_and_refuses_tampering() -> None:
    packet, sealed = _packet()
    result = score_api(approved(), packet, sealed, _labels(packet, ["E001"]))
    assert result["summary"]["recall"]["value"] == 0.5
    assert result["summary"]["abstention_rate"]["value"] == 0.5
    assert result["overall"] == "failed"  # recall 0.5 < 0.80 and abstention 0.5 > 0.20
    assert result["sink_recall"]["value"] == round(2 / 3, 6)
    assert result["endpoints"]["paths_equal"]["value"] == 1.0
    assert result["site_methods_equal"]["value"] == 1.0
    tampered = copy.deepcopy(packet)
    tampered["sites"].pop()
    with pytest.raises(EvaluationError, match="PACKET_CHANGED"):
        score_api(approved(), tampered, sealed, _labels(packet, ["none"]))
    forged = copy.deepcopy(sealed)
    forged["sites"]["S002"]["branches"][0]["outcome"] = "conditional_candidate"
    with pytest.raises(EvaluationError, match="SEALED_CHANGED"):
        score_api(approved(), packet, forged, _labels(packet, ["none"]))
    incomplete = _labels(packet, ["none"])
    del incomplete["sites"]["S002"]
    with pytest.raises(EvaluationError, match="LABELS_INCOMPLETE"):
        score_api(approved(), packet, sealed, incomplete)
    other = approved()
    other["suites"]["self_api"]["recall_min"] = 0.1
    with pytest.raises(EvaluationError, match="PACKET_CHANGED"):
        score_api(other, packet, sealed, _labels(packet, ["none"]))


def _broken(change: Any) -> dict[str, Any]:
    packet, _ = _packet()
    labels = _labels(packet, ["none"])
    change(labels)
    return labels


@pytest.mark.parametrize(
    ("change", "code"),
    [
        # R01: every section must be complete, not only the request sites.
        (lambda v: v["endpoints"].pop("E001"), "LABELS_INCOMPLETE"),
        (lambda v: v.pop("endpoints"), "LABELS_INCOMPLETE"),
        (lambda v: v["endpoints"]["E001"].update(determinable=None), "LABELS_INCOMPLETE"),
        (lambda v: v["endpoints"]["E001"].update(paths=[]), "LABELS_INCOMPLETE"),
        (lambda v: v["sink_sample"].pop("web/a.ts"), "LABELS_INCOMPLETE"),
        (lambda v: v.pop("sink_sample"), "LABELS_INCOMPLETE"),
        (lambda v: v["sink_sample"]["web/a.ts"].update(reviewed=False), "LABELS_INCOMPLETE"),
        (lambda v: v["sites"]["S001"].update(targets=[]), "LABELS_INCOMPLETE"),
        (lambda v: v.update(reviewer=" "), "LABELS_INCOMPLETE"),
        # unexpected units and fields
        (
            lambda v: v["sites"].update(
                S999={"statically_determinable": True, "targets": ["none"]}
            ),
            "LABELS_UNEXPECTED_UNIT",
        ),
        (
            lambda v: v["endpoints"].update(E999=dict(v["endpoints"]["E001"])),
            "LABELS_UNEXPECTED_UNIT",
        ),
        (
            lambda v: v["sink_sample"].update(
                {"web/b.ts": {"reviewed": True, "request_lines": []}}
            ),
            "LABELS_UNEXPECTED_UNIT",
        ),
        (lambda v: v["sites"]["S001"].update(confidence=0.9), "LABELS_UNEXPECTED_FIELD"),
        (lambda v: v.update(scores={}), "LABELS_UNEXPECTED_FIELD"),
        # duplicates and vocabulary
        (lambda v: v["sites"]["S001"].update(targets=["E001", "E001"]), "LABELS_DUPLICATE_VALUE"),
        (
            lambda v: v["sink_sample"]["web/a.ts"].update(request_lines=[1, 1]),
            "LABELS_DUPLICATE_VALUE",
        ),
        (lambda v: v["endpoints"]["E001"].update(methods=["GET", "GET"]), "LABELS_DUPLICATE_VALUE"),
        (lambda v: v["sites"]["S001"].update(targets=["E404"]), "LABELS_UNKNOWN_ENDPOINT"),
        (lambda v: v["sites"]["S001"].update(targets=["external", "E001"]), "LABELS_INVALID"),
        (lambda v: v["sites"]["S001"].update(targets=["opaque"]), "LABELS_INVALID"),
        (lambda v: v["sites"]["S002"].update(statically_determinable=False), "LABELS_INVALID"),
        (
            lambda v: v["sites"]["S001"].update(destination_class="external_service"),
            "LABELS_INVALID",
        ),
        (lambda v: v["endpoints"]["E001"].update(methods=["FETCH"]), "LABELS_INVALID"),
        (lambda v: v["endpoints"]["E001"].update(paths=["x"]), "LABELS_INVALID"),
        (lambda v: v["endpoints"]["E001"].update(determinable=False), "LABELS_INVALID"),
        (lambda v: v["sink_sample"]["web/a.ts"].update(request_lines=[0]), "LABELS_INVALID"),
        (lambda v: v["sink_sample"]["web/a.ts"].update(request_lines=[True]), "LABELS_INVALID"),
        (lambda v: v.update(version="architecture-api-labels.v1"), "LABELS_VERSION"),
        (lambda v: v.update(packet_digest="0" * 64), "LABELS_FOR_ANOTHER_PACKET"),
    ],
)
def test_incomplete_or_malformed_api_labels_are_refused(change: Any, code: str) -> None:
    packet, sealed = _packet()
    with pytest.raises(EvaluationError, match=code):
        score_api(approved(), packet, sealed, _broken(change))


def test_untouched_labels_template_cannot_be_scored() -> None:
    packet, sealed = _packet()
    template = labels_template(packet)
    template["reviewer"] = "owner"
    with pytest.raises(EvaluationError, match="LABELS_INCOMPLETE"):
        score_api(approved(), packet, sealed, template)
    tampered = copy.deepcopy(packet)
    tampered["sites"].pop()
    with pytest.raises(EvaluationError, match="PACKET_CHANGED"):
        labels_template(tampered)


def test_duplicate_json_keys_in_a_label_file_are_refused(tmp_path: Path) -> None:
    from buildlens.infrastructure.evaluation.local import read_json

    path = tmp_path / "labels.json"
    path.write_text('{"sites": {"S001": {}, "S001": {}}}')
    with pytest.raises(ValueError, match="DUPLICATE_JSON_KEY"):
        read_json(path)


def test_external_opaque_none_and_endpoint_targets_are_scored_by_label() -> None:
    packet, sealed = _packet()
    sealed["sites"]["S002"]["branches"] = [
        {"outcome": "external_destination", "candidates": [],
         "destination": "public_host_unbound"},
    ]  # fmt: skip
    _reseal(sealed)
    external = score_api(approved(), packet, sealed, _labels(packet, ["external"]))
    outcomes = {unit["unit"]: unit["outcome"] for unit in external["units"]}
    # v1 turned "external" into "none": a correct external call could never count.
    assert outcomes == {"S001": "emitted_correct", "S002": "emitted_correct"}
    assert external["summary"]["oracle_positive"] == 2
    opaque = score_api(approved(), packet, sealed, _labels(packet, ["opaque"]))
    assert {u["unit"]: u["outcome"] for u in opaque["units"]}["S002"] == "emitted_incorrect"
    # The reviewer says the call is external; the system connected it to a local endpoint.
    _, local = _packet()
    local["sites"]["S002"] = copy.deepcopy(local["sites"]["S001"])
    _reseal(local)
    result = score_api(approved(), packet, local, _labels(packet, ["external"]))
    gates = {row["name"]: row for row in result["criteria"]}
    assert gates["unbound_host_local_connections_max"]["value"] == 1
    assert not gates["unbound_host_local_connections_max"]["passed"]
    assert result["overall"] == "failed"


def test_api_hard_gates_use_the_sealed_values() -> None:
    packet, sealed = _packet()
    sealed["sites"]["S002"]["branches"] = [
        {"outcome": "conditional_candidate", "candidates": ["E001"],
         "destination": "public_host_unbound"},
        {"outcome": "external_destination", "candidates": [], "destination": "external_service"},
    ]  # fmt: skip
    _reseal(sealed)
    result = score_api(approved(), packet, sealed, _labels(packet, ["E001"]))
    gates = {row["name"]: row for row in result["criteria"]}
    # v1 hard-coded external_service_labels to 0.
    assert gates["external_service_labels_max"]["value"] == 1
    assert gates["unbound_host_local_connections_max"]["value"] == 1
    assert result["gates"]["unbound_host_local_connection_sites"] == ["S002"]
    assert result["overall"] == "failed"
    invalid = copy.deepcopy(sealed)
    invalid["sites"]["S002"]["branches"][0]["candidates"] = ["E404"]
    _reseal(invalid)
    with pytest.raises(EvaluationError, match="SEALED_INVALID"):
        score_api(approved(), packet, invalid, _labels(packet, ["E001"]))
    missing = copy.deepcopy(sealed)
    del missing["endpoints"]["E001"]
    _reseal(missing)
    with pytest.raises(EvaluationError, match="SEALED_INVALID"):
        score_api(approved(), packet, missing, _labels(packet, ["E001"]))


def test_api_undefined_metrics_fail_instead_of_passing() -> None:
    packet, sealed = _packet()
    for unit in ("S001", "S002"):
        sealed["sites"][unit]["branches"] = [
            {"outcome": "no_candidate", "candidates": [], "destination": "same_origin_relative"}
        ]
    _reseal(sealed)
    labels = _labels(packet, ["none"])
    labels["sites"]["S001"]["targets"] = ["none"]
    result = score_api(approved(), packet, sealed, labels)
    gates = {row["name"]: row for row in result["criteria"]}
    # No oracle positives and no emissions: precision and recall are undefined, so they fail.
    assert gates["precision_min"]["value"] is None and not gates["precision_min"]["passed"]
    assert gates["recall_min"]["value"] is None and not gates["recall_min"]["passed"]
    assert result["overall"] == "failed"
    empty_packet: dict[str, Any] = {**packet, "sites": [], "sink_sample": {}}
    empty_packet.pop("packet_digest")
    empty_packet["packet_digest"] = digest(empty_packet)
    empty_sealed: dict[str, Any] = {**sealed, "packet_digest": empty_packet["packet_digest"],
                                    "sites": {}, "sample_sites": {}}  # fmt: skip
    _reseal(empty_sealed)
    empty_labels = {**_labels(empty_packet, ["none"]), "sites": {}, "sink_sample": {}}
    assert score_api(approved(), empty_packet, empty_sealed, empty_labels)["overall"] == (
        "inconclusive"
    )


def test_endpoint_and_sink_metrics_are_diagnostic_and_never_move_the_verdict() -> None:
    """The frozen self_api suite gates site metrics and two hard gates only (pre-registration
    ``suites.self_api``; ``sink_sample.reported`` says "not gated")."""
    packet, sealed = _packet()
    agreeing = score_api(approved(), packet, sealed, _labels(packet, ["none"]))
    assert agreeing["overall"] == "passed"
    assert {row["name"] for row in agreeing["criteria"]} == set(approved()["suites"]["self_api"])
    disagreeing = _labels(packet, ["none"])
    disagreeing["endpoints"]["E001"] = {"determinable": True, "methods": ["POST"], "paths": ["/y"]}
    disagreeing["sink_sample"]["web/a.ts"] = {"reviewed": True, "request_lines": [9]}
    disagreeing["sites"]["S001"]["method"] = "DELETE"
    result = score_api(approved(), packet, sealed, disagreeing)
    assert result["endpoints"]["paths_equal"]["value"] == 0.0
    assert result["endpoints"]["methods_equal"]["value"] == 0.0
    assert result["site_methods_equal"]["value"] == 0.0
    assert result["sink_recall"]["value"] == 0.0
    assert result["overall"] == "passed" and result["criteria"] == agreeing["criteria"]
    assert result["verdict_basis"]["diagnostic_only"] == [
        "endpoints.paths_equal", "endpoints.methods_equal", "site_methods_equal", "sink_recall",
        "summary.coverage", "summary.ambiguous_set_recall",
    ]  # fmt: skip
    # Completeness is a precondition, not a metric: an unreviewed file stops scoring entirely.
    disagreeing["sink_sample"]["web/a.ts"]["reviewed"] = False
    with pytest.raises(EvaluationError, match="LABELS_INCOMPLETE"):
        score_api(approved(), packet, sealed, disagreeing)


def test_extra_system_predictions_count_against_precision() -> None:
    """R02: 1,001 correct units plus 100 predictions for statements the oracle never saw."""
    labels = [{"label": "module", "targets": ["b.py"]}] * 1000
    data = _units([{"label": "module", "targets": ["b.py"]}] * 1000, labels)
    data["oracle"]["units"].append(
        {"unit": "t0", "locator": "web/x.ts", "language": "typescript", "label": "external"}
    )
    data["system"]["units"].append(
        {"unit": "t0", "locator": "web/x.ts", "system": {"label": "external", "targets": []}}
    )
    clean = score_module_units(approved(), data["oracle"], data["system"])
    assert clean["overall"] == "passed" and clean["false_positives"] == 0
    injected = copy.deepcopy(data)
    injected["system"]["units"] += [
        {"unit": f"a.py|from|0|m{i}|x|1", "locator": "a.py",
         "system": {"label": "module", "targets": [f"m{i}.py"]}}
        for i in range(100)
    ]  # fmt: skip
    # Out of scope, listed but not scored: an ineligible file, and a unit with no prediction.
    injected["system"]["coverage"]["gen/skip.py"] = "not_analysed"
    injected["system"]["units"] += [
        {"unit": "gen/skip.py|import|0||y|1", "locator": "gen/skip.py",
         "system": {"label": "module", "targets": ["y.py"]}},
        {"unit": "a.py|import|0||z|1", "locator": "a.py", "system": {"label": "unknown"}},
    ]  # fmt: skip
    # Even explained as oracle limitations, the extra predictions stay in the metrics.
    notes = {f"a.py|from|0|m{i}|x|1": {"code": "ORACLE_LIMITATION"} for i in range(100)}
    result = score_module_units(approved(), injected["oracle"], injected["system"], notes)
    summary = result["summary"]
    assert summary["precision"]["numerator"] == 1001
    assert summary["precision"]["denominator"] == 1101
    assert summary["recall"] == clean["summary"]["recall"]
    assert summary["coverage"] == clean["summary"]["coverage"]
    assert result["false_positives"] == 100 and result["system_only_emitted"] == 100
    assert result["disagreement_counts"] == {"emitted_incorrect": 100}
    assert {u["reason"] for u in result["out_of_scope_system_units"]} == {
        "FILE_NOT_ELIGIBLE", "NO_PREDICTION",
    }  # fmt: skip
    assert result["unexplained"] == 0
    gates = {row["name"]: row for row in result["criteria"]}
    assert not gates["precision_min"]["passed"] and result["overall"] == "failed"


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
