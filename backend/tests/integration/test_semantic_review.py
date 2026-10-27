"""Stage 13 Milestone D: grounding metrics, hard gates and pre-registered review tooling."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, text, update

from buildlens.application.semantic_catalog import EntityFilters
from buildlens.application.semantic_generation import PlanSelection
from buildlens.application.semantic_review import (
    ReviewError,
    preregistration_template,
    recognizer_score,
    review_template,
    score,
)
from buildlens.domain.source import digest
from buildlens.infrastructure.persistence.models import SymbolRecord
from tests.generation_support import (
    StubModelServer,
    completion,
    generation_service,
    grounded_claims,
    package_of,
)
from tests.semantic_support import REPOSITORY, CatalogStack, write

pytestmark = pytest.mark.integration


def _approved(**criteria: float) -> dict[str, Any]:
    value = preregistration_template()
    value["quality_criteria"] = {
        "claim_correctness_min": 0.9,
        "overclaim_rate_max": 0.05,
        "omission_rate_max": 0.25,
        "usefulness_mean_min": 2.4,
        **criteria,
    }
    value["sample"].update(size=8, pilot_size=2)
    value["provider"].update(provider="local", model="stub-model")
    value["status"], value["approved_by"] = "approved", "test: explicit approval message"
    return value


def _filled(template: dict[str, Any], correctness: str = "correct") -> dict[str, Any]:
    reviews: dict[str, Any] = copy.deepcopy(template)
    reviews["reviewer"] = "test reviewer"
    for entity in reviews["entities"]:
        entity["usefulness"], entity["omission"] = 3, False
        for claim in entity["claims"]:
            claim["correctness"], claim["overclaim"] = correctness, False
    return reviews


def test_metrics_gates_review_packets_and_recognizer_ground_truth(
    pg_engine: Engine, tmp_path: Path
) -> None:
    write(tmp_path, REPOSITORY)
    stack = CatalogStack(pg_engine, tmp_path)
    snapshot = stack.prepared()
    project, snap = stack.project.id, snapshot.id
    stack.catalog.build(project, snap)
    names: dict[tuple[str, str], str] = {}
    fate: dict[str, str] = {}

    def reply(request: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        package = package_of(request)
        entity = package["entity"]
        planned = fate.get(names.get((entity["locator"], entity["name"]), ""))
        if planned == "rate_limited":
            return 429, {"error": {"message": "slow down"}}, {}
        if planned == "abstain":
            return 200, completion({"claims": [], "unknowns": []}), {}
        if planned == "reject":
            bad: dict[str, Any] = {"category": "purpose", "text": "`nowhere_defined` is the core.",
                   "evidence": [], "assertion": None}  # fmt: skip
            return 200, completion({"claims": [bad], "unknowns": []}), {}
        return 200, completion(grounded_claims(package)), {}

    server = StubModelServer(reply)
    service = generation_service(stack, server, settings=_settings())
    source = {"kind": "test", "root": "fixture"}

    # The pre-registration (criteria, rubric, denominators) must be approved first.
    with pytest.raises(ReviewError, match="PREREGISTRATION_NOT_APPROVED"):
        service.freeze_sample(project, snap, preregistration_template(), source)
    edited = _approved()
    edited["rubric"] = {**edited["rubric"], "scope": "anything goes"}
    with pytest.raises(ReviewError, match="PREREGISTRATION_RUBRIC_MISMATCH"):
        service.freeze_sample(project, snap, edited, source)

    # The sample and a disjoint pilot are frozen from the catalog before any generation.
    approved = _approved()
    sample = service.freeze_sample(project, snap, approved, source)
    assert sample == service.freeze_sample(project, snap, approved, source)  # deterministic
    keys = [item["entity_key"] for item in sample["sample"]]
    pilot = {item["entity_key"] for item in sample["pilot"]}
    assert len(keys) == 8 and len(pilot) == 2 and not pilot & set(keys)
    assert {item["language"] for item in sample["sample"]} == {"Python", "TypeScript"}

    # Generation fails for some sampled entities; none of them is replaced.
    names.update({(item["locator"], item["name"]): item["entity_key"] for item in sample["sample"]})
    # Some sampled fixture entities can be ineligible in a plan (e.g. EMPTY_PACKAGE); they stay
    # in the packet under that outcome. Fates go to the entities the plan will send, and the
    # budget is one request short of them, so the last one is never attempted.
    selection = PlanSelection(entity_keys=tuple(keys))
    probe = service.plan(project, snap, selection)
    ineligible = [item.entity_key for item in probe.items if item.disposition == "ineligible"]
    sent = [
        item.entity_key
        for item in sorted(probe.items, key=lambda item: item.ordinal)
        if item.disposition == "generate"
    ]
    assert len(sent) >= 4
    fate.update(zip(sent[:3], ("rate_limited", "abstain", "reject"), strict=True))
    runner = generation_service(
        stack, server, settings=_settings(max_outbound_requests=len(sent) - 1)
    )
    try:
        plan = runner.plan(project, snap, selection)
        run = runner.execute(project, snap, plan.id, plan.plan_digest, True)
    finally:
        server.close()

    metrics = service.metrics(project, snap)
    gates = metrics["hard_gates"]
    assert gates["passed"] and gates["H1_published_claims_failing_revalidation"] == 0
    assert metrics["outbound"]["attempts_outside_plans"] == 0
    assert metrics["claims"]["total"] > 0 and metrics["coverage"]["entries"]["current"] > 0
    assert metrics["claims"]["label"]  # basis labels are reported, not quality

    packet = service.review_packet(project, snap, approved, sample, run.id)
    assert packet == service.review_packet(project, snap, approved, sample, run.id)
    outcomes = {item["entity_key"]: item["outcome"] for item in packet["entities"]}
    assert list(outcomes) == keys  # every sampled entity, in frozen order
    assert outcomes[sent[0]] == "provider_failure"
    assert outcomes[sent[1]] == "abstained" and outcomes[sent[2]] == "rejected"
    assert outcomes[sent[-1]] == "not_attempted"
    assert all(outcomes[key] == "ineligible" for key in ineligible)
    assert (packet["plan_id"], packet["catalog_run_id"]) == (str(plan.id), sample["catalog_run_id"])
    assert (packet["provider"], packet["model"]) == ("local", "stub-model")
    # The run must belong to the sample's catalog and the pre-registered provider and model.
    other_model = _approved()
    other_model["provider"]["model"] = "another-model"
    with pytest.raises(ReviewError, match="PROVIDER_MISMATCH"):
        service.review_packet(
            project, snap, other_model,
            service.freeze_sample(project, snap, other_model, source), run.id,
        )  # fmt: skip
    foreign = {key: value for key, value in sample.items() if key != "sample_digest"}
    foreign["catalog_run_id"] = str(uuid4())
    foreign["sample_digest"] = digest(foreign)
    with pytest.raises(ReviewError, match="SAMPLE_CATALOG_MISMATCH"):
        service.review_packet(project, snap, approved, foreign, run.id)
    reviewable = [item for item in packet["entities"] if item["reviewable"]]
    assert {item["outcome"] for item in reviewable} <= {"accepted", "abstained"}

    template = review_template(packet)
    assert [item["entity_key"] for item in template["entities"]] == [
        item["entity_key"] for item in reviewable
    ]
    result = score(packet, _filled(template), approved, sample, metrics)
    assert result["operational"]["sample_size"] == 8
    assert result["operational"]["counts"]["provider_failure"] == 1
    assert result["operational"]["provider_failure_causes"]["provider"] == 1
    assert result["operational"]["counts"]["rejected"] == 1
    assert result["quality"]["reviewable_entities"] == len(reviewable)
    assert result["quality"]["measured"]["claim_correctness"] == 1.0 and result["quality"]["passed"]
    # Perfect reviewed outputs are not an overall pass while sampled entities lack an outcome.
    assert result["overall"] == "inconclusive" and not result["passed"]
    incomplete = result["evaluation"]["incomplete_outcomes"]
    assert incomplete == {
        "provider_failure": 1,
        "not_attempted": 1,
        **({"ineligible": len(ineligible)} if ineligible else {}),
    }
    # Partially correct claims never count as correct.
    partial = score(packet, _filled(template, "partially_correct"), approved, sample, metrics)
    assert partial["quality"]["measured"]["claim_correctness"] == 0.0
    assert partial["quality"]["measured"]["claim_partially_correct_rate"] == 1.0
    assert (
        not partial["passed"] and not partial["quality"]["criteria_passed"]["claim_correctness_min"]
    )
    with pytest.raises(ReviewError, match="REVIEW_INCOMPLETE"):
        score(
            packet,
            {**_filled(template), "entities": _filled(template)["entities"][1:]},
            approved,
            sample,
            metrics,
        )
    tampered = copy.deepcopy(packet)
    tampered["entities"][0]["outcome"] = "accepted"
    with pytest.raises(ReviewError, match="PACKET_DIGEST_MISMATCH"):
        score(tampered, _filled(template), approved, sample, metrics)
    shrunk = copy.deepcopy(sample)
    shrunk["sample"] = shrunk["sample"][1:]
    with pytest.raises(ReviewError, match="SAMPLE_DIGEST_MISMATCH"):
        score(packet, _filled(template), approved, shrunk, metrics)
    with pytest.raises(ReviewError, match="PREREGISTRATION_MISMATCH"):
        score(packet, _filled(template), _approved(claim_correctness_min=0.5), sample, metrics)

    # Recognizer ground truth packet and scoring.
    recognizers = service.recognizer_packet(project, snap)
    found = {(m["recognizer"], m["name"]) for m in recognizers["matches"]}
    assert ("fastapi-route.v1", "GET /users/{user_id}") in found
    assert ("react-hook.v1", "useToggle") in found
    reviews = {
        "packet_digest": recognizers["packet_digest"],
        "matches": [
            {"entity_key": m["entity_key"], "is_correct": True} for m in recognizers["matches"]
        ],
        "candidates": [
            {"entity_key": c["entity_key"], "is_missed_match": False, "recognizer": None}
            for c in recognizers["candidates"]
        ],
    }
    scored = recognizer_score(recognizers, reviews)
    assert all(value["precision"] == 1.0 for value in scored["recognizers"].values())

    # Hard gate H1 trips when a cited fact's structural rows no longer reproduce it...
    catalog_run = stack.catalog.select(project, snap, None)[0]
    entities, _ = stack.store.entity_page(catalog_run.id, EntityFilters(kind="symbol"), 500, 0)
    with stack.factory.begin() as session:
        session.execute(
            update(SymbolRecord)
            .where(SymbolRecord.stable_identity_id.in_([e.symbol_identity_id for e in entities]))
            .values(qualified_name=func.upper(SymbolRecord.qualified_name))
        )
    tripped = service.metrics(project, snap)["hard_gates"]
    assert tripped["H1_published_claims_failing_revalidation"] > 0 and not tripped["passed"]
    # ...and H2 when any attempt is not a planned generate item of its run.
    with pg_engine.begin() as connection:
        run_id = connection.execute(
            text("SELECT id FROM semantic_generation_runs WHERE project_version_id = :s"),
            {"s": snap},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO semantic_generation_attempts (id, run_id, entity_key, attempt_no, "
                "package_digest, request_digest, input_key) VALUES (:id, :run, 'symbol:rogue', 1, "
                "'p', :digest, 'k')"
            ),
            {"id": uuid4(), "run": run_id, "digest": "e" * 64},
        )
    audit = service.metrics(project, snap)["outbound"]
    assert audit["attempts_outside_plans"] == 1


def _settings(**values: Any) -> Any:
    from buildlens.application.semantic_generation import GenerationSettings

    return GenerationSettings(
        **{"enabled": True, "request_timeout_seconds": 10, "deadline_seconds": 120, **values}
    )
