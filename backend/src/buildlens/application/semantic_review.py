"""Pre-registered human review of semantic interpretations (§8.5); no LLM judge.

Order is enforced: an approved pre-registration (criteria, rubric and denominators) is locked
first; the stratified evaluation sample and a disjoint pilot are frozen from the catalog
*before* any generation; failed or difficult entities are never replaced. Scoring reports
operational outcomes, semantic quality and the hard gates as separate sections.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

from buildlens.domain.semantic import SemanticEntity
from buildlens.domain.semantic_generation import Interpretation
from buildlens.domain.source import digest

PREREGISTRATION_VERSION = "semantic-preregistration.v2"
SAMPLE_VERSION = "semantic-sample.v1"
PACKET_VERSION = "semantic-review-packet.v3"
REVIEW_VERSION = "semantic-review.v2"
SCORE_VERSION = "semantic-review-score.v3"
RECOGNIZER_PACKET_VERSION = "semantic-recognizer-packet.v1"
RECOGNIZER_SCORE_VERSION = "semantic-recognizer-score.v2"
OUT_OF_SCOPE = "out_of_scope"
GATE_KEYS = ("H1_published_claims_failing_revalidation", "H2_outbound_payloads_outside_plans")
# Outcomes that leave the frozen sample without a model-attributable result. No coverage
# threshold was pre-registered, so any of them makes the overall evaluation inconclusive.
INCOMPLETE = ("provider_failure", "not_attempted", "ineligible")
CORRECTNESS = ("correct", "partially_correct", "incorrect")
PYTHON = "Python"
LANGUAGES = {None, "Python", "TypeScript", "JavaScript"}
CRITERIA = (
    "claim_correctness_min",
    "overclaim_rate_max",
    "omission_rate_max",
    "usefulness_mean_min",
)
OUTCOMES = (
    "accepted",
    "abstained",
    "rejected",
    "refused",
    "provider_failure",
    "not_attempted",
    "ineligible",
)
REVIEWABLE = frozenset({"accepted", "abstained"})
_PROVIDER_FAILURES = {
    "rate_limited",
    "provider_unavailable",
    "timeout",
    "context_rejected",
    "token_bound_violated",
    "outcome_unknown",
    "configuration_invalid",
}

RUBRIC: dict[str, Any] = {
    "scope": (
        "Judge each claim only against the entity's package as shown in the packet (its quoted "
        "source and facts). Do not credit or penalise knowledge from outside the package."
    ),
    "correctness": {
        "correct": "Every assertion in the claim is true of the code and supported by its "
        "quoted evidence; nothing in it is inaccurate, invented or broader than the evidence.",
        "partially_correct": "The central point is supported, but some part is inaccurate, "
        "unsupported, imprecise or broader than the evidence. Counts as NOT correct.",
        "incorrect": "The central assertion is false, unsupported by its quotes, or about "
        "something other than this entity.",
    },
    "overclaim": "True when the claim asserts more than its quotes show (e.g. behaviour, "
    "guarantees, scope or generality not visible in the cited text), independent of the "
    "correctness label.",
    "omission": "Per reviewable entity: true when an important responsibility or behaviour "
    "clearly visible in the package is covered by no published claim. For an abstention (no "
    "claims), true whenever the package shows at least one such responsibility.",
    "usefulness": {
        "1": "Not useful: misleading, trivial or uninformative for understanding the entity.",
        "2": "Somewhat useful: accurate in part but thin, generic or missing key points.",
        "3": "Useful: an accurate, specific account a developer could rely on to orient.",
    },
    "unknowns": "Recorded and shown but not scored.",
    "conflicts": "Claims with status conflicts_with_fact are published next to the fact and are "
    "reviewed and counted like any other published claim.",
}
DENOMINATORS: dict[str, str] = {
    "operational_rates": "N = every entity in the frozen evaluation sample. Each entity has "
    "exactly one final outcome: accepted, abstained, rejected, refused, provider_failure, "
    "not_attempted or ineligible. No entity is replaced or dropped.",
    "claim_correctness": "count(claims labelled correct) / count(published claims of accepted "
    "sample entities). partially_correct and incorrect both count as not correct.",
    "overclaim_rate": "count(claims with overclaim=true) / count(published claims of accepted "
    "sample entities).",
    "omission_rate": "count(reviewable entities with omission=true) / count(reviewable "
    "entities), where reviewable = accepted or abstained.",
    "usefulness_mean": "sum(usefulness) / count(reviewable entities).",
    "undefined": "If a denominator is zero the metric is undefined and its criterion fails.",
    "separation": "Operational outcomes, semantic quality and hard gates are reported "
    "separately; quality never includes failed, refused, rejected or ineligible entities, and "
    "their counts are always shown beside it.",
}


def preregistration_template() -> dict[str, Any]:
    """A draft whose criteria must be agreed and approved before any sample or packet."""
    return {
        "version": PREREGISTRATION_VERSION,
        "status": "draft",
        "approved_by": None,
        "hard_gates": {
            "H1_published_claims_failing_revalidation": 0,
            "H2_outbound_payloads_outside_plans": 0,
        },
        "quality_criteria": {name: None for name in CRITERIA},
        "rubric": RUBRIC,
        "denominators": DENOMINATORS,
        "sample": {
            "size": 60,
            "pilot_size": 6,
            "seed": 13,
            "strata": "half Python, half TS/TSX/JS; round-robin by kind; one per subject group",
            "frozen_before_generation": True,
            "replacement": "none: failed, refused, rejected or difficult entities stay in the "
            "sample and are reported under their outcome",
            "pilot": "disjoint from the sample; operational check only, never scored",
        },
        "provider": {"provider": None, "model": None, "model_changes_during_evaluation": "none"},
        "judging": "Human review only; no LLM judge.",
    }


class ReviewError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def require_approved(preregistration: dict[str, Any]) -> str:
    """The pre-registration must be approved, complete and score-compatible; returns its digest."""
    if preregistration.get("version") != PREREGISTRATION_VERSION:
        raise ReviewError("PREREGISTRATION_VERSION")
    if preregistration.get("status") != "approved" or not preregistration.get("approved_by"):
        raise ReviewError("PREREGISTRATION_NOT_APPROVED")
    criteria = preregistration.get("quality_criteria") or {}
    if set(criteria) != set(CRITERIA) or not all(
        isinstance(criteria[name], (int, float)) for name in CRITERIA
    ):
        raise ReviewError("PREREGISTRATION_INCOMPLETE")
    provider = preregistration.get("provider") or {}
    named = (provider.get("provider"), provider.get("model"))
    if not all(isinstance(value, str) and value for value in named):
        raise ReviewError("PREREGISTRATION_INCOMPLETE")
    if (
        preregistration.get("rubric") != RUBRIC
        or preregistration.get("denominators") != DENOMINATORS
    ):
        raise ReviewError("PREREGISTRATION_RUBRIC_MISMATCH")
    return digest(preregistration)


# ---------------------------------------------------------------- frozen sample and pilot


def _rank(seed: int, key: str) -> str:
    return hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()


def _entry(entity: SemanticEntity) -> dict[str, Any]:
    return {
        "entity_key": entity.entity_key,
        "portable_key": entity.portable_key,
        "kind": entity.kind,
        "language": entity.language or PYTHON,
        "locator": entity.locator,
        "name": entity.name,
        "lines": [entity.start_line, entity.end_line],
        "context_coverage": entity.context_coverage,
        "content_digest": entity.content_digest,
    }


def _draw(
    pool: Sequence[SemanticEntity], size: int, seed: int, used: set[str]
) -> list[SemanticEntity]:
    """Half Python, half TS/TSX/JS; round-robin over kinds; one entity per subject group."""
    groups: dict[str, dict[str, list[SemanticEntity]]] = {
        "python": defaultdict(list),
        "typescript": defaultdict(list),
    }
    for entity in pool:
        language = "python" if entity.language in {None, PYTHON} else "typescript"
        groups[language][entity.kind].append(entity)
    selected: list[SemanticEntity] = []
    for language, share in (("python", size // 2), ("typescript", size - size // 2)):
        kinds = {
            kind: sorted(items, key=lambda item: _rank(seed, item.entity_key))
            for kind, items in groups[language].items()
        }
        taken = 0
        while taken < share and any(kinds.values()):
            for kind in sorted(kinds):
                while kinds[kind] and taken < share:
                    candidate = kinds[kind].pop(0)
                    group = candidate.subject_entity_key or candidate.entity_key
                    if group in used:
                        continue
                    used.add(group)
                    selected.append(candidate)
                    taken += 1
                    break
    return selected


def freeze_sample(
    entities: Sequence[SemanticEntity],
    *,
    size: int,
    pilot_size: int,
    seed: int,
    preregistration_digest: str,
    snapshot_id: str,
    catalog_run_id: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    """Freeze the evaluation sample and a disjoint pilot before any generation."""
    pool = [
        entity
        for entity in sorted(entities, key=lambda item: item.entity_key)
        if entity.context_coverage != "not_eligible"
        and entity.content_digest
        and entity.language in LANGUAGES
    ]
    used: set[str] = set()
    sample = _draw(pool, size, seed, used)
    pilot = _draw(pool, pilot_size, seed + 1, used)
    for drawn, wanted in ((sample, size), (pilot, pilot_size)):
        # The pool must fill every stratum: never freeze a short or unbalanced sample.
        python = sum(1 for item in drawn if item.language in {None, PYTHON})
        if len(drawn) != wanted or python != wanted // 2:
            raise ReviewError("SAMPLE_POOL_INSUFFICIENT")
    population = Counter(
        ("python" if item.language in {None, PYTHON} else "typescript", item.kind) for item in pool
    )
    body = {
        "version": SAMPLE_VERSION,
        "preregistration_digest": preregistration_digest,
        "snapshot_id": snapshot_id,
        "catalog_run_id": catalog_run_id,
        "source": source,
        "seed": seed,
        "population": {
            f"{language}/{kind}": count for (language, kind), count in sorted(population.items())
        },
        "population_rule": "Catalog entities with generation-eligible context (full or outline) "
        "and a content digest in Python, TypeScript or JavaScript files; one entity per subject "
        "group (a role entity and its symbol are never both drawn).",
        "sample": [_entry(item) for item in sample],
        "pilot": [_entry(item) for item in pilot],
    }
    return {**body, "sample_digest": digest(body)}


def verify_sample(sample: dict[str, Any]) -> None:
    body = {key: value for key, value in sample.items() if key != "sample_digest"}
    if sample.get("version") != SAMPLE_VERSION or digest(body) != sample.get("sample_digest"):
        raise ReviewError("SAMPLE_DIGEST_MISMATCH")


# ---------------------------------------------------------------- outcomes and packets


def classify(disposition: str | None, last_outcome: str | None, status: str | None) -> str:
    """One final operational outcome per sampled entity (no entity is ever dropped)."""
    if disposition is None:
        return "not_attempted"
    if disposition == "ineligible":
        return "ineligible"
    if disposition == "reuse" or last_outcome == "reused_concurrently":
        return "abstained" if status == "insufficient_evidence" else "accepted"
    if last_outcome in {"accepted", "accepted_with_conflicts"}:
        return "accepted"
    if last_outcome == "insufficient_evidence":
        return "abstained"
    if last_outcome == "rejected":
        return "rejected"
    if last_outcome == "refused":
        return "refused"
    if last_outcome in _PROVIDER_FAILURES:
        return "provider_failure"
    return "not_attempted"


def failure_cause(last_outcome: str | None, rejection_codes: Sequence[str]) -> str | None:
    """Within the frozen ``provider_failure`` outcome, separate broken configuration (a key,
    permission or model error) from provider-side failures. Rows recorded before the
    ``configuration_invalid`` outcome existed carry it as their first rejection code."""
    if last_outcome not in _PROVIDER_FAILURES:
        return None
    if last_outcome == "configuration_invalid" or "CONFIGURATION_INVALID" in rejection_codes:
        return "configuration_invalid"
    return "provider"


def _claims(interpretation: Interpretation) -> list[dict[str, Any]]:
    return [
        {
            "ordinal": claim.ordinal,
            "category": claim.category,
            "text": claim.text,
            "status": claim.status,
            "basis_label": claim.basis_label,
            "assertion": claim.assertion,
            "assertion_outcome": claim.assertion_outcome,
            "scope_warning": claim.scope_warning,
            "citations": [
                {
                    "id": item.evidence_id,
                    "locator": item.locator,
                    "line": item.start_line,
                    "verification": item.verification,
                    "quote": item.quote,
                }
                for item in claim.citations
            ],
        }
        for claim in interpretation.claims
    ]


def build_packet(
    *,
    sample: dict[str, Any],
    preregistration: dict[str, Any],
    authority: dict[str, str],
    outcomes: dict[str, dict[str, Any]],
    interpretations: dict[str, Interpretation],
) -> dict[str, Any]:
    """``authority`` is the generation run's own scope: snapshot, catalog run, plan, run and
    the plan's provider/model. Each must match the frozen sample and the pre-registration."""
    preregistration_digest = require_approved(preregistration)
    verify_sample(sample)
    if sample["preregistration_digest"] != preregistration_digest:
        raise ReviewError("PREREGISTRATION_MISMATCH")
    if authority["snapshot_id"] != sample["snapshot_id"]:
        raise ReviewError("SAMPLE_SNAPSHOT_MISMATCH")
    if authority["catalog_run_id"] != sample["catalog_run_id"]:
        raise ReviewError("SAMPLE_CATALOG_MISMATCH")
    expected = (preregistration["provider"]["provider"], preregistration["provider"]["model"])
    if (authority["provider"], authority["model"]) != expected:
        raise ReviewError("PROVIDER_MISMATCH")
    if any(item["entity_key"] not in outcomes for item in sample["sample"]):
        raise ReviewError("SAMPLE_NOT_IN_PLAN")
    for value in interpretations.values():
        # Reused interpretations must come from the pre-registered provider and model too.
        if (value.provider, value.requested_model) != expected:
            raise ReviewError("INTERPRETATION_PROVIDER_MISMATCH")
    entities = []
    for item in sample["sample"]:
        key = item["entity_key"]
        outcome = outcomes[key]
        interpretation = interpretations.get(key)
        reviewable = outcome["outcome"] in REVIEWABLE and interpretation is not None
        entities.append(
            {
                **item,
                "outcome": outcome["outcome"],
                "outcome_detail": outcome,
                "reviewable": reviewable,
                "interpretation_id": str(interpretation.id) if interpretation else None,
                "provider": interpretation.provider if interpretation else None,
                "requested_model": interpretation.requested_model if interpretation else None,
                "returned_model": interpretation.returned_model if interpretation else None,
                "status": interpretation.status if interpretation else None,
                "claims": _claims(interpretation) if reviewable and interpretation else [],
                "unknowns": list(interpretation.unknowns) if reviewable and interpretation else [],
            }
        )
    body = {
        "version": PACKET_VERSION,
        "preregistration_digest": preregistration_digest,
        "sample_digest": sample["sample_digest"],
        **{key: authority[key] for key in sorted(authority)},
        "entities": entities,
    }
    return {**body, "packet_digest": digest(body)}


