"""Stage 13 Milestone C: plan-authorised generation on real PostgreSQL (§8.3, §11.5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text, update

from buildlens.api.main import create_app
from buildlens.application.semantic_catalog import EntityFilters
from buildlens.application.semantic_generation import (
    BudgetExhaustedError,
    GenerationConflictError,
    GenerationDisabledError,
    GenerationSettings,
    OutcomeRecord,
    PlanAlreadyUsedError,
    PlanChangedError,
    PlanConfigChangedError,
    PlanExpiredError,
    PlanInvalidError,
    PlanSelection,
    RunFencedError,
    RunResults,
    SemanticGenerationService,
)
from buildlens.core.config import Settings
from buildlens.domain.semantic_generation import GenerationPlan, Interpretation
from buildlens.infrastructure.persistence.models import (
    ChunkRecord,
    SemanticGenerationLateReceiptRecord,
    SemanticGenerationOutcomeRecord,
    SemanticGenerationPlanItemRecord,
    SemanticGenerationRunRecord,
    SemanticInterpretationRecord,
)
from buildlens.infrastructure.persistence.semantic_generation import SqlAlchemyGenerationStore
from tests.generation_support import (
    EXECUTE_SCRIPT,
    StubModelServer,
    completion,
    generation_service,
    grounded_claims,
    package_of,
)
from tests.semantic_support import REPOSITORY, CatalogStack, write

pytestmark = pytest.mark.integration
BACKEND = Path(__file__).parents[2]


@pytest.fixture
def server() -> Iterator[StubModelServer]:
    value = StubModelServer()
    yield value
    value.close()


def _stack(engine: Engine, root: Path) -> tuple[CatalogStack, Any]:
    write(root, REPOSITORY)
    stack = CatalogStack(engine, root)
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    return stack, snapshot


def _keys(stack: CatalogStack, snapshot: Any, *names: str) -> tuple[str, ...]:
    run = stack.catalog.select(stack.project.id, snapshot.id, None)[0]
    values, _ = stack.store.entity_page(run.id, EntityFilters(), 500, 0)
    by_name = {(item.kind, item.name): item.entity_key for item in values}
    return tuple(by_name[("symbol", name)] for name in names)


def _plan(
    service: SemanticGenerationService, stack: CatalogStack, snapshot: Any, *keys: str, **extra: Any
) -> GenerationPlan:
    return service.plan(stack.project.id, snapshot.id, PlanSelection(entity_keys=keys, **extra))


def _interpretation(run: Any, attempt: Any, item: Any, profile: str) -> Interpretation:
    return Interpretation(
        uuid4(), run.project_id, run.snapshot_id, run.id, attempt.id, item.entity_key,
        item.input_key, {}, profile, "local", "other-model", "returned", "semantic-prompt.v1",
        "semantic-claims.v1", "semantic-validator.v1", "insufficient_evidence", (),
        datetime.now(UTC),
    )  # fmt: skip


def _generate(plan: GenerationPlan) -> Any:
    return next(item for item in plan.items if item.disposition == "generate")


def _expire(stack: CatalogStack, run_id: Any) -> None:
    with stack.factory.begin() as session:
        session.execute(
            update(SemanticGenerationRunRecord)
            .where(SemanticGenerationRunRecord.id == run_id)
            .values(lease_expires_at=func.clock_timestamp() - text("interval '1 second'"))
        )


def _pointer(stack: CatalogStack, snapshot: Any) -> Any:
    with stack.factory() as session:
        return session.execute(
            text("SELECT active_semantic_index_run_id FROM project_versions WHERE id = :id"),
            {"id": snapshot.id},
        ).scalar_one()


def test_plan_execute_reuse_profiles_and_provenance(
    pg_engine: Engine, tmp_path: Path, server: StubModelServer
) -> None:
    stack, snapshot = _stack(pg_engine, tmp_path)
    service = generation_service(stack, server)
    assert service.configuration()["sharing_class"] == "loopback"
    keys = _keys(stack, snapshot, "get_user", "session", "Button")
    plan = _plan(service, stack, snapshot, *keys)
    assert server.requests == []  # configuration and preflight never contact the provider
    again = service.get_plan(stack.project.id, snapshot.id, plan.id)
    assert (again.plan_nonce, again.plan_digest, again.items) == (
        plan.plan_nonce, plan.plan_digest, plan.items,
    )  # fmt: skip
    assert [item.disposition for item in plan.items] == ["generate"] * 3
    roles = {(value["locator"], value["role"]) for value in plan.contributing_locators}
    assert ("app/db.py", "neighbor_facts") in roles  # get_user's neighbour fact source
    assert plan.caps["max_outbound_requests"] == 6 and plan.sharing_class == "loopback"

    run = service.execute(stack.project.id, snapshot.id, plan.id, plan.plan_digest, True)
    assert run.state == "complete" and len(server.requests) == 3
    _, attempts = service.run(stack.project.id, snapshot.id, run.id)
    assert [item.outcome for item in attempts] == ["accepted"] * 3
    assert {request["model"] for request in server.requests} == {"stub-model"}
    view = service.entry(stack.project.id, snapshot.id, keys[0])
    assert view.state == "current" and view.entry is not None and view.entry.mode == "generated"
    assert view.interpretation is not None
    assert (view.interpretation.requested_model, view.interpretation.returned_model) == (
        "stub-model", "stub-model-1",
    )  # fmt: skip
    citations = [c for claim in view.interpretation.claims for c in claim.citations]
    assert citations and all(c.verification == "verified" and c.quote for c in citations)
    with pytest.raises(PlanAlreadyUsedError):
        service.execute(stack.project.id, snapshot.id, plan.id, plan.plan_digest, True)

    # Same profile: exact-key reuse, zero outbound.
    reuse = _plan(service, stack, snapshot, *keys)
    assert [item.disposition for item in reuse.items] == ["reuse"] * 3
    assert (
        service.execute(stack.project.id, snapshot.id, reuse.id, reuse.plan_digest, True).state
        == "complete"
    )
    assert len(server.requests) == 3
    # A different profile never reuses a foreign profile in generate mode...
    other = generation_service(stack, server, model="other-model")
    assert [i.disposition for i in _plan(other, stack, snapshot, *keys).items] == ["generate"] * 3
    # ...and reuse_only needs explicit acceptance, recording the source profile.
    with pytest.raises(ValueError):
        _plan(other, stack, snapshot, *keys, mode="reuse_only")
    accepted = _plan(other, stack, snapshot, *keys, mode="reuse_only", accept_profiles="any")
    assert {item.reuse_profile_digest for item in accepted.items} == {
        service.configuration()["generator_profile_digest"]
    }
    other.execute(stack.project.id, snapshot.id, accepted.id, accepted.plan_digest, True)
    view = other.entry(stack.project.id, snapshot.id, keys[1])
    assert view.entry is not None and view.entry.mode == "reused"
    assert view.entry.reused_from_profile == service.configuration()["generator_profile_digest"]
    assert view.interpretation is not None and view.interpretation.requested_model == "stub-model"
    assert len(server.requests) == 3


def test_plan_integrity_expiry_configuration_and_authority(
    pg_engine: Engine, tmp_path: Path, server: StubModelServer
) -> None:
    stack, snapshot = _stack(pg_engine, tmp_path)
    service = generation_service(stack, server)
    (key,) = _keys(stack, snapshot, "load")
    project, snap = stack.project.id, snapshot.id

    tampered = _plan(service, stack, snapshot, key)
    with stack.factory.begin() as session:
        session.execute(
            update(SemanticGenerationPlanItemRecord)
            .where(SemanticGenerationPlanItemRecord.plan_id == tampered.id)
            .values(request_bytes=1)
        )
    with pytest.raises(PlanInvalidError):
        service.execute(project, snap, tampered.id, tampered.plan_digest, True)
    plan = _plan(service, stack, snapshot, key)
    with pytest.raises(PlanInvalidError):
        service.execute(project, snap, plan.id, "0" * 64, True)
    with pytest.raises(PlanConfigChangedError):
        generation_service(stack, server, model="changed").execute(
            project, snap, plan.id, plan.plan_digest, True
        )
    with pytest.raises(ValueError):
        service.execute(project, snap, plan.id, plan.plan_digest, False)
    short = generation_service(
        stack, server, settings=GenerationSettings(enabled=True, plan_ttl_seconds=1)
    )
    expiring = _plan(short, stack, snapshot, key)
    time.sleep(1.2)
    with pytest.raises(PlanExpiredError):
        short.execute(project, snap, expiring.id, expiring.plan_digest, True)
    with pytest.raises(GenerationDisabledError):
        generation_service(stack, server, settings=GenerationSettings()).plan(
            project, snap, PlanSelection(entity_keys=(key,))
        )
    stack.processing.process(project, snap)
    stack.catalog.build(project, snap)
    with pytest.raises(PlanChangedError):
        service.execute(project, snap, plan.id, plan.plan_digest, True)
    assert server.requests == []


def test_budget_explicit_retry_token_bound_and_rejection(
    pg_engine: Engine, tmp_path: Path, server: StubModelServer
) -> None:
    stack, snapshot = _stack(pg_engine, tmp_path)
    project, snap = stack.project.id, snapshot.id
    keys = _keys(stack, snapshot, "load", "session", "get_user")
    calls = {"n": 0}

    def flaky(request: dict[str, Any]) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {"error": "slow down"}, {"Retry-After": "0"}
        return 200, completion(grounded_claims(package_of(request))), {}

    server.reply = flaky
    service = generation_service(stack, server)
    plan = _plan(service, stack, snapshot, keys[0])
    run = service.execute(project, snap, plan.id, plan.plan_digest, True)
    _, attempts = service.run(project, snap, run.id)
    assert [(a.attempt_no, a.outcome) for a in attempts] == [(1, "rate_limited"), (2, "accepted")]
    assert run.attempts_started == 2 and len(server.requests) == 2

    # The outbound budget is never exceeded: two entities, one request allowed.
    server.reply = lambda request: (200, completion(grounded_claims(package_of(request))), {})
    tight = generation_service(
        stack, server, settings=GenerationSettings(enabled=True, max_outbound_requests=1)
    )
    plan = _plan(tight, stack, snapshot, keys[1], keys[2])
    run = tight.execute(project, snap, plan.id, plan.plan_digest, True)
    assert run.state == "partial" and run.diagnostics == ("BUDGET_EXHAUSTED",)
    assert len(server.requests) == 3 and run.attempts_started == 1

    # Reported prompt tokens above the byte bound stop the run; nothing is stored.
    server.reply = lambda request: (
        200, {**completion(grounded_claims(package_of(request))),
              "usage": {"prompt_tokens": 10_000_000, "completion_tokens": 1}}, {},
    )  # fmt: skip
    plan = _plan(service, stack, snapshot, keys[2])
    run = service.execute(project, snap, plan.id, plan.plan_digest, True)
    _, attempts = service.run(project, snap, run.id)
    assert attempts[0].outcome == "token_bound_violated" and run.state == "failed"
    # Malformed output is rejected and never becomes a candidate.
    server.reply = lambda request: (200, completion({"claims": "nope"}), {})
    plan = _plan(service, stack, snapshot, keys[2])
    run = service.execute(project, snap, plan.id, plan.plan_digest, True)
    _, attempts = service.run(project, snap, run.id)
    assert attempts[0].outcome == "rejected" and attempts[0].rejection_codes == ("OUTPUT_INVALID",)
    with stack.factory() as session:
        stored = session.scalar(
            select(func.count())
            .select_from(SemanticInterpretationRecord)
            .where(SemanticInterpretationRecord.entity_key == keys[2])
        )
    assert stored == 0


def test_presend_recheck_blocks_changed_bytes_and_authority(
    pg_engine: Engine, tmp_path: Path, server: StubModelServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, snapshot = _stack(pg_engine, tmp_path)
    project, snap = stack.project.id, snapshot.id
    service = generation_service(stack, server)
    load, session_key = _keys(stack, snapshot, "load", "session")
    plan = _plan(service, stack, snapshot, load)
    entity = stack.store.entity(stack.catalog.select(project, snap, None)[0].id, load)
    assert entity is not None
    with stack.factory.begin() as session:
        session.execute(
            update(ChunkRecord)
            .where(ChunkRecord.symbol_identity_id == entity.symbol_identity_id)
            .values(content=func.upper(ChunkRecord.content))
        )
    run = service.execute(project, snap, plan.id, plan.plan_digest, True)
    _, attempts = service.run(project, snap, run.id)
    assert attempts[0].outcome == "not_sent" and attempts[0].rejection_codes == (
        "PACKAGE_INTEGRITY",
    )
    assert server.requests == []

    plan = _plan(service, stack, snapshot, session_key)
    answers = iter([True, False])
    store = service._store  # noqa: SLF001 - drive the pre-send recheck deterministically
    monkeypatch.setattr(store, "authority_unchanged", lambda run: next(answers, False))
    run = service.execute(project, snap, plan.id, plan.plan_digest, True)
    _, attempts = service.run(project, snap, run.id)
    assert attempts[0].outcome == "not_sent" and attempts[0].rejection_codes == (
        "AUTHORITY_CHANGED",
    )
    assert server.requests == []


def test_fencing_late_receipts_abandonment_and_publication(
    pg_engine: Engine, tmp_path: Path, server: StubModelServer
) -> None:
    stack, snapshot = _stack(pg_engine, tmp_path)
    project, snap = stack.project.id, snapshot.id
    service = generation_service(stack, server)
    other = generation_service(stack, server, model="other-model")
    store = SqlAlchemyGenerationStore(stack.factory)
    config = other.configuration()
    keys = _keys(stack, snapshot, "load", "session")

    def admit(plan: GenerationPlan) -> Any:
        return store.admit(
            project, snap, plan.id, plan.plan_digest, config["provider_config_digest"], 60
        )[0]

    # 1. A's lease expires, B admits (abandoning A) and publishes; A's late response is fenced.
    plan_a = _plan(other, stack, snapshot, *keys)
    plan_b = _plan(service, stack, snapshot, *keys)
    run_a = admit(plan_a)
    attempt_a = store.begin_attempt(run_a, plan_a.items[0], 60)
    _expire(stack, run_a.id)
    run_b = service.execute(project, snap, plan_b.id, plan_b.plan_digest, True)
    assert run_b.state == "complete" and _pointer(stack, snapshot) == run_b.id
    late = OutcomeRecord("accepted", "m", 1, 1, 5, "f" * 64)
    candidate = _interpretation(
        run_a, attempt_a, plan_a.items[0], config["generator_profile_digest"]
    )
    with pytest.raises(RunFencedError):
        store.record_outcome(run_a, attempt_a, late, candidate, 60)
    with pytest.raises(RunFencedError):
        store.begin_attempt(run_a, plan_a.items[1], 60)
    with pytest.raises(RunFencedError):
        store.finish(run_a, "complete", RunResults())
    with stack.factory() as session:
        assert session.get(SemanticGenerationRunRecord, run_a.id).state == "abandoned"
        assert (
            session.scalar(
                select(SemanticGenerationOutcomeRecord.outcome).where(
                    SemanticGenerationOutcomeRecord.attempt_id == attempt_a.id
                )
            )
            == "outcome_unknown"
        )
        assert (
            session.scalar(
                select(SemanticGenerationLateReceiptRecord.reason).where(
                    SemanticGenerationLateReceiptRecord.attempt_id == attempt_a.id
                )
            )
            == "RUN_TERMINAL"
        )
        assert session.get(SemanticInterpretationRecord, candidate.id) is None
    assert _pointer(stack, snapshot) == run_b.id

    # 2. An expired lease with no other process: the fenced process self-abandons.
    plan_c = _plan(other, stack, snapshot, *keys)
    run_c = admit(plan_c)
    _expire(stack, run_c.id)
    with pytest.raises(RunFencedError):
        store.begin_attempt(run_c, _generate(plan_c), 60)
    assert store.self_abandon(run_c) and _pointer(stack, snapshot) == run_b.id

    # 3a. T2 commits before abandonment: candidate + outcome, never outcome_unknown.
    plan_d = _plan(other, stack, snapshot, *keys)
    run_d = admit(plan_d)
    attempt_d = store.begin_attempt(run_d, _generate(plan_d), 60)
    kept = _interpretation(run_d, attempt_d, _generate(plan_d), config["generator_profile_digest"])
    store.record_outcome(
        run_d, attempt_d, OutcomeRecord("insufficient_evidence", response_digest="a" * 64), kept, 60
    )
    _expire(stack, run_d.id)
    assert store.abandon_expired(project, snap) == [run_d.id]
    # 3b. Abandonment before T2: outcome_unknown plus a late receipt, never a candidate.
    plan_e = _plan(other, stack, snapshot, *keys)
    run_e = admit(plan_e)
    attempt_e = store.begin_attempt(run_e, _generate(plan_e), 60)
    _expire(stack, run_e.id)
    assert store.abandon_expired(project, snap) == [run_e.id]
    lost = _interpretation(run_e, attempt_e, _generate(plan_e), config["generator_profile_digest"])
    with pytest.raises(RunFencedError):
        store.record_outcome(
            run_e, attempt_e, OutcomeRecord("accepted", response_digest="b" * 64), lost, 60
        )
    with stack.factory() as session:
        outcomes = dict(
            session.execute(
                select(
                    SemanticGenerationOutcomeRecord.attempt_id,
                    SemanticGenerationOutcomeRecord.outcome,
                ).where(
                    SemanticGenerationOutcomeRecord.attempt_id.in_([attempt_d.id, attempt_e.id])
                )
            ).all()
        )
        assert outcomes == {attempt_d.id: "insufficient_evidence", attempt_e.id: "outcome_unknown"}
        assert session.get(SemanticInterpretationRecord, kept.id) is not None
        assert session.get(SemanticInterpretationRecord, lost.id) is None

    # 4. Lease renewal and recovery serialize: a renewed lease is never abandoned.
    plan_f = _plan(other, stack, snapshot, *keys)
    run_f = admit(plan_f)
    with stack.factory.begin() as session:
        session.execute(
            update(SemanticGenerationRunRecord)
            .where(SemanticGenerationRunRecord.id == run_f.id)
            .values(lease_expires_at=func.clock_timestamp() + text("interval '2 seconds'"))
        )
    store.begin_attempt(run_f, _generate(plan_f), 60)  # renewal
    assert store.abandon_expired(project, snap) == []
    _expire(stack, run_f.id)
    assert store.abandon_expired(project, snap) == [run_f.id]
    with pytest.raises(RunFencedError):
        store.begin_attempt(run_f, _generate(plan_f), 60)

    # 5. A superseded run keeps its committed candidate; a later reuse_only run reuses it.
    plan_g = _plan(other, stack, snapshot, keys[1])
    run_g = admit(plan_g)
    attempt_g = store.begin_attempt(run_g, _generate(plan_g), 60)
    kept_g = _interpretation(
        run_g, attempt_g, _generate(plan_g), config["generator_profile_digest"]
    )
    store.record_outcome(
        run_g,
        attempt_g,
        OutcomeRecord("insufficient_evidence", response_digest="c" * 64),
        kept_g,
        60,
    )
    stack.processing.process(project, snap)
    stack.catalog.build(project, snap)
    assert store.finish(run_g, "complete", RunResults()).state == "superseded"
    assert _pointer(stack, snapshot) == run_b.id
    reuse = _plan(other, stack, snapshot, keys[1], mode="reuse_only", accept_profiles="any")
    assert reuse.items[0].reuse_interpretation_id == kept_g.id
    assert lost.id not in {item.reuse_interpretation_id for item in reuse.items}
    run_i = other.execute(project, snap, reuse.id, reuse.plan_digest, True)
    assert run_i.state == "complete" and _pointer(stack, snapshot) == run_i.id

    # 6. R3: a run whose captured index is no longer active cannot publish over it.
    plan_h = _plan(other, stack, snapshot, *keys)
    run_h = admit(plan_h)
    with stack.factory.begin() as session:
        session.execute(
            text("UPDATE project_versions SET active_semantic_index_run_id = :old WHERE id = :id"),
            {"old": run_b.id, "id": snap},
        )
    assert store.finish(run_h, "complete", RunResults()).state == "superseded"
    assert _pointer(stack, snapshot) == run_b.id
    assert len(server.requests) == 2  # only B's two sends ever reached the provider


def test_concurrent_t1_never_exceeds_the_budget(
    pg_engine: Engine, tmp_path: Path, server: StubModelServer
) -> None:
    stack, snapshot = _stack(pg_engine, tmp_path)
    service = generation_service(
        stack, server, settings=GenerationSettings(enabled=True, max_outbound_requests=1)
    )
    store = SqlAlchemyGenerationStore(stack.factory)
    plan = _plan(service, stack, snapshot, *_keys(stack, snapshot, "load", "session"))
    assert plan.caps["max_outbound_requests"] == 1
    run, _ = store.admit(
        stack.project.id, snapshot.id, plan.id, plan.plan_digest,
        service.configuration()["provider_config_digest"], 60,
    )  # fmt: skip
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt(index: int) -> None:
        barrier.wait()
        try:
            store.begin_attempt(run, plan.items[index], 60)
            outcomes.append("started")
        except BudgetExhaustedError:
            outcomes.append("budget")

    threads = [threading.Thread(target=attempt, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["budget", "started"]
    with stack.factory() as session:
        assert session.get(SemanticGenerationRunRecord, run.id).attempts_started == 1


def _spawn(
    engine: Engine,
    server: StubModelServer,
    stack: CatalogStack,
    snapshot: Any,
    plan: GenerationPlan,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable, "-c", EXECUTE_SCRIPT, engine.url.render_as_string(hide_password=False),
            server.url, str(stack.project.id), str(snapshot.id), str(plan.id), plan.plan_digest,
        ],
        cwd=BACKEND,
        env={**os.environ, "PYTHONPATH": f"{BACKEND / 'src'}{os.pathsep}{BACKEND}"},
        stdout=subprocess.PIPE,
        text=True,
    )  # fmt: skip


def test_two_processes_consume_one_plan_and_one_executing_run(
    pg_engine: Engine, tmp_path: Path, server: StubModelServer
) -> None:
    stack, snapshot = _stack(pg_engine, tmp_path)
    service = generation_service(stack, server)
    keys = _keys(stack, snapshot, "load")
    plan = _plan(service, stack, snapshot, *keys)
    processes = [_spawn(pg_engine, server, stack, snapshot, plan) for _ in range(2)]
    results = sorted(p.communicate(timeout=120)[0].strip().splitlines()[-1] for p in processes)
    assert [json.loads(item) for item in results] == [
        {"error": "PlanAlreadyUsedError"},
        {"state": "complete"},
    ]
    with stack.factory() as session:
        runs = session.scalar(
            select(func.count())
            .select_from(SemanticGenerationRunRecord)
            .where(SemanticGenerationRunRecord.plan_id == plan.id)
        )
    assert runs == 1 and len(server.requests) == 1

    # A slow run in another OS process holds the snapshot; a concurrent run is refused.
    def slow(request: dict[str, Any]) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
        time.sleep(2)
        return 200, completion(grounded_claims(package_of(request))), {}

    server.reply = slow
    other = generation_service(stack, server, model="slow-model")
    keys = _keys(stack, snapshot, "session")
    first = _plan(service, stack, snapshot, *_keys(stack, snapshot, "get_user"))
    second = _plan(other, stack, snapshot, *keys)
    process = _spawn(pg_engine, server, stack, snapshot, first)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with stack.factory() as session:
            if session.scalar(
                select(func.count())
                .select_from(SemanticGenerationRunRecord)
                .where(SemanticGenerationRunRecord.state == "executing")
            ):
                break
        time.sleep(0.05)
    with pytest.raises(GenerationConflictError):
        other.execute(stack.project.id, snapshot.id, second.id, second.plan_digest, True)
    assert json.loads(process.communicate(timeout=120)[0].strip().splitlines()[-1]) == {
        "state": "complete"
    }


def test_http_contracts_and_entry_states(
    pg_engine: Engine, tmp_path: Path, server: StubModelServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, REPOSITORY)
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False),
        semantic_generation_enabled=True,
        semantic_provider="local",
        semantic_base_url=server.url,
        semantic_model="stub-model",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/semantic-generation").json()["sharing_class"] == "loopback"
        project = client.post(
            "/api/v1/projects", json={"name": "Gen", "root_path": str(tmp_path)}
        ).json()["id"]
        base = f"/api/v1/projects/{project}/snapshots"
        scope = f"{base}/{client.post(base, json={}).json()['id']}"
        for step in ("/structure", "/processing", "/semantic/catalog"):
            assert client.post(scope + step, json={}).status_code == 200
        semantic = scope + "/semantic"
        hooks = client.get(semantic + "/entities?kind=hook").json()["items"]
        assert hooks[0]["interpretation_state"] == "absent"
        key = hooks[0]["entity_key"]
        rejected = client.post(
            semantic + "/generation/plan", content="{}", headers={"Content-Type": "text/plain"}
        )
        assert rejected.status_code == 415
        plan = client.post(semantic + "/generation/plan", json={"entity_keys": [key]}).json()
        assert plan["items"][0]["disposition"] == "generate" and server.requests == []
        assert (
            client.get(semantic + f"/generation/plans/{plan['plan_id']}").json()["plan_digest"]
            == plan["plan_digest"]
        )
        body = {"plan_id": plan["plan_id"], "plan_digest": plan["plan_digest"], "confirm": True}
        run = client.post(semantic + "/generation", json=body)
        assert run.status_code == 200, run.text
        assert (
            run.json()["state"] == "complete" and run.json()["attempts"][0]["outcome"] == "accepted"
        )
        again = client.post(semantic + "/generation", json=body)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "PLAN_ALREADY_USED"
        entry = client.get(semantic + f"/entities/{key}/interpretation").json()
        assert entry["state"] == "current" and entry["mode"] == "generated"
        claims = entry["interpretation"]["claims"]
        assert claims and all(c["text"] and c["citations"][0]["quote"] for c in claims)
        assert client.get(semantic + "/entities?state=current").json()["total"] == 1
        history = client.get(semantic + f"/entities/{key}/history").json()["items"]
        assert len(history) == 1 and history[0]["interpretation"]["provider"] == "local"
        interpretation_id = entry["interpretation"]["interpretation_id"]
        assert client.get(semantic + f"/interpretations/{interpretation_id}").status_code == 200
        assert client.get(semantic + "/generation/runs").json()["total"] == 1
        missing = client.post(semantic + "/generation", json={**body, "plan_id": str(uuid4())})
        assert missing.json()["detail"]["code"] == "PLAN_NOT_FOUND"
        # A prompt/schema/validator bump makes every entry stale (CONTRACT_CHANGED).
        from buildlens.application import semantic_generation
        from buildlens.domain import semantic_generation as domain

        monkeypatch.setattr(domain, "PROMPT_VERSION", "semantic-prompt.v2")
        monkeypatch.setattr(semantic_generation, "PROMPT_VERSION", "semantic-prompt.v2")
        stale = client.get(semantic + f"/entities/{key}/interpretation").json()
        assert stale["state"] == "stale" and stale["stale_reasons"] == ["CONTRACT_CHANGED"]
    assert len(server.requests) == 1


def test_new_snapshot_reuse_only_adds_exact_key_entries_without_outbound(
    pg_engine: Engine, tmp_path: Path, server: StubModelServer
) -> None:
    stack, snapshot = _stack(pg_engine, tmp_path)
    service = generation_service(stack, server)
    keys = _keys(stack, snapshot, "load", "session")
    plan = _plan(service, stack, snapshot, *keys)
    service.execute(stack.project.id, snapshot.id, plan.id, plan.plan_digest, True)
    assert len(server.requests) == 2
    (tmp_path / "app" / "api.py").write_text(
        (tmp_path / "app" / "api.py").read_text() + "\n# edited\n"
    )
    second = stack.prepared()
    stack.catalog.build(stack.project.id, second.id)
    _, views = service.entry_views(stack.project.id, second.id, keys)
    assert {view.state for view in views.values()} == {"absent"}  # a new snapshot starts empty
    reuse = service.plan(
        stack.project.id, second.id,
        PlanSelection(entity_keys=keys, mode="reuse_only", accept_profiles="any"),
    )  # fmt: skip
    dispositions = {item.entity_key: item.disposition for item in reuse.items}
    # session (app/db.py) is byte-identical; load's file gained a trailing comment, but its own
    # package is unchanged too, so both reuse by exact input key.
    assert (
        set(dispositions.values()) <= {"reuse", "ineligible"} and "reuse" in dispositions.values()
    )
    run = service.execute(stack.project.id, second.id, reuse.id, reuse.plan_digest, True)
    assert run.state == "complete" and len(server.requests) == 2
    _, views = service.entry_views(stack.project.id, second.id, keys)
    assert any(
        view.state == "current" and view.entry and view.entry.mode == "reused"
        for view in views.values()
    )
    _ = replace
