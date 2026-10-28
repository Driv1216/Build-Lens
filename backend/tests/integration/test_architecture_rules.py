"""Stage 14 Milestone C F-rules: owner-declared intent, conformance outcomes and the API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from buildlens.api.architecture_rule_routes import IntentRuleSetResponse
from buildlens.api.main import create_app
from buildlens.application.architecture_queries import ArchitectureQueries
from buildlens.application.architecture_rules import ArchitectureRulesService
from buildlens.core.config import Settings
from buildlens.domain.architecture_rules import RuleValidationError, glob_expression, validate_rules
from buildlens.infrastructure.persistence.architecture_rules import SqlAlchemyArchitectureRuleStore
from tests.integration.test_architecture import architecture, prepared

pytestmark = pytest.mark.integration

RULES: list[dict[str, Any]] = [
    {"name": "api must not import db", "kind": "forbid", "from": ["app/api.py"], "to": ["app/**"]},
    {"name": "alias file", "kind": "forbid", "from": ["web/alias.ts"], "to": ["app/**"]},
    {"name": "nothing", "kind": "forbid", "from": ["nowhere/**"], "to": ["app/**"]},
    {"name": "api above db", "kind": "layers", "layers": [["app/api.py"], ["app/db.py"]]},
    {"name": "db above api", "kind": "layers", "layers": [["app/db.py"], ["module:app/api.py"]]},
    {"name": "overlap", "kind": "layers", "layers": [["app/*.py"], ["app/db.py"]]},
    {"name": "type level only", "kind": "forbid", "from": ["app/api.py"], "to": ["app/**"],
     "classes": ["type_level"]},
]  # fmt: skip


def test_validation_is_strict_and_globs_are_not_regex() -> None:
    with pytest.raises(RuleValidationError, match="Unknown rule fields"):
        validate_rules([{"name": "x", "kind": "forbid", "from": ["a"], "to": ["b"], "tier": "x"}])
    with pytest.raises(RuleValidationError, match="glob"):
        validate_rules([{"name": "x", "kind": "forbid", "from": ["app/(a|b)"], "to": ["b"]}])
    with pytest.raises(RuleValidationError, match="unique"):
        validate_rules([RULES[0], RULES[0]])
    with pytest.raises(RuleValidationError, match="class set"):
        validate_rules([{**RULES[0], "classes": "S9"}])
    assert glob_expression("app/**/x.py").match("app/x.py")
    assert glob_expression("app/**/x.py").match("app/a/b/x.py")
    assert not glob_expression("app/*.py").match("app/a/b.py")
    assert not glob_expression("a.b").match("aXb")  # '.' is literal


def test_conformance_outcomes_with_witnesses_and_gates(pg_engine: Engine, tmp_path: Path) -> None:
    stack, snapshot = prepared(pg_engine, tmp_path, "Rules")
    architecture(stack).build(stack.project.id, snapshot.id)
    service = ArchitectureRulesService(
        SqlAlchemyArchitectureRuleStore(stack.factory), ArchitectureQueries(architecture(stack))
    )
    first = service.declare(stack.project.id, RULES)
    evaluated = service.evaluate(stack.project.id, snapshot.id, None, None)
    outcomes = {item.rule.name: item for item in evaluated.results}
    forbidden = outcomes["api must not import db"]
    assert forbidden.outcome == "not_satisfied"
    assert [e.key for e in forbidden.witnesses] == [
        "imports|module:app/api.py|module:app/db.py|resolved|unconditional"
    ]
    assert forbidden.witness_min_tier == "derived"
    # An alias import leaving the selected file blocks a "satisfied" claim.
    alias = outcomes["alias file"]
    assert alias.outcome == "not_determinable" and alias.coverage_qualifier == (
        "UNRESOLVED_IMPORTS",
    )
    assert outcomes["nothing"].outcome == "selector_matches_nothing"
    assert outcomes["api above db"].outcome == "satisfied_in_observed_graph"
    assert outcomes["db above api"].outcome == "not_satisfied"
    assert outcomes["overlap"].coverage_qualifier == ("SELECTOR_OVERLAP",)
    assert outcomes["type level only"].outcome == "satisfied_in_observed_graph"
    # Versions are append-only and results are keyed by graph and rule-set digests.
    second = service.declare(stack.project.id, RULES[:1])
    assert second.version == first.version + 1
    assert service.rule_set(stack.project.id, first.version).digest == first.digest
    again = service.evaluate(stack.project.id, snapshot.id, None, first.version)
    assert again.result_key == evaluated.result_key
    latest = service.evaluate(stack.project.id, snapshot.id, None, None)
    assert latest.result_key != evaluated.result_key and len(latest.results) == 1


def test_http_contract_keeps_intent_without_tiers(pg_engine: Engine, tmp_path: Path) -> None:
    assert "tier" not in IntentRuleSetResponse.model_fields  # intent is not evidence (C-4)
    stack, snapshot = prepared(pg_engine, tmp_path, "Rules API")
    architecture(stack).build(stack.project.id, snapshot.id)
    app = create_app(Settings(database_url=pg_engine.url.render_as_string(hide_password=False)))
    base = f"/api/v1/projects/{stack.project.id}"
    with TestClient(app) as client:
        assert client.get(base + "/architecture-rules").status_code == 404
        results = f"{base}/snapshots/{snapshot.id}/architecture/rule-results"
        missing = client.get(results)
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "ARCHITECTURE_RULES_NOT_FOUND"
        not_json = client.post(base + "/architecture-rules", content="rules")
        assert not_json.status_code == 415
        bad = client.post(
            base + "/architecture-rules",
            json={"rules": [{**RULES[0], "from": ["app/(a|b)"]}]},
        )
        assert (
            bad.status_code == 422 and bad.json()["detail"]["code"] == "ARCHITECTURE_RULES_INVALID"
        )
        extra = client.post(base + "/architecture-rules", json={"rules": RULES, "source": "repo"})
        assert extra.status_code == 422
        created = client.post(base + "/architecture-rules", json={"rules": RULES[:1]})
        assert created.status_code == 201 and created.json()["version"] == 1
        assert "tier" not in created.json()
        checked = client.get(results).json()
        assert checked["verification"] == "verified"
        assert checked["results"][0]["outcome"] == "not_satisfied"
        assert checked["results"][0]["witnesses"][0]["tier"] == "derived"
        assert client.get(base + "/architecture-rules/versions").json()["items"][0]["version"] == 1