def review_template(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": REVIEW_VERSION,
        "packet_digest": packet["packet_digest"],
        "preregistration_digest": packet["preregistration_digest"],
        "reviewer": None,
        "entities": [
            {
                "entity_key": item["entity_key"],
                "usefulness": None,
                "omission": None,
                "notes": "",
                "claims": [
                    {"ordinal": claim["ordinal"], "correctness": None, "overclaim": None}
                    for claim in item["claims"]
                ],
            }
            for item in packet["entities"]
            if item["reviewable"]
        ],
    }


def render_markdown(packet: dict[str, Any]) -> str:
    counts = Counter(item["outcome"] for item in packet["entities"])
    lines = [
        "# Stage 13 semantic review packet",
        "",
        f"Packet `{packet['packet_digest']}`"
        f" · pre-registration `{packet['preregistration_digest']}`"
        f" · sample `{packet['sample_digest']}` · run `{packet['run_id']}`.",
        "",
        "Outcomes over the frozen sample: "
        + ", ".join(f"{name} {counts.get(name, 0)}" for name in OUTCOMES)
        + ". Only accepted and abstained entities are reviewed;"
        " the rest are reported, never replaced.",
        "",
        "## Rubric (pre-registered)",
        "",
        f"- Scope: {RUBRIC['scope']}",
        *[f"- **{name}**: {text}" for name, text in RUBRIC["correctness"].items()],
        f"- **overclaim**: {RUBRIC['overclaim']}",
        f"- **omission**: {RUBRIC['omission']}",
        *[f"- usefulness **{score}**: {text}" for score, text in RUBRIC["usefulness"].items()],
        "",
    ]
    for index, item in enumerate(packet["entities"], 1):
        lines += [
            f"## {index}. {item['kind']} `{item['name']}` — `{item['locator']}` "
            f"lines {item['lines'][0]}–{item['lines'][1]} · **{item['outcome']}**",
            "",
        ]
        if not item["reviewable"]:
            lines += [f"Not reviewable ({item['outcome_detail']}).", ""]
            continue
        lines += [
            f"{item['provider']}/{item['returned_model'] or item['requested_model']} · "
            f"{item['status']} · context {item['context_coverage']}",
            "",
        ]
        if not item["claims"]:
            lines.append("- (abstention: no claims)")
        for claim in item["claims"]:
            chips = [claim["category"], claim["basis_label"], claim["status"]]
            if claim["assertion_outcome"]:
                chips.append(claim["assertion_outcome"])
            if claim["scope_warning"]:
                chips.append("scope warning")
            lines.append(f"- **Claim {claim['ordinal']}** ({', '.join(chips)}): {claim['text']}")
            for citation in claim["citations"]:
                quote = (citation["quote"] or "").replace("\n", " ⏎ ")
                where = f"{citation['locator']}:{citation['line'] or ''}"
                lines.append(
                    f"  - {citation['id']} {where} — `{quote}`"
                    if citation["quote"]
                    else f"  - {citation['id']} unverified"
                )
        for unknown in item["unknowns"]:
            lines.append(f"- Unknown (not scored): {unknown}")
        lines.append("")
    return "\n".join(lines)


