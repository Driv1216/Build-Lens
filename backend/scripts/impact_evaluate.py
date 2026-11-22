"""Score frozen Stage 16 quality and performance packets without running target code.

Inputs are local JSON files. Freezing and reviewer provenance happen before prediction
collection. The scorer preserves unknown strata, missed discoveries and incomplete samples.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from buildlens.domain.source import digest

QUALITY_STRATA = {
    "signature",
    "return_data",
    "predicate_outcome",
    "api",
    "database",
    "configuration_architecture",
}
DIMENSIONS = {"entity", "property", "mechanism", "witness", "test"}
CORPORA = {"buildlens", "full-stack-fastapi-template", "polar"}
PERFORMANCE_MODES = {"entity", "hypothesis", "snapshots", "patch", "proposal"}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("A packet must be a JSON object.")
    return value


def _seal(value: dict[str, Any]) -> str:
    return str(digest({key: item for key, item in value.items() if key != "digest"}))


def _verified(value: dict[str, Any]) -> str:
    calculated = _seal(value)
    if value.get("digest") != calculated:
        raise ValueError("Packet digest mismatch.")
    return calculated


def _counts(expected: set[tuple[str, str]], emitted: set[tuple[str, str]]) -> dict[str, Any]:
    hits = len(expected & emitted)
    return {
        "correct": hits,
        "expected": len(expected),
        "emitted": len(emitted),
        "precision": hits / len(emitted) if emitted else None,
        "recall": hits / len(expected) if expected else None,
    }


def quality_readiness(
    packet: dict[str, Any], labels: dict[str, Any] | None = None
) -> dict[str, Any]:
    packet_id = _verified(packet)
    cases = packet["cases"]
    case_ids = {case["id"] for case in cases}
    if (
        len(cases) != 24
        or len(case_ids) != 24
        or Counter(case["stratum"] for case in cases) != {stratum: 4 for stratum in QUALITY_STRATA}
    ):
        raise ValueError("The quality packet must contain exactly four pairs per stratum.")
    if labels is None:
        return {
            "packet_digest": packet_id,
            "status": "inconclusive",
            "reason": "independent_labels_unavailable",
            "unlabelled_cases": sorted(case_ids),
        }
    label_id = _verified(labels)
    if labels.get("packet_digest") != packet_id:
        raise ValueError("Unbound labels.")
    reviews = {row["case"]: row for row in labels["reviewed_cases"]}
    if len(reviews) != len(labels["reviewed_cases"]) or not reviews.keys() <= case_ids:
        raise ValueError("Invalid or duplicate reviewer provenance.")
    pending = []
    for case in cases:
        review = reviews.get(case["id"], {})
        scope = review.get("scope_entities")
        if scope is not None and (
            not isinstance(scope, list)
            or any(not isinstance(entity, str) or not entity.strip() for entity in scope)
            or len(scope) != len(set(scope))
        ):
            raise ValueError("Invalid canonical entity scope.")
        if (
            not review.get("reviewer")
            or review.get("label_status") == "unavailable"
            or ("scope" in case and scope is None)
        ):
            pending.append(case["id"])
    return {
        "packet_digest": packet_id,
        "label_digest": label_id,
        "status": "inconclusive" if pending else "ready_for_scoring",
        **(
            {"reason": "independent_labels_unavailable", "unlabelled_cases": sorted(pending)}
            if pending
            else {}
        ),
    }


def quality(
    packet: dict[str, Any], labels: dict[str, Any], observations: dict[str, Any]
) -> dict[str, Any]:
    readiness = quality_readiness(packet, labels)
    if readiness["status"] != "ready_for_scoring":
        return readiness
    observed_id = _verified(observations)
    label_id = readiness["label_digest"]
    if observations.get("label_digest") != label_id:
        raise ValueError("Unbound observations.")
    cases = packet["cases"]
    case_ids = {case["id"] for case in cases}
    scopes = {
        row["case"]: frozenset(row["scope_entities"])
        for row in labels["reviewed_cases"]
        if "scope_entities" in row
    }

    def require_scoped_entity(case: str, entity: str) -> None:
        if case not in case_ids or (case in scopes and entity not in scopes[case]):
            raise ValueError("Entity outside the frozen scope.")

    known: dict[str, set[tuple[str, str]]] = {key: set() for key in DIMENSIONS}
    unknown: dict[str, set[tuple[str, str]]] = {key: set() for key in DIMENSIONS}
    forbidden: dict[str, set[tuple[str, str]]] = {key: set() for key in DIMENSIONS}
    units = set()
    for label in labels["units"]:
        unit = (label["case"], label["dimension"], label["claim"])
        if unit in units or unit[0] not in case_ids or unit[1] not in DIMENSIONS:
            raise ValueError("Invalid or duplicate label unit.")
        units.add(unit)
        membership = (unit[0], unit[2])
        if unit[1] == "entity":
            require_scoped_entity(*membership)
        if label["verdict"] == "expected":
            known[unit[1]].add(membership)
        elif label["verdict"] == "unknown":
            unknown[unit[1]].add(membership)
        elif label["verdict"] == "forbidden":
            forbidden[unit[1]].add(membership)
        else:
            raise ValueError("Unknown label verdict.")

    actual: dict[str, set[tuple[str, str]]] = {key: set() for key in DIMENSIONS}
    abstained: dict[str, set[tuple[str, str]]] = {key: set() for key in DIMENSIONS}
    seen = set()
    observed_cases = [row["case"] for row in observations["cases"]]
    if len(observed_cases) != len(case_ids) or set(observed_cases) != case_ids:
        raise ValueError("Observations must cover all cases exactly once.")
    for row in observations["cases"]:
        case = row["case"]
        for finding in row["findings"]:
            key = (case, finding["dimension"], finding["claim"])
            if key in seen or key[1] not in DIMENSIONS:
                raise ValueError("Invalid or duplicate observation.")
            seen.add(key)
            membership = (case, key[2])
            if key[1] == "entity":
                require_scoped_entity(*membership)
            if finding["assertion"] == "definitive":
                actual[key[1]].add(membership)
            elif finding["assertion"] == "uncertain":
                abstained[key[1]].add(membership)
            else:
                raise ValueError("Unknown assertion type.")

    metrics = {
        key: {
            **_counts(known[key], actual[key]),
            "abstentions": len(abstained[key]),
            "abstained_expected": len(abstained[key] & known[key]),
            "labelled_unknowns": len(unknown[key]),
            "labelled_forbidden": len(forbidden[key]),
            "false_definitive_unknowns": len(actual[key] & unknown[key]),
            "false_definitive_forbidden": len(actual[key] & forbidden[key]),
        }
        for key in sorted(DIMENSIONS)
    }
    baselines = {}
    for name in ("file", "symbol"):
        predictions = {(row["case"], row["entity"]) for row in observations["baselines"][name]}
        for case, entity in predictions:
            require_scoped_entity(case, entity)
        baselines[name] = _counts(known["entity"], predictions)

    expected_completion = {case["id"]: case["expected_completion"] for case in cases}
    mismatches = sorted(
        row["case"]
        for row in observations["cases"]
        if row["status"] != expected_completion[row["case"]]
    )
    precision = [metrics["entity"]["precision"], *(b["precision"] for b in baselines.values())]
    recall = [metrics["entity"]["recall"], *(b["recall"] for b in baselines.values())]
    property_scores = [metrics[key]["precision"] for key in ("property", "witness")]
    overclaims = sum(row["false_definitive_unknowns"] for row in metrics.values())
    evaluable = all(value is not None for value in (*precision, *recall, *property_scores))
    passed = (
        evaluable
        and not mismatches
        and overclaims == 0
        and precision[0] > max(precision[1:])
        and recall[0] >= max(recall[1:])
        and min(property_scores) >= 0.90
    )
    independently_reviewed = all(row.get("independent") is True for row in labels["reviewed_cases"])
    return {
        "packet_digest": readiness["packet_digest"],
        "label_digest": label_id,
        "observations_digest": observed_id,
        "models": metrics,
        "baselines": baselines,
        "completion_mismatches": mismatches,
        "status": "inconclusive"
        if not independently_reviewed or not evaluable
        else "passed"
        if passed
        else "failed",
    }


def performance(packet: dict[str, Any], observations: dict[str, Any]) -> dict[str, Any]:
    packet_id, observed_id = _verified(packet), _verified(observations)
    if observations.get("packet_digest") != packet_id:
        raise ValueError("Unbound performance observations.")
    cases = {(row["corpus"], row["mode"]): row for row in packet["cases"]}
    if len(packet["cases"]) != 15 or set(cases) != {
        (corpus, mode) for corpus in CORPORA for mode in PERFORMANCE_MODES
    }:
        raise ValueError("The packet must cover each corpus and mode.")
    actual = {}
    for sample in observations["samples"]:
        key = (sample["corpus"], sample["mode"], sample["state"], sample["repeat"])
        if key in actual:
            raise ValueError("Duplicate performance observation.")
        actual[key] = sample
    expected_keys = {
        (corpus, mode, state, repeat)
        for corpus, mode in cases
        for state in ("cold_application", "warm")
        for repeat in (1, 2)
    }
    if len(actual) != 60 or set(actual) != expected_keys:
        raise ValueError("The 60-observation packet is incomplete or changed.")

    grouped = {
        name: {"samples": 0, "verified_within_two_seconds": 0, "classified_within_three_seconds": 0}
        for name in sorted(CORPORA)
    }
    for (corpus, mode, _, _), sample in actual.items():
        group = grouped[corpus]
        expected = cases[corpus, mode]
        group["samples"] += 1
        completion = (
            sample["status"] == expected["expected_completion"]
            and sample["status"] not in {"deadline", "canceled", "busy"}
            and frozenset(expected["required_findings"]) <= frozenset(sample["findings"])
            and sample["verification"] == "verified"
        )
        wall = max(sample["wall_seconds"], sample["cleanup_completed_seconds"])
        group["verified_within_two_seconds"] += int(completion and wall <= 2)
        group["classified_within_three_seconds"] += int(
            (completion or sample["status"] == "deadline") and wall <= 3
        )
    passed = all(
        row["verified_within_two_seconds"] >= 18 and row["classified_within_three_seconds"] == 20
        for row in grouped.values()
    )
    return {
        "packet_digest": packet_id,
        "observations_digest": observed_id,
        "corpora": grouped,
        "status": "passed" if passed else "failed",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("input", type=Path)
    freeze.add_argument("output", type=Path)
    quality_command = commands.add_parser("quality")
    quality_command.add_argument("packet", type=Path)
    quality_command.add_argument("labels", type=Path, nargs="?")
    quality_command.add_argument("observations", type=Path, nargs="?")
    performance_command = commands.add_parser("performance")
    performance_command.add_argument("packet", type=Path)
    performance_command.add_argument("observations", type=Path)
    args = parser.parse_args()
    if args.action == "freeze":
        payload = _read(args.input)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump({**payload, "digest": _seal(payload)}, output, indent=2, ensure_ascii=False)
            output.write("\n")
        return
    if args.action == "quality":
        packet = _read(args.packet)
        labels = _read(args.labels) if args.labels else None
        result = quality_readiness(packet, labels)
        if labels is not None and result["status"] == "ready_for_scoring" and args.observations:
            result = quality(packet, labels, _read(args.observations))
    else:
        result = performance(_read(args.packet), _read(args.observations))
    print(json.dumps({**result, "digest": _seal(result)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
