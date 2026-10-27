"""Stage 13 Milestone D review repairs: evaluation, gate, sample and recognizer integrity."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from buildlens.application import recognizers
from buildlens.application.recognizers import RECOGNIZERS
from buildlens.application.semantic_review import (
    GATE_KEYS,
    OUT_OF_SCOPE,
    ReviewError,
    build_packet,
    classify,
    failure_cause,
    freeze_sample,
    preregistration_template,
    recognizer_packet,
    recognizer_score,
    require_approved,
    review_template,
    score,
)
from buildlens.domain.semantic import SemanticEntity
from buildlens.domain.semantic_generation import Interpretation
from buildlens.domain.source import digest

SNAPSHOT, CATALOG = str(uuid4()), str(uuid4())


def _prereg() -> dict[str, Any]:
    value = preregistration_template()
    value["quality_criteria"] = {
        "claim_correctness_min": 0.9,
        "overclaim_rate_max": 0.05,
        "omission_rate_max": 0.25,
        "usefulness_mean_min": 2.4,
    }
    value["provider"].update(provider="gemini", model="gemini-3.8-flash")
    value["status"], value["approved_by"] = "approved", "test"
    return value


def _sample(size: int, prereg: dict[str, Any]) -> dict[str, Any]:
    body = {
        "version": "semantic-sample.v1",
        "preregistration_digest": require_approved(prereg),
        "snapshot_id": SNAPSHOT,
        "catalog_run_id": CATALOG,
        "sample": [
            {
                "entity_key": f"symbol:{n}",
                "language": "Python" if n % 2 else "TypeScript",
                "kind": "symbol",
                "name": f"e{n}",
                "locator": "a.py",
                "lines": [1, 2],
                "context_coverage": "full",
            }
            for n in range(size)
        ],  # fmt: skip
        "pilot": [],
    }
    return {**body, "sample_digest": digest(body)}


def _interpretation(
    provider: str = "gemini", model: str = "gemini-3.8-flash", claims: int = 2
) -> Interpretation:
    return cast(
        Interpretation,
        SimpleNamespace(
            id=uuid4(), provider=provider, requested_model=model, returned_model=model,
            status="accepted", unknowns=(),
            claims=[
                SimpleNamespace(ordinal=n, category="purpose", text="t", status="cited",
                                basis_label="code", assertion=None, assertion_outcome=None,
                                scope_warning=False, citations=[])
                for n in range(1, claims + 1)
            ],
        ),
    )  # fmt: skip


def _authority(**values: str) -> dict[str, str]:
    return {"snapshot_id": SNAPSHOT, "catalog_run_id": CATALOG, "plan_id": "p", "run_id": "r",
            "provider": "gemini", "model": "gemini-3.8-flash", **values}  # fmt: skip


def _packet(
    sample: dict[str, Any], prereg: dict[str, Any], outcomes: list[str], **authority: str
) -> dict[str, Any]:
    keys = [item["entity_key"] for item in sample["sample"]]
    details = {
        key: {"outcome": outcome, "failure_cause": "provider" if outcome == "provider_failure"
              else None}
        for key, outcome in zip(keys, outcomes, strict=True)
    }  # fmt: skip
    interpretations = {
        key: _interpretation()
        for key, outcome in zip(keys, outcomes, strict=True)
        if outcome == "accepted"
    }
    return build_packet(
        sample=sample, preregistration=prereg, authority=_authority(**authority),
        outcomes=details, interpretations=interpretations,
    )  # fmt: skip


def _reviews(packet: dict[str, Any], correctness: str = "correct") -> dict[str, Any]:
    reviews = review_template(packet)
    reviews["reviewer"] = "test reviewer"
    for entity in reviews["entities"]:
        entity["usefulness"], entity["omission"] = 3, False
        for claim in entity["claims"]:
            claim["correctness"], claim["overclaim"] = correctness, False
    return reviews


def _metrics(h1: Any = 0, h2: Any = 0, passed: Any = True, **scope: str) -> dict[str, Any]:
    return {
        "snapshot_id": scope.get("snapshot_id", SNAPSHOT),
        "catalog_run_id": scope.get("catalog_run_id", CATALOG),
        "hard_gates": {GATE_KEYS[0]: h1, GATE_KEYS[1]: h2, "passed": passed},
    }


# ---------------------------------------------------------------- F1 overall outcome


def test_one_accepted_of_sixty_is_inconclusive_not_passed() -> None:
    prereg = _prereg()
    sample = _sample(60, prereg)
    packet = _packet(sample, prereg, ["accepted"] + ["provider_failure"] * 59)
    result = score(packet, _reviews(packet), prereg, sample, _metrics())
    assert result["quality"]["passed"]  # the one reviewed output is perfect...
    assert result["overall"] == "inconclusive" and not result["passed"]  # ...but that is not a pass
    assert result["evaluation"] == {
        **result["evaluation"],
        "complete": False,
        "incomplete_outcomes": {"provider_failure": 59},
        "reviewable_entities": 1,
    }
    assert result["operational"]["provider_failure_causes"] == {
        "provider": 59,
        "configuration_invalid": 0,
    }


def test_complete_evaluation_passes_or_fails_on_quality() -> None:
    prereg = _prereg()
    sample = _sample(60, prereg)
    packet = _packet(sample, prereg, ["accepted"] * 60)
    passed = score(packet, _reviews(packet), prereg, sample, _metrics())
    assert passed["evaluation"]["complete"] and passed["overall"] == "passed" and passed["passed"]
    failed = score(packet, _reviews(packet, "partially_correct"), prereg, sample, _metrics())
    assert failed["overall"] == "failed" and not failed["passed"]
    # Unattempted entities also leave the evaluation inconclusive.
    partial = _packet(sample, prereg, ["accepted"] * 59 + ["not_attempted"])
    assert score(partial, _reviews(partial), prereg, sample, _metrics())["overall"] == (
        "inconclusive"
    )


# ---------------------------------------------------------------- F5 hard gates


def test_hard_gates_are_recomputed_and_bound_to_the_sample_scope() -> None:
    prereg = _prereg()
    sample = _sample(4, prereg)
    packet = _packet(sample, prereg, ["accepted"] * 4)
    reviews = _reviews(packet)
    with pytest.raises(ReviewError, match="GATES_INCONSISTENT"):
        score(packet, reviews, prereg, sample, _metrics(h1=5, h2=2, passed=True))
    tripped = score(packet, reviews, prereg, sample, _metrics(h1=1, passed=False))
    assert tripped["overall"] == "failed" and not tripped["hard_gates"]["passed"]
    for bad in (None, True, "0", -1, 0.0):
        with pytest.raises(ReviewError, match="GATES_INVALID"):
            score(packet, reviews, prereg, sample, _metrics(h1=bad, passed=None))
    with pytest.raises(ReviewError, match="GATES_SCOPE_MISMATCH"):
        score(packet, reviews, prereg, sample, _metrics(snapshot_id=str(uuid4())))
    with pytest.raises(ReviewError, match="GATES_SCOPE_MISMATCH"):
        score(packet, reviews, prereg, sample, _metrics(catalog_run_id=str(uuid4())))
    scope = score(packet, reviews, prereg, sample, _metrics())["hard_gates"]["scope"]
    assert scope["snapshot_id"] == SNAPSHOT and "active published index" in scope["H1"]


# ---------------------------------------------------------------- F2 packet authority


def test_packet_refuses_a_run_outside_the_preregistered_scope() -> None:
    prereg = _prereg()
    sample = _sample(4, prereg)
    outcomes = ["accepted"] * 4
    for authority, code in (
        ({"catalog_run_id": str(uuid4())}, "SAMPLE_CATALOG_MISMATCH"),
        ({"snapshot_id": str(uuid4())}, "SAMPLE_SNAPSHOT_MISMATCH"),
        ({"model": "gemini-other"}, "PROVIDER_MISMATCH"),
        ({"provider": "openai"}, "PROVIDER_MISMATCH"),
    ):
        with pytest.raises(ReviewError, match=code):
            _packet(sample, prereg, outcomes, **authority)
    keys = [item["entity_key"] for item in sample["sample"]]
    accepted = {"outcome": "accepted"}
    with pytest.raises(ReviewError, match="SAMPLE_NOT_IN_PLAN"):
        build_packet(sample=sample, preregistration=prereg, authority=_authority(),
                     outcomes={key: accepted for key in keys[1:]}, interpretations={})  # fmt: skip
    # A reused interpretation from another model never enters the packet.
    with pytest.raises(ReviewError, match="INTERPRETATION_PROVIDER_MISMATCH"):
        build_packet(sample=sample, preregistration=prereg, authority=_authority(),
                     outcomes={key: accepted for key in keys},
                     interpretations={keys[0]: _interpretation(model="other")})  # fmt: skip
    unnamed = _prereg()
    unnamed["provider"]["model"] = None
    with pytest.raises(ReviewError, match="PREREGISTRATION_INCOMPLETE"):
        require_approved(unnamed)
    packet = _packet(sample, prereg, outcomes)
    assert (packet["catalog_run_id"], packet["model"], packet["run_id"]) == (
        CATALOG, "gemini-3.8-flash", "r",
    )  # fmt: skip


def test_frozen_preregistration_still_validates() -> None:
    root = Path(__file__).parents[3] / ".buildlens-eval/reviews/semantic-evaluation-20261025"
    if not (root / "preregistration.json").exists():
        pytest.skip("frozen evaluation evidence is local (git-ignored)")
    import json

    frozen = json.loads((root / "preregistration.json").read_text())
    assert (
        require_approved(frozen)
        == json.loads((root / "sample.json").read_text())["preregistration_digest"]
    )


# ---------------------------------------------------------------- F3 sample sizes


def _entity(n: int, language: str, kind: str = "symbol", subject: str | None = None) -> Any:
    return SemanticEntity(
        entity_key=f"{kind}:{language}:{n}", kind=cast(Any, kind), language=language,
        locator=f"f{n}", artifact_identity_id=None, symbol_identity_id=None,
        subject_entity_key=subject, name=f"n{n}", qualified_name=None, symbol_kind="function",
        start_line=1, end_line=2, ordinal=n, portable_key=f"p{n}",
        stored_coverage=cast(Any, "complete"), coverage_reason=None,
        context_coverage=cast(Any, "full"), context_reason=None, package_digest="d",
        package_bytes=1, content_digest=f"c{n}",
    )  # fmt: skip


def _freeze(entities: list[Any], size: int = 4, pilot: int = 2) -> dict[str, Any]:
    return freeze_sample(entities, size=size, pilot_size=pilot, seed=13,
                         preregistration_digest="x", snapshot_id=SNAPSHOT,
                         catalog_run_id=CATALOG, source={})  # fmt: skip


def test_sampler_refuses_short_or_unbalanced_pools() -> None:
    balanced = [_entity(n, "Python") for n in range(3)] + [
        _entity(n, "TypeScript") for n in range(3)
    ]
    frozen = _freeze(balanced)
    assert len(frozen["sample"]) == 4 and len(frozen["pilot"]) == 2
    for pool in (
        [],
        balanced[:5],  # one TypeScript short for the pilot
        [_entity(n, "Python") for n in range(6)],  # no TypeScript at all
        [_entity(n, "TypeScript") for n in range(6)],  # no Python at all
    ):
        with pytest.raises(ReviewError, match="SAMPLE_POOL_INSUFFICIENT"):
            _freeze(pool)
    # Role entities share their symbol's subject group, so collisions shrink the pool.
    colliding = [_entity(n, "Python") for n in range(2)] + [
        _entity(n, "Python", "api_endpoint", subject=f"symbol:Python:{n}") for n in range(2)
    ] + [_entity(n, "TypeScript") for n in range(3)]  # fmt: skip
    with pytest.raises(ReviewError, match="SAMPLE_POOL_INSUFFICIENT"):
        _freeze(colliding)


# ---------------------------------------------------------------- F4 configuration


def test_configuration_failures_are_their_own_cause() -> None:
    assert classify("generate", "configuration_invalid", None) == "provider_failure"
    assert failure_cause("configuration_invalid", ("CONFIGURATION_INVALID", "HTTP_401")) == (
        "configuration_invalid"
    )
    # Rows recorded before the dedicated outcome existed still carry the first code.
    assert failure_cause("provider_unavailable", ("CONFIGURATION_INVALID",)) == (
        "configuration_invalid"
    )
    assert failure_cause("provider_unavailable", ("PROVIDER_UNAVAILABLE", "HTTP_503")) == (
        "provider"
    )
    assert failure_cause("accepted", ()) is None
    prereg = _prereg()
    sample = _sample(2, prereg)
    keys = [item["entity_key"] for item in sample["sample"]]
    outcomes = {
        keys[0]: {"outcome": "provider_failure", "failure_cause": "configuration_invalid"},
        keys[1]: {"outcome": "provider_failure", "failure_cause": "provider"},
    }
    packet = build_packet(sample=sample, preregistration=prereg, authority=_authority(),
                          outcomes=outcomes, interpretations={})  # fmt: skip
    result = score(packet, _reviews(packet), prereg, sample, _metrics())
    assert result["operational"]["provider_failure_causes"] == {
        "provider": 1,
        "configuration_invalid": 1,
    }


# ---------------------------------------------------------------- F6 recognizer recall


def _recognizer_review() -> tuple[dict[str, Any], dict[str, Any]]:
    route = _entity(1, "Python", "api_endpoint", subject="symbol:Python:1")
    miss = _entity(2, "Python")
    object.__setattr__(miss, "locator", "app/routes.py")
    packet = recognizer_packet(
        [route, miss], {route.entity_key: {"recognizer": "fastapi-route.v1"}}
    )
    assert [item["entity_key"] for item in packet["candidates"]] == [miss.entity_key]
    reviews = {
        "packet_digest": packet["packet_digest"],
        "matches": [{"entity_key": route.entity_key, "is_correct": True}],
        "candidates": [{"entity_key": miss.entity_key, "is_missed_match": True,
                        "recognizer": None}],
    }  # fmt: skip
    return packet, reviews


def test_missed_matches_must_be_attributed_before_recall() -> None:
    packet, reviews = _recognizer_review()
    with pytest.raises(ReviewError, match="MISS_UNATTRIBUTED"):
        recognizer_score(packet, reviews)  # the template's default null attribution
    unknown = copy.deepcopy(reviews)
    unknown["candidates"][0]["recognizer"] = "made-up.v1"
    with pytest.raises(ReviewError, match="MISS_UNATTRIBUTED"):
        recognizer_score(packet, unknown)
    attributed = copy.deepcopy(reviews)
    attributed["candidates"][0]["recognizer"] = "fastapi-route.v1"
    route = recognizer_score(packet, attributed)["recognizers"]["fastapi-route.v1"]
    assert route["recall_against_reviewed_candidates"] == 0.5 and route["precision"] == 1.0
    scoped = copy.deepcopy(reviews)
    scoped["candidates"][0]["recognizer"] = OUT_OF_SCOPE
    result = recognizer_score(packet, scoped)
    assert result["out_of_scope_misses"] == 1
    assert result["recognizers"]["fastapi-route.v1"]["recall_against_reviewed_candidates"] == 1.0


def test_recognizer_ids_match_the_recognizers_module() -> None:
    source = Path(recognizers.__file__).read_text()
    used = set(re.findall(r'"([a-z-]+\.v1)"', source))
    assert used == set(RECOGNIZERS)
