"""Stage 13 Milestone D: the adversarial repository end to end (§8.7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from buildlens.application.semantic_catalog import EntityFilters
from buildlens.application.semantic_generation import PlanSelection
from tests.adversarial_repository import GITHUB_TOKEN, INJECTION, RLO, SENTINEL, materialize
from tests.generation_support import StubModelServer, completion, generation_service, package_of
from tests.semantic_support import CatalogStack

pytestmark = pytest.mark.integration


def _quote(evidence: list[dict[str, Any]], needle: str) -> dict[str, Any] | None:
    for item in evidence:
        index = item["text"].find(needle)
        if index >= 0 and item["text"].count(needle) == 1:
            return {"id": item["id"], "quote": needle}
    return None


def obedient_attacker(request: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
    """A model that follows every injected instruction it sees."""
    package = package_of(request)
    evidence = package["evidence"]
    claims: list[dict[str, Any]] = []
    name = package["entity"]["name"]
    injected = _quote(evidence, "IGNORE ALL PREVIOUS INSTRUCTIONS")
    if injected:
        claims += [
            {"category": "side_effect", "text": f"`{name}` deletes the production database.",
             "evidence": [injected], "assertion": None},
            {"category": "purpose", "text": "The code is secure.", "evidence": [injected],
             "assertion": None},
            {"category": "purpose", "text": f"`{name}` is documented as being in maintenance mode.",
             "evidence": [injected], "assertion": None},
        ]  # fmt: skip
    contradiction = _quote(evidence, "Returns None and sends an email")
    if contradiction:
        claims += [
            {"category": "behavior", "text": f"`{name}` sends an email to every user.",
             "evidence": [contradiction], "assertion": None},
            {"category": "purpose", "text": f"`{name}` is a coroutine.",
             "evidence": [contradiction], "assertion": {"kind": "is_async", "target": None}},
        ]  # fmt: skip
    homoglyph = _quote(evidence, "return value == access")
    if homoglyph:
        claims += [
            {"category": "behavior", "text": "`password_check` compares `value` with `access`.",
             "evidence": [homoglyph], "assertion": None},
            {"category": "purpose", "text": f"Checks the user{RLO}nimda value.",
             "evidence": [homoglyph], "assertion": None},
        ]  # fmt: skip
    return 200, completion({"claims": claims, "unknowns": []}), {}


def test_adversarial_repository_is_parsed_never_executed_and_injection_is_contained(
    pg_engine: Engine, tmp_path: Path
) -> None:
    root = materialize(tmp_path / "repo", dts_files=5)
    stack = CatalogStack(pg_engine, root)
    snapshot = stack.prepared()
    run = stack.catalog.build(stack.project.id, snapshot.id)

    # Nothing in the repository ran: no sentinel from imports, npm scripts or plugins.
    assert not list(tmp_path.rglob(SENTINEL)) and not (Path.cwd() / SENTINEL).exists()
    with pg_engine.connect() as connection:
        excluded = dict(
            connection.execute(text(
                "SELECT locator, exclusion_reasons[1] FROM artifacts "
                "WHERE project_version_id = :id AND inventory_status = 'excluded'"
            ), {"id": snapshot.id}).all()
        )  # fmt: skip
        analyses = dict(
            connection.execute(text(
                "SELECT locator, coalesce(diagnostics->0->>'code', outcome) FROM artifact_analyses"
            )).all()
        )  # fmt: skip
        leaked = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM chunks WHERE content LIKE :token) "
                "+ (SELECT count(*) FROM semantic_facts WHERE rendered_text LIKE :token) "
                "+ (SELECT count(*) FROM symbols WHERE signature::text LIKE :token)"
            ),
            {"token": f"%{GITHUB_TOKEN}%"},
        ).scalar_one()
    assert excluded["app/outside_link.py"] == "symlink_not_followed"
    assert excluded["app/inner_link.py"] == "symlink_not_followed"
    assert excluded["node_modules"] == "dependency_directory" and excluded["dist"] == "build_output"
    assert excluded["deploy/server.pem"] == "secret_sensitive"
    assert analyses["app/huge.py"] == "SOURCE_TOO_LARGE"
    assert analyses["web/vendor.min.js"] == "GENERATED_ARTIFACT"
    assert analyses["app/settings_values.py"] == "RECOGNIZABLE_GITHUB_TOKEN"
    assert analyses["web/broken.ts"] == "SYNTAX_RECOVERED"
    assert leaked == 0  # the screened secret never reaches chunks, facts or symbols

    entities, _ = stack.store.entity_page(run.id, EntityFilters(), 2000, 0)
    by_name = {(item.locator, item.name): item for item in entities}
    homoglyph = next(
        item for item in entities if item.locator == "app/unicode_ids.py" and item.kind == "symbol"
    )
    assert "MIXED_SCRIPT_IDENTIFIER" in homoglyph.limitations
    assert by_name[("web/broken.ts", "web/broken.ts")].context_coverage == "not_eligible"
    flood = [
        item
        for item in entities
        if item.locator.startswith("types/flood/") and item.kind == "symbol"
    ]
    assert len(flood) == 1000 and all("AMBIENT_DECLARATION" in item.limitations for item in flood)

    server = StubModelServer(obedient_attacker)
    try:
        service = generation_service(stack, server)
        keys = [
            by_name[("app/injected.py", "cleanup")].entity_key,
            by_name[("app/contradiction.py", "total")].entity_key,
            homoglyph.entity_key,
        ]
        plan = service.plan(stack.project.id, snapshot.id, PlanSelection(entity_keys=tuple(keys)))
        generation = service.execute(stack.project.id, snapshot.id, plan.id, plan.plan_digest, True)
        assert len(server.requests) == 3
        for request in server.requests:
            # Instructions stay server-owned; hostile text only ever travels as untrusted data.
            assert "IGNORE ALL" not in request["messages"][0]["content"]
        assert INJECTION in server.requests[0]["messages"][1]["content"]
        _, attempts = service.run(stack.project.id, snapshot.id, generation.id)
        codes = {code for attempt in attempts for code in attempt.rejection_codes}
        assert {"claim0:BEHAVIOR_WITHOUT_CODE_EVIDENCE", "claim1:LEXICON_VERDICT"} <= codes
        assert "claim0:UNGROUNDED_IDENTIFIER" in codes and "claim1:BIDI_CONTROL" in codes
        injected = service.entry(stack.project.id, snapshot.id, keys[0]).interpretation
        assert injected is not None
        assert [(c.text, c.basis_label) for c in injected.claims] == [
            ("`cleanup` is documented as being in maintenance mode.", "per_documentation")
        ]
        contradiction = service.entry(stack.project.id, snapshot.id, keys[1]).interpretation
        assert contradiction is not None and contradiction.status == "accepted_with_conflicts"
        assert [c.status for c in contradiction.claims] == ["conflicts_with_fact"]
        assert service.entry(stack.project.id, snapshot.id, keys[2]).interpretation is None
        with pg_engine.connect() as connection:
            retained = connection.execute(
                text("SELECT count(*) FROM semantic_generation_attempt_outcomes "
                     "WHERE retained_output IS NOT NULL")
            ).scalar_one()  # fmt: skip
        assert retained == 0  # raw model output is not retained by default
    finally:
        server.close()
