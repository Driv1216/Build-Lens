"""Stage 15 evaluation mechanics, separate from production inference and AI explanations.

Inputs are digest-bound, source-only packets, frozen labels and sealed observations. This
module never generates labels. AI-assisted review is explicitly distinguished from independent
human gold; the two may not be combined into an independent-validation claim.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from buildlens.application.architecture_evaluation import metric
from buildlens.domain.source import digest

VERSION = "flow-evaluation.v1"
PACKET_VERSION = "flow-evaluation-packet.v1"
LABELS_VERSION = "flow-evaluation-labels.v1"
SYSTEM_VERSION = "flow-evaluation-observations.v1"
SCORE_VERSION = "flow-evaluation-score.v1"
FACETS = ("path", "tiers", "guards", "responses", "continuations", "async", "effects", "unknowns")
METRICS = {
    "precision": "Exact target-set matches / all emitted local-target sets; wrong targets count.",
    "recall": "Exact target-set matches / all source-labelled determinable local calls.",
    "coverage": "Independently enumerated sampled callsites extracted / all sampled callsites.",
    "unknown": "Indeterminable labels remain in the precision and uncertainty denominators.",
    "path": "LCS of scope visits in traversal order; exact match requires all eight facets "
    "to agree and every forbidden target to be absent. Bounded model walk, not execution.",
    "empty": "Zero denominators are undefined, never perfect; empty strata are inconclusive.",
    "independence": "AI-assisted labels are exploratory even when owner-authorized. They "
    "never satisfy the original independent human gold requirement.",
}
THRESHOLDS = {
    "precision_wilson_lower_min": 0.90,
    "recall_min": 0.70,
    "whole_path_min": 0.80,
    "guard_precision_min": 0.90,
    "guard_recall_min": 0.80,
    "overclaim_max": 0,
    "query_p95_upper_seconds_max": 2.0,
}


class FlowEvaluationError(ValueError):
    pass


def bound(value: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the entire record, excluding only its own digest field."""
    body = {k: v for k, v in value.items() if k != "digest"}
    return {**body, "digest": str(digest(body))}


def verified(value: Mapping[str, Any], version: str) -> None:
    if value.get("version") != version or value.get("digest") != bound(value)["digest"]:
        raise FlowEvaluationError("RECORD_DIGEST_OR_VERSION")


def preregistration(corpora: Sequence[dict[str, Any]], authorization: str) -> dict[str, Any]:
    """Freeze before observing the new evaluation runs. No invented human approver."""
    return bound(
        {
            "version": VERSION,
            "status": "frozen",
            "authorization": authorization,
            "label_basis": "owner-authorized AI-assisted source review; not independent human gold",
            "historical_exposure": "The implementing agent has seen A–E fixtures and measurements. "
            "Existing corpora are not claimed to be untouched holdouts.",
            "metrics": METRICS,
            "thresholds": THRESHOLDS,
            "corpora": list(corpora),
            "sampling": {
                "seed": 1512,
                "calls_per_language_per_corpus": 50,
                "strata": "corpus x Python/TypeScript x direct/attribute/dynamic; "
                "round-robin allocation over source-form classes, then seeded shuffle",
                "precision_gate": "Per-language unweighted accuracy of the stated balanced "
                "sample mixture, not an estimate of corpus prevalence; minimum 150 sampled "
                "calls per language. Resolution classes are diagnostic strata.",
                "power": "Exact binomial power against p=0.97 at n=150, rejecting p<=0.90 "
                "when Wilson 95% lower bound >=0.90; reported by power_calculation().",
            },
            "performance": {
                "seed": 151207,
                "inputs_per_class": 20,
                "samples": 59,
                "warmups": 5,
                "classes": ["forward", "backward", "path", "entry_search"],
                "workloads": ["cold_application", "warm"],
                "cold": "Evict flow graph cache before each sample; DB buffers uncontrolled.",
                "drift": "abs(Spearman latency vs order)>0.30 or split-half median "
                "ratio>1.25 => not stationary, no latency gate.",
                "search_mode": "lexical",
            },
            "provider": "S6 real-model quality deferred; loopback MockTransport hard gates only. "
            "No real-provider requests are authorized by this protocol.",
            "decision": "A failed quality gate is retained as failed; missing evidence is "
            "inconclusive. Finishing the evaluation does not imply passing quality gates.",
        }
    )


