"""Focused independent-review reproductions and bounded correction contracts."""

from copy import deepcopy
from textwrap import dedent
from typing import Any
from uuid import uuid4

import pytest

from buildlens.application.impact import ImpactError, Operation, _status
from buildlens.application.impact_properties import compare_syntax, hypothetical
from buildlens.domain.source import digest
from buildlens.infrastructure.persistence.impact import PostgresImpactView
from buildlens.infrastructure.persistence.models import ImpactPreparationRecord
from buildlens.infrastructure.structural.impact import extract_impact


def compare(
    before: str, after: str, locator: str = "case.py"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    return compare_syntax(
        extract_impact(locator, dedent(before).encode()),
        extract_impact(locator, dedent(after).encode()),
        locator,
        "snapshot",
    )


@pytest.mark.parametrize("operator", ["and", "or"])
def test_short_circuit_effect_condition_is_explicitly_unresolved(operator: str) -> None:
    before = f"def effect(flag):\n    flag {operator} emit()\n"
    after = before.replace(f" {operator} ", " or " if operator == "and" else " and ")
    changes, findings, gaps = compare(before, after)
    assert any(row["property"] == "declaration_syntax" for row in changes)
    assert "SHORT_CIRCUIT_CONDITION_COMPARISON_UNAVAILABLE" in gaps
    assert _status(gaps) == "evidence_incomplete"
    assert not any(row["mechanism"] == "control" for row in findings)


@pytest.mark.parametrize(
    "source",
    [
        "async def f():\n    return {'a': True}\n",
        "class C:\n    def f(self):\n        return True\n",
        "def f():\n    return True\n",
    ],
)
def test_unmodeled_changed_body_cannot_be_complete(source: str) -> None:
    _, _, gaps = compare(source, source.replace("True", "False"))
    assert _status(gaps) == "evidence_incomplete"


def test_supported_signature_and_constant_short_circuit_controls() -> None:
    before = "def p(a=0):\n    return a\ndef c():\n    p()\n"
    _, findings, _ = compare(before, before.replace("a=0", "a"))
    assert [row["consequence"] for row in findings] == ["missing_required_argument"]
    before = "def p():\n    return {'a': True}\ndef c():\n    return False and p()['a']\n"
    _, findings, _ = compare(before, before.replace("'a': True", "'a': False"))
    assert findings == []


@pytest.mark.parametrize(
    "before,after",
    [
        ("def f(x: int):\n    return x\n", "def f(x: str):\n    return x\n"),
        ("@first\ndef f():\n    return 1\n", "@second\ndef f():\n    return 1\n"),
    ],
)
def test_uncompared_declaration_semantics_remain_unknown(before: str, after: str) -> None:
    _, _, gaps = compare(before, after)
    assert "DECLARATION_SEMANTICS_NOT_FULLY_COMPARED" in gaps
    assert _status(gaps) == "evidence_incomplete"


def test_promise_barrier_sync_consumer_and_unrelated_reads() -> None:
    source = """\
        async function p() { return {a: true, b: 1}; }
        function c() { return p().a; }
        async function awaited() { const x = await p(); return x.a; }
        function ignored() { p(); }
        function unrelated() { return p().b; }
    """
    _, findings, gaps = compare(source, source.replace("a: true", "a: false"), "case.ts")
    assert findings == []
    assert "ASYNC_RESULT_TRANSFER_UNAVAILABLE" in gaps
    sync = source.replace("async function p", "function p")
    _, findings, _ = compare(sync, sync.replace("a: true", "a: false"), "case.ts")
    assert [row["entity"] for row in findings] == ["c"]


@pytest.mark.parametrize("container", ["{x}", "[x]"])
def test_js_container_escape_stops_precise_field_transfer(container: str) -> None:
    source = (
        "function p() { return {a: true}; }\n"
        f"function c() {{ const x = p(); const box = {container}; mutate(box); return x.a; }}\n"
        "function direct() { const x = p(); return x.a; }\n"
    )
    _, findings, gaps = compare(source, source.replace("a: true", "a: false"), "case.ts")
    assert [row["entity"] for row in findings] == ["direct"]
    assert "CONTAINER_ALIAS_ESCAPE" in gaps


def preparation_record() -> ImpactPreparationRecord:
    row = ImpactPreparationRecord(
        id=uuid4(),
        project_id=uuid4(),
        snapshot_id=uuid4(),
        locator="case.py",
        fingerprint="a" * 64,
        contract="b" * 64,
        facts_digest=digest({"safe": True}),
        facts={"safe": True},
        source_identity={"artifact_id": str(uuid4()), "policy": "admitted.v1"},
    )
    row.input_seal_digest = PostgresImpactView._input_seal(row)
    return row


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", uuid4()),
        ("project_id", uuid4()),
        ("snapshot_id", uuid4()),
        ("locator", "other.py"),
        ("fingerprint", "c" * 64),
        ("contract", "d" * 64),
        ("source_identity", {"artifact_id": str(uuid4()), "policy": "excluded.v1"}),
        ("input_seal_digest", None),
    ],
)
def test_preparation_seal_detects_each_identity_edit(field: str, value: object) -> None:
    row = preparation_record()
    PostgresImpactView._preparation(row)
    setattr(row, field, value)
    with pytest.raises(ImpactError, match="IMPACT_PREPARATION_IDENTITY_CHANGED"):
        PostgresImpactView._preparation(row)