def _rate(numerator: float, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _gates(metrics: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
    """Hard gates are recomputed from their counts and must cover the frozen sample's scope."""
    if (
        metrics.get("snapshot_id") != sample["snapshot_id"]
        or metrics.get("catalog_run_id") != sample["catalog_run_id"]
    ):
        raise ReviewError("GATES_SCOPE_MISMATCH")
    hard = metrics.get("hard_gates") or {}
    counts = {key: hard.get(key) for key in GATE_KEYS}
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in counts.values()
    ):
        raise ReviewError("GATES_INVALID")
    passed = all(value == 0 for value in counts.values())
    if hard.get("passed") is not None and hard["passed"] is not passed:
        raise ReviewError("GATES_INCONSISTENT")
    return {
        **counts,
        "passed": passed,
        "scope": {
            "snapshot_id": sample["snapshot_id"],
            "catalog_run_id": sample["catalog_run_id"],
            "H1": "claims in the snapshot's active published index at scoring time",
            "H2": "every outbound attempt recorded for the snapshot",
        },
    }


def score(
    packet: dict[str, Any],
    reviews: dict[str, Any],
    preregistration: dict[str, Any],
    sample: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """``metrics`` is the snapshot's grounding-metrics report (scope plus ``hard_gates``)."""
    body = {key: value for key, value in packet.items() if key != "packet_digest"}
    if digest(body) != packet.get("packet_digest"):
        raise ReviewError("PACKET_DIGEST_MISMATCH")
    verify_sample(sample)
    if (
        packet.get("version") != PACKET_VERSION
        or packet["sample_digest"] != sample["sample_digest"]
        or [item["entity_key"] for item in packet["entities"]]
        != [item["entity_key"] for item in sample["sample"]]
        or packet["snapshot_id"] != sample["snapshot_id"]
        or packet["catalog_run_id"] != sample["catalog_run_id"]
    ):
        raise ReviewError("SAMPLE_MISMATCH")
    if require_approved(preregistration) != packet["preregistration_digest"]:
        raise ReviewError("PREREGISTRATION_MISMATCH")
    gates = _gates(metrics, sample)
    if (
        reviews.get("version") != REVIEW_VERSION
        or reviews.get("packet_digest") != packet["packet_digest"]
        or reviews.get("preregistration_digest") != packet["preregistration_digest"]
        or not reviews.get("reviewer")
    ):
        raise ReviewError("REVIEW_NOT_BOUND")
    entities = packet["entities"]
    total = len(entities)
    outcomes = Counter(item["outcome"] for item in entities)
    reviewable = {item["entity_key"]: item for item in entities if item["reviewable"]}
    reviewed = {item["entity_key"]: item for item in reviews.get("entities", [])}
    if set(reviewed) != set(reviewable):
        raise ReviewError("REVIEW_INCOMPLETE")
    correctness: Counter[str] = Counter()
    by_language: dict[str, Counter[str]] = defaultdict(Counter)
    overclaims = omissions = usefulness = claims = 0
    for key, item in reviewable.items():
        review = reviewed[key]
        if review.get("usefulness") not in {1, 2, 3} or not isinstance(
            review.get("omission"), bool
        ):
            raise ReviewError("REVIEW_INCOMPLETE")
        rows = {claim["ordinal"]: claim for claim in review.get("claims", [])}
        if set(rows) != {claim["ordinal"] for claim in item["claims"]}:
            raise ReviewError("REVIEW_INCOMPLETE")
        for claim in rows.values():
            if claim.get("correctness") not in CORRECTNESS or not isinstance(
                claim.get("overclaim"), bool
            ):
                raise ReviewError("REVIEW_INCOMPLETE")
            correctness[claim["correctness"]] += 1
            group = "python" if item["language"] == PYTHON else "typescript"
            by_language[group][claim["correctness"]] += 1
            overclaims += claim["overclaim"]
            claims += 1
        omissions += review["omission"]
        usefulness += review["usefulness"]
    measured = {
        "claim_correctness": _rate(correctness["correct"], claims),
        "claim_partially_correct_rate": _rate(correctness["partially_correct"], claims),
        "claim_incorrect_rate": _rate(correctness["incorrect"], claims),
        "overclaim_rate": _rate(overclaims, claims),
        "omission_rate": _rate(omissions, len(reviewable)),
        "usefulness_mean": _rate(usefulness, len(reviewable)),
    }
    criteria = preregistration["quality_criteria"]

    def meets(value: float | None, bound: float, minimum: bool) -> bool:
        return value is not None and (value >= bound if minimum else value <= bound)

    checks = {
        "claim_correctness_min": meets(
            measured["claim_correctness"], criteria["claim_correctness_min"], True
        ),
        "overclaim_rate_max": meets(
            measured["overclaim_rate"], criteria["overclaim_rate_max"], False
        ),
        "omission_rate_max": meets(measured["omission_rate"], criteria["omission_rate_max"], False),
        "usefulness_mean_min": meets(
            measured["usefulness_mean"], criteria["usefulness_mean_min"], True
        ),
    }
    quality_passed = all(checks.values())
    incomplete = {name: outcomes[name] for name in INCOMPLETE if outcomes.get(name)}
    if not gates["passed"]:
        overall = "failed"
    elif incomplete:
        overall = "inconclusive"
    else:
        overall = "passed" if quality_passed else "failed"
    causes = Counter(
        item["outcome_detail"].get("failure_cause") or "provider"
        for item in entities
        if item["outcome"] == "provider_failure"
    )
    return {
        "version": SCORE_VERSION,
        "packet_digest": packet["packet_digest"],
        "preregistration_digest": packet["preregistration_digest"],
        "sample_digest": packet["sample_digest"],
        "run_id": packet["run_id"],
        "reviewer": reviews["reviewer"],
        "operational": {
            "sample_size": total,
            "counts": {name: outcomes.get(name, 0) for name in OUTCOMES},
            "rates": {name: _rate(outcomes.get(name, 0), total) for name in OUTCOMES},
            "provider_failure_causes": {
                name: causes.get(name, 0) for name in ("provider", "configuration_invalid")
            },
        },
        "evaluation": {
            "complete": not incomplete,
            "incomplete_outcomes": incomplete,
            "reviewable_entities": len(reviewable),
            "rule": "Complete only when every sampled entity reached a model-attributable "
            "outcome (accepted, abstained, rejected or refused). No coverage threshold was "
            "pre-registered, so any provider failure, unattempted or ineligible entity makes "
            "the overall result inconclusive; quality is then conditional on the reviewed subset.",
        },
        "quality": {
            "reviewable_entities": len(reviewable),
            "published_claims": claims,
            "correctness_counts": {name: correctness.get(name, 0) for name in CORRECTNESS},
            "by_language": {key: dict(value) for key, value in sorted(by_language.items())},
            "measured": measured,
            "criteria": criteria,
            "criteria_passed": checks,
            "passed": quality_passed,
        },
        "hard_gates": gates,
        "overall": overall,
        "passed": overall == "passed",
    }


# ---------------------------------------------------------------- recognizer ground truth


def recognizer_packet(
    entities: list[SemanticEntity], role_facts: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Every recognizer match plus heuristic candidate misses, for human ground truth."""
    matches = sorted(
        (
            {
                "entity_key": item.entity_key,
                "subject_entity_key": item.subject_entity_key,
                "locator": item.locator,
                "name": item.name,
                "role": item.kind,
                "recognizer": role_facts.get(item.entity_key, {}).get("recognizer"),
                "strength": role_facts.get(item.entity_key, {}).get("strength"),
                "met": role_facts.get(item.entity_key, {}).get("met"),
                "unmet": role_facts.get(item.entity_key, {}).get("unmet"),
                "is_correct": None,
            }
            for item in entities
            if item.kind in {"api_endpoint", "db_table", "component", "hook"}
        ),
        key=lambda value: (value["locator"], value["name"], value["entity_key"]),
    )
    subjects = {value["subject_entity_key"] for value in matches}
    candidates = sorted(
        (
            {
                "entity_key": item.entity_key,
                "locator": item.locator,
                "name": item.name,
                "reason": reason,
                "is_missed_match": None,
            }
            for item in entities
            if item.kind == "symbol" and item.entity_key not in subjects
            for reason in [_candidate_reason(item)]
            if reason
        ),
        key=lambda value: (value["locator"], value["name"]),
    )
    body = {"version": RECOGNIZER_PACKET_VERSION, "matches": matches, "candidates": candidates}
    return {**body, "packet_digest": digest(body)}


def _candidate_reason(item: SemanticEntity) -> str | None:
    name = item.name
    if item.language in {"TypeScript", "JavaScript"}:
        if (
            item.locator.endswith((".tsx", ".jsx"))
            and name[:1].isupper()
            and item.symbol_kind == "function"
        ):
            return "PascalCase function in a JSX file"
        if name.startswith("use") and name[3:4].isupper():
            return "use-prefixed function"
    elif item.symbol_kind == "class" and "/models" in f"/{item.locator}":
        return "class in a models module"
    elif "/routes" in f"/{item.locator}" or item.locator.endswith("_routes.py"):
        if item.symbol_kind in {"function", "method"}:
            return "function in a routes module"
    return None


def recognizer_score(packet: dict[str, Any], reviews: dict[str, Any]) -> dict[str, Any]:
    """A missed match counts only once the reviewer attributes it to the recognizer that should
    have matched it, or explicitly to ``out_of_scope`` (a role no recognizer covers)."""
    from buildlens.application.recognizers import RECOGNIZERS

    body = {key: value for key, value in packet.items() if key != "packet_digest"}
    if (
        digest(body) != packet.get("packet_digest")
        or reviews.get("packet_digest") != packet["packet_digest"]
    ):
        raise ReviewError("PACKET_DIGEST_MISMATCH")
    marks = {item["entity_key"]: item for item in reviews.get("matches", [])}
    misses = {item["entity_key"]: item for item in reviews.get("candidates", [])}
    per: dict[str, Counter[str]] = defaultdict(Counter)
    for match in packet["matches"]:
        verdict = marks.get(match["entity_key"], {}).get("is_correct")
        if not isinstance(verdict, bool):
            raise ReviewError("REVIEW_INCOMPLETE")
        per[match["recognizer"] or "unknown"]["true_positive" if verdict else "false_positive"] += 1
    out_of_scope = 0
    for candidate in packet["candidates"]:
        mark = misses.get(candidate["entity_key"], {})
        if not isinstance(mark.get("is_missed_match"), bool):
            raise ReviewError("REVIEW_INCOMPLETE")
        if not mark["is_missed_match"]:
            continue
        attributed = mark.get("recognizer")
        if attributed == OUT_OF_SCOPE:
            out_of_scope += 1
        elif attributed in RECOGNIZERS:
            per[attributed]["false_negative"] += 1
        else:
            raise ReviewError("MISS_UNATTRIBUTED")
    results = {}
    for name, counts in sorted(per.items()):
        tp, fp, fn = counts["true_positive"], counts["false_positive"], counts["false_negative"]
        results[name] = {
            **counts,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall_against_reviewed_candidates": round(tp / (tp + fn), 4) if tp + fn else None,
        }
    return {
        "version": RECOGNIZER_SCORE_VERSION,
        "packet_digest": packet["packet_digest"],
        "recognizers": results,
        "out_of_scope_misses": out_of_scope,
        "recall_note": "Recall is over heuristic candidates a human reviewed, not over every "
        "role in the repository; misses outside the candidate list are not counted.",
    }