def require_preregistration(value: Mapping[str, Any]) -> None:
    verified(value, VERSION)
    if (
        value.get("status") != "frozen"
        or not value.get("authorization")
        or value.get("metrics") != METRICS
        or value.get("thresholds") != THRESHOLDS
        or not value.get("corpora")
    ):
        raise FlowEvaluationError("PREREGISTRATION_INVALID")


def power_calculation(n: int = 150, alternative: float = 0.97) -> dict[str, Any]:
    critical = next(
        k
        for k in range(n + 1)
        if metric(k, n)["wilson95"][0] >= THRESHOLDS["precision_wilson_lower_min"]
    )

    def tail(p: float) -> float:
        return sum(math.comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(critical, n + 1))

    return {
        "n": n,
        "alternative": alternative,
        "critical_successes": critical,
        "power": tail(alternative),
        "null_upper_tail": tail(0.90),
    }


def _unique(items: Sequence[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    result = {row["id"]: row for row in items}
    if len(result) != len(items):
        raise FlowEvaluationError(f"DUPLICATE_{kind}")
    return result


def validate_labels(packet: Mapping[str, Any], labels: Mapping[str, Any]) -> None:
    verified(packet, PACKET_VERSION)
    verified(labels, LABELS_VERSION)
    if labels.get("packet_digest") != packet["digest"]:
        raise FlowEvaluationError("LABEL_PACKET_MISMATCH")
    if labels.get("basis") not in {"ai_assisted", "independent_human"} or not labels.get(
        "reviewer"
    ):
        raise FlowEvaluationError("LABEL_PROVENANCE")
    for kind in ("calls", "cases", "queries"):
        expected = _unique(packet.get(kind, []), kind)
        actual = _unique(labels.get(kind, []), kind)
        if expected.keys() != actual.keys():
            raise FlowEvaluationError(f"LABELS_INCOMPLETE_{kind}")
        for row in actual.values():
            if row.get("reviewed") is not True or not row.get("reason"):
                raise FlowEvaluationError("LABEL_UNREVIEWED")
    for row in labels.get("calls", []):
        state, targets = row.get("state"), row.get("targets")
        if (
            state not in {"internal", "external", "unknown"}
            or not isinstance(targets, list)
            or len(targets) != len(set(targets))
            or (state == "internal") != bool(targets)
            or any(not isinstance(t, str) or not t for t in targets)
        ):
            raise FlowEvaluationError("CALL_LABEL_INVALID")
    for row in labels.get("cases", []):
        if (
            set(row.get("expected", {})) != set(FACETS)
            or any(not isinstance(row["expected"][f], list) for f in FACETS)
            or not isinstance(row.get("forbidden"), list)
            or not row["expected"]["path"]
        ):
            raise FlowEvaluationError("CASE_LABEL_INVALID")
    for row in labels.get("queries", []):
        if not isinstance(row.get("targets"), list) or not row["targets"]:
            raise FlowEvaluationError("QUERY_LABEL_INVALID")


def lcs_length(left: Sequence[Any], right: Sequence[Any]) -> int:
    """Linear-space LCS; duplicates count, never collapse paths to sets."""
    previous = [0] * (len(right) + 1)
    for item in left:
        current = [0]
        for j, other in enumerate(right, 1):
            current.append(previous[j - 1] + 1 if item == other else max(current[-1], previous[j]))
        previous = current
    return previous[-1]


def _sets(expected: Sequence[Any], actual: Sequence[Any]) -> dict[str, Any]:
    e, a = {str(digest(x)) for x in expected}, {str(digest(x)) for x in actual}
    return {
        "precision": metric(len(e & a), len(a)),
        "recall": metric(len(e & a), len(e)),
        "exact": e == a,
        "expected": len(e),
        "actual": len(a),
    }


def score(
    prereg: Mapping[str, Any],
    packet: Mapping[str, Any],
    labels: Mapping[str, Any],
    system: Mapping[str, Any],
) -> dict[str, Any]:
    require_preregistration(prereg)
    validate_labels(packet, labels)
    verified(system, SYSTEM_VERSION)
    if (
        packet.get("preregistration_digest") != prereg["digest"]
        or system.get("packet_digest") != packet["digest"]
        or system.get("labels_digest") != labels["digest"]
    ):
        raise FlowEvaluationError("EVALUATION_BINDING")
    observations = _unique(system.get("calls", []), "system_calls")
    sampled = _unique(packet.get("calls", []), "packet_calls")
    if observations.keys() != sampled.keys():
        raise FlowEvaluationError("SYSTEM_CALL_MEMBERSHIP")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    units = []
    for label in labels.get("calls", []):
        predicted = observations[label["id"]]
        targets = predicted.get("targets", [])
        correct = (
            bool(targets) and label["state"] == "internal" and set(targets) == set(label["targets"])
        )
        row = {
            "id": label["id"],
            "correct": correct,
            "emitted": bool(targets),
            "positive": label["state"] == "internal",
            "extracted": predicted.get("extracted") is True,
            "overclaim": bool(targets) and label["state"] == "unknown",
            "label": label,
            "system": predicted,
        }
        units.append(row)
        unit = sampled[label["id"]]
        groups[unit["language"]].append(row)
        groups[f"{unit['corpus']}:{unit['language']}:{unit['form']}"].append(row)

    def aggregate(rows: list[dict[str, Any]], gate: bool) -> dict[str, Any]:
        correct = sum(r["correct"] for r in rows)
        precision = metric(correct, sum(r["emitted"] for r in rows))
        recall = metric(correct, sum(r["positive"] for r in rows))
        checks = {
            "precision": bool(precision["wilson95"] and precision["wilson95"][0] >= 0.90),
            "recall": recall["value"] is not None and recall["value"] >= 0.70,
            "overclaim": not any(r["overclaim"] for r in rows),
        }
        status = "passed" if all(checks.values()) else "failed"
        if (
            not rows
            or (gate and len(rows) < 150)
            or precision["value"] is None
            or recall["value"] is None
        ):
            status = "inconclusive"
        if any(r["overclaim"] for r in rows):
            status = "failed"
        return {
            "n": len(rows),
            "precision": precision,
            "recall": recall,
            "coverage": metric(sum(r["extracted"] for r in rows), len(rows)),
            "overclaims": sum(r["overclaim"] for r in rows),
            "criteria": checks,
            "status": status if gate else "diagnostic",
        }

    call_scores = {
        key: aggregate(rows, key in {"python", "typescript"})
        for key, rows in sorted(groups.items())
    }
    path_observations = _unique(system.get("cases", []), "system_cases")
    if path_observations.keys() != _unique(packet.get("cases", []), "cases").keys():
        raise FlowEvaluationError("SYSTEM_CASE_MEMBERSHIP")
    cases = []
    for label in labels.get("cases", []):
        actual = path_observations[label["id"]]
        expected = label["expected"]
        facets = {f: _sets(expected[f], actual.get(f, [])) for f in FACETS if f != "path"}
        aligned = lcs_length(expected["path"], actual.get("path", []))
        forbidden = sorted(set(label["forbidden"]) & set(actual.get("path", [])))
        exact = (
            expected["path"] == actual.get("path")
            and all(v["exact"] for v in facets.values())
            and not forbidden
        )
        cases.append(
            {
                "id": label["id"],
                "exact": exact,
                "facets": facets,
                "hop_precision": metric(aligned, len(actual.get("path", []))),
                "hop_recall": metric(aligned, len(expected["path"])),
                "forbidden_emissions": forbidden,
                "system_error": actual.get("error"),
            }
        )
    query_system = _unique(system.get("queries", []), "system_queries")
    if query_system.keys() != _unique(packet.get("queries", []), "queries").keys():
        raise FlowEvaluationError("SYSTEM_QUERY_MEMBERSHIP")
    ranks = []
    for label in labels.get("queries", []):
        ranked = query_system[label["id"]].get("targets", [])
        ranks.append(
            next((i for i, target in enumerate(ranked, 1) if target in label["targets"]), None)
        )
    languages = [
        call_scores.get(k, {"status": "inconclusive"})["status"] for k in ("python", "typescript")
    ]
    exact_metric = metric(sum(c["exact"] for c in cases), len(cases))
    facet_totals = {}
    for facet in FACETS:
        if facet == "path":
            continue
        comparisons = [case["facets"][facet] for case in cases]
        precision = metric(
            sum(c["precision"]["numerator"] for c in comparisons),
            sum(c["precision"]["denominator"] for c in comparisons),
        )
        recall = metric(
            sum(c["recall"]["numerator"] for c in comparisons),
            sum(c["recall"]["denominator"] for c in comparisons),
        )
        facet_totals[facet] = {"precision": precision, "recall": recall}
    guards = facet_totals["guards"]
    guard_status = (
        "inconclusive"
        if guards["precision"]["value"] is None or guards["recall"]["value"] is None
        else "passed"
        if guards["precision"]["value"] >= THRESHOLDS["guard_precision_min"]
        and guards["recall"]["value"] >= THRESHOLDS["guard_recall_min"]
        else "failed"
    )
    status = "failed" if "failed" in languages else "passed"
    if "inconclusive" in languages or not cases or not ranks:
        status = "inconclusive" if status != "failed" else status
    if cases and (exact_metric["value"] or 0) < THRESHOLDS["whole_path_min"]:
        status = "failed"
    return bound(
        {
            "version": SCORE_VERSION,
            "preregistration_digest": prereg["digest"],
            "packet_digest": packet["digest"],
            "labels_digest": labels["digest"],
            "observations_digest": system["digest"],
            "basis": labels["basis"],
            "independent_human_validation": labels["basis"] == "independent_human",
            "acceptance_status": "not_established",
            "status": status,
            "S0": {
                "whole_path_exact": exact_metric,
                "cases": cases,
                "facets": facet_totals,
                "guard_status": guard_status,
                "scope": labels.get("S0_original_gold", "source-labelled cases"),
                "original_gold_suite_complete": labels.get("gold_suite_complete") is True,
            },
            "S1": {
                "n": len(ranks),
                "recall_at_5": metric(sum(r is not None and r <= 5 for r in ranks), len(ranks)),
                "MRR": sum(1 / r for r in ranks if r) / len(ranks) if ranks else None,
            },
            "S3": call_scores,
            "units": units,
        }
    )


def latency_summary(
    values: Sequence[float], *, distinct_inputs: int, threshold: float = 2.0
) -> dict[str, Any]:
    """Order-statistic p95 bound, plus preregistered diagnostic drift checks."""
    if any(not math.isfinite(x) or x < 0 for x in values):
        raise FlowEvaluationError("INVALID_LATENCY")
    n = len(values)
    if not n:
        return {"n": 0, "status": "inconclusive"}
    ordered = sorted(values)
    ranks = {
        v: statistics.mean(i + 1 for i, x in enumerate(ordered) if x == v) for v in set(values)
    }
    ranked = [ranks[v] for v in values]
    rho = statistics.correlation(range(n), ranked) if n > 1 and len(set(values)) > 1 else 0.0
    half = n // 2
    a, b = (
        (statistics.median(values[:half]), statistics.median(values[half:]))
        if half
        else (values[0], values[0])
    )
    ratio = max(a, b) / min(a, b) if min(a, b) else (1.0 if a == b else math.inf)
    stationary = abs(rho) <= 0.30 and ratio <= 1.25
    enough = n >= 59 and distinct_inputs >= 20
    return {
        "n": n,
        "distinct_inputs": distinct_inputs,
        "median": statistics.median(values),
        "p95": ordered[math.ceil(0.95 * n) - 1],
        "sample_max": max(values),
        "p95_upper95": max(values) if enough and stationary else None,
        "confidence": 1 - 0.95**n if enough and stationary else None,
        "spearman": rho,
        "split_half_median_ratio": ratio if math.isfinite(ratio) else None,
        "stationary": stationary,
        "status": (
            "inconclusive"
            if not enough
            else "not_stationary"
            if not stationary
            else "passed"
            if max(values) <= threshold
            else "failed"
        ),
    }