def test_legacy_preparation_is_never_promoted_to_a_sealed_identity() -> None:
    row = preparation_record()
    row.input_seal_digest = None
    row.source_identity = None
    original = deepcopy(row.facts)
    checked = PostgresImpactView._preparation(row)
    assert checked.input_seal is None and row.facts == original


def hypothesis(
    source: str, **operation: object
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    before = extract_impact("case.py", dedent(source).encode())
    candidate = hypothetical(before, [Operation.model_validate(operation).model_dump()])
    assert candidate is not None
    return compare_syntax(before, candidate, "case.py", "hypothesis")


@pytest.mark.parametrize("kind", ["remove_symbol", "rename_symbol"])
def test_symbol_hypothesis_retains_unchanged_callsite_exposure(kind: str) -> None:
    changes, findings, _ = hypothesis(
        "def p():\n    return 1\ndef c():\n    p()\n",
        kind=kind,
        target="p",
        **({"new_name": "renamed"} if kind == "rename_symbol" else {}),
    )
    assert any(row["entity"] == "p" and row["comparison"] == "removed" for row in changes)
    assert [(row["entity"], row["consequence"]) for row in findings] == [
        ("c", "call_target_declaration_removed")
    ]


API = """\
    from fastapi import FastAPI, Query
    from pydantic import BaseModel
    app = FastAPI()
    class Result(BaseModel):
        ready: bool = True
    @app.get('/items', response_model=Result)
    def items(limit: int = Query(10)):
        return {'ready': True}
"""


@pytest.mark.parametrize(
    "operation,property_name",
    [
        (
            {"kind": "remove_api_field", "family": "response", "member": "ready"},
            "api_response_contract",
        ),
        ({"kind": "require_api_field", "family": "query", "member": "limit"}, "api_query_contract"),
        (
            {"kind": "rename_api_field", "family": "response", "member": "ready", "new_name": "ok"},
            "api_response_contract",
        ),
        ({"kind": "set_endpoint_method", "value": "POST"}, "api_route_declaration"),
        ({"kind": "set_endpoint_path", "value": "/inventory"}, "api_route_declaration"),
    ],
)
def test_api_hypotheses_change_evidenced_contracts(
    operation: dict[str, Any], property_name: str
) -> None:
    changes, _, gaps = hypothesis(API, target="items", **operation)
    assert property_name in {row["property"] for row in changes}
    assert "HYPOTHESIS_API_CONTRACT_LOCAL_ONLY" in gaps


DB = """\
    from sqlalchemy.orm import DeclarativeBase, mapped_column
    class Base(DeclarativeBase):
        pass
    class Item(Base):
        __tablename__ = 'items'
        quantity = mapped_column(nullable=True)
"""


@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "remove_database_column"},
        {"kind": "set_database_constraint", "constraint": "nullable", "value": False},
    ],
)
def test_database_hypotheses_require_evidenced_model_and_column(operation: dict[str, Any]) -> None:
    changes, _, gaps = hypothesis(DB, target="Item", member="quantity", **operation)
    assert any(row["property"] == "orm_column_declaration" for row in changes)
    assert "ORM_INHERITED_AND_RUNTIME_SCHEMA_UNKNOWN" in gaps
    source = extract_impact("case.py", dedent(DB).encode())
    invalid = Operation(target="Item", member="absent", **operation).model_dump()
    assert hypothetical(source, [invalid]) is None


def test_configuration_default_is_conditional_and_retains_no_raw_value() -> None:
    changes, findings, gaps = hypothesis(
        "import os\ndef settings():\n    return os.getenv('CACHE_MODE', 'old')\n",
        kind="set_configuration_default",
        target="settings",
        member="CACHE_MODE",
        value="new-public-default",
    )
    assert changes[0]["property"] == "environment_default"
    assert findings[0]["consequence"] == "conditional_source_default_changed"
    assert "EFFECTIVE_CONFIGURATION_UNAVAILABLE" in gaps
    assert "new-public-default" not in repr((changes, findings))


def test_invalid_hypothesis_values_and_overlapping_writes_are_rejected() -> None:
    before = extract_impact("case.py", dedent(API).encode())
    op = {"kind": "set_endpoint_path", "target": "items", "value": "https://external.invalid"}
    assert hypothetical(before, [op]) is None
    op["value"] = "/different"
    assert hypothetical(before, [op, op]) is None
    assert (
        hypothetical(
            before,
            [
                {
                    "kind": "remove_api_field",
                    "target": "items",
                    "family": "body",
                    "member": "unknown",
                }
            ],
        )
        is None
    )


def test_new_operations_validate_common_baseline_and_candidate_name_collisions() -> None:
    source = extract_impact("case.py", dedent(API).encode())
    assert (
        hypothetical(
            source,
            [
                {
                    "kind": "rename_api_field",
                    "target": "items",
                    "family": "response",
                    "member": "ready",
                    "new_name": "ok",
                },
                {
                    "kind": "remove_api_field",
                    "target": "items",
                    "family": "response",
                    "member": "ok",
                },
            ],
        )
        is None
    )
    declarations = extract_impact("case.py", b"def p():\n    pass\ndef q():\n    pass\n")
    assert (
        hypothetical(
            declarations,
            [
                {"kind": "rename_symbol", "target": "p", "new_name": "same"},
                {"kind": "rename_symbol", "target": "q", "new_name": "same"},
            ],
        )
        is None
    )
