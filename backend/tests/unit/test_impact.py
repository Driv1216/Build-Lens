from dataclasses import replace
from textwrap import dedent
from typing import Any

import pytest

from buildlens.application.impact_properties import compare_syntax, hypothetical
from buildlens.domain.impact import CellKey, Frontier, Threshold, Transfer, integer_difference, walk
from buildlens.domain.impact_patch import PatchRejected, apply_patch, fingerprint, parse_patch
from buildlens.infrastructure.structural.impact import extract_impact

RESERVE = dedent("""\
    def reserve(qty, *, notify=False):
        if type(qty) is not int:
            raise ValueError()
        if qty > 10:
            record(qty)
        return {"accepted": True, "quantity": qty}

    def consume():
        value = reserve(7, notify=True)
        if value["accepted"]:
            show()

    def discard():
        reserve(2)

    def quantity():
        return reserve(12, notify=False)["quantity"]

    def overwritten():
        value = reserve(7, notify=False)
        value = {}
        return value["accepted"]
""")


def _compare(after: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    return compare_syntax(
        extract_impact("shop.py", RESERVE.encode()),
        extract_impact("shop.py", after.encode()),
        "shop.py",
        "candidate",
    )


def test_signature_return_and_side_effect_changes_have_distinct_findings() -> None:
    _, findings, _ = _compare(RESERVE.replace("notify=False):", "notify):"))
    assert [(row["entity"], row["mechanism"]) for row in findings] == [("discard", "compatibility")]

    _, findings, _ = _compare(RESERVE.replace('"accepted": True', '"accepted": False'))
    assert [(row["entity"], row["mechanism"]) for row in findings] == [("consume", "data")]

    changes, findings, _ = _compare(RESERVE.replace("qty > 10", "qty > 5"))
    assert [(row["entity"], row["mechanism"]) for row in findings] == [("record", "control")]
    assert findings[0]["regions"] == [{"min": 6, "max": 10, "sample": 6, "old": False, "new": True}]
    assert not any(row["property"] == "return_field" for row in changes)


def test_combined_signature_and_callsite_repair_does_not_emit_mismatch() -> None:
    _, findings, _ = _compare(
        RESERVE.replace("notify=False):", "notify):").replace(
            "reserve(2)", "reserve(2, notify=False)"
        )
    )
    assert findings == []


def test_function_body_limit_preserves_signature_and_qualifies_body_comparisons() -> None:
    declaration = "def validate(*, strict=False):\n"
    source = declaration + "    touch()\n" * 520 + "    return {'accepted': True}\n"
    before = extract_impact("validation.py", source.encode())
    after = extract_impact("validation.py", b"def validate(*, strict):\n    return {}\n")
    simulated = hypothetical(
        before,
        [
            {"kind": "require_parameter", "target": "validate", "member": "strict"},
        ],
    )
    assert simulated is not None
    for projection in (after, simulated):
        changes, findings, gaps = compare_syntax(before, projection, "validation.py", "snapshot")
        assert [row["property"] for row in changes] == ["signature"]
        assert changes[0]["after"][0]["required"] is True
        assert "FUNCTION_BODY_SYNTAX_LIMIT" in gaps
        assert findings == []


def test_same_witness_is_not_required_for_distinct_late_route_conditions() -> None:
    root = CellKey("a", "old", "root", "data", "field")
    second = replace(root, origin="b")
    edges = {
        "root": (
            Transfer("root-a", "shared", "data", ("x",)),
            Transfer("root-b", "indirect", "data", ("!x",)),
        ),
        "indirect": (Transfer("indirect", "shared", "data"),),
        "shared": (
            Transfer("cycle", "shared", "data"),
            Transfer("target", "target", "data", ("!x",), ("opaque branch",)),
        ),
    }
    result = walk([root, second], lambda key: Frontier(edges.get(key.subject, ())), lambda: False)
    for seed in (root, second):
        target = replace(seed, subject="target")
        assert result.cells[target].terms == frozenset({frozenset({"!x"})})
        assert result.cells[target].gaps == frozenset({"opaque branch"})
        assert result.supporting[target].refuted
        assert result.supporting[target].records == (
            (frozenset({"!x"}), ("root-b", "indirect", "target")),
        )


def test_display_cap_does_not_drop_analytic_routes_and_recursion_stops() -> None:
    root = CellKey("a", "old", "root", "data", "x")

    def frontier(key: CellKey) -> Frontier:
        if key.subject == "root":
            return Frontier(
                tuple(Transfer(str(i), "merge", "data", (f"atom{i}",)) for i in range(6))
            )
        if key.subject == "merge":
            return Frontier((Transfer("consume", "consume", "data"),))
        return Frontier((Transfer("recur", "consume", "data", push="frame"),))

    result = walk([root], frontier, lambda: False)
    merged = result.cells[replace(root, subject="merge")].terms
    assert merged is not None and len(merged) == 6
    assert len(result.supporting[replace(root, subject="merge")].records) == 3
    assert "CONTEXT_LIMIT" in result.gaps


def test_return_to_an_unrelated_callsite_is_blocked() -> None:
    root = CellKey("a", "new", "scope", "data", "x", ("caller1",))
    result = walk(
        [root],
        lambda key: (
            Frontier(
                (
                    Transfer("bad", "wrong", "data", pop="caller2"),
                    Transfer("good", "right", "data", pop="caller1"),
                )
            )
            if key.subject == "scope"
            else Frontier()
        ),
        lambda: False,
    )
    assert {key.subject for key in result.cells} == {"scope", "right"}


def test_integer_regions_are_clamped_and_handle_zero() -> None:
    assert integer_difference(Threshold("<", 0), Threshold("<", 1), (-2, 2)) == [
        {"min": 0, "max": 0, "sample": 0, "old": False, "new": True},
    ]
    assert integer_difference(Threshold(">", 5), Threshold(">", 10), (8, 9)) == [
        {"min": 8, "max": 9, "sample": 8, "old": True, "new": False},
    ]


def test_patch_requires_exact_bytes_and_context() -> None:
    patch = "\n".join(
        [
            "diff --git a/f.py b/f.py",
            "--- a/f.py",
            "+++ b/f.py",
            "@@ -1,2 +1,2 @@",
            " def f():",
            "-    return True",
            "+    return False",
            "",
        ]
    )
    (item,) = parse_patch(patch)
    raw = b"def f():\n    return True\n"
    assert apply_patch(item, raw, fingerprint(raw)) == b"def f():\n    return False\n"
    with pytest.raises(PatchRejected, match="PATCH_BASELINE_MISMATCH"):
        apply_patch(item, raw + b"\n", fingerprint(raw))
    other = b"def g():\n    return True\n"
    with pytest.raises(PatchRejected, match="PATCH_CONTEXT_MISMATCH"):
        apply_patch(item, other, fingerprint(other))
    with pytest.raises(PatchRejected, match="PATCH_UNSUPPORTED_PATH"):
        parse_patch(patch.replace("f.py", "../f.py"))


def test_literal_route_column_and_default_changes_stay_source_qualified() -> None:
    old = dedent("""\
        import os
        from fastapi import APIRouter
        from sqlalchemy.orm import mapped_column
        router = APIRouter(prefix="/api")

        @router.get("/items", status_code=200)
        def list_items():
            return {"cached": os.getenv("ITEM_CACHE", 1)}

        class Item:
            __tablename__ = "items"
            quantity = mapped_column(nullable=True)
    """)
    new = old.replace('"/items"', '"/inventory"').replace('CACHE", 1', 'CACHE", 2')
    new = new.replace("nullable=True", "nullable=False")
    changes, findings, _ = compare_syntax(
        extract_impact("catalog.py", old.encode()),
        extract_impact("catalog.py", new.encode()),
        "catalog.py",
        "snapshot",
    )
    indexed = {entry["property"]: entry for entry in changes}
    assert indexed["api_route_declaration"]["after"]["mount"] == "unverified_local_declaration"
    assert indexed["orm_column_declaration"]["after"]["nullable"]["value"] is False
    assert indexed["environment_default"]["after"]["condition"] == "no_environment_override"
    assert [row["consequence"] for row in findings] == ["conditional_source_default_changed"]
    assert "EFFECTIVE_OVERRIDES_UNKNOWN" in findings[0]["witness"]["conditions"]
    renamed = new.replace("ITEM_CACHE", "ITEM_CACHE_LIMIT")
    changes, _, _ = compare_syntax(
        extract_impact("catalog.py", new.encode()),
        extract_impact("catalog.py", renamed.encode()),
        "catalog.py",
        "snapshot",
    )
    assert any(
        row["property"] == "environment_key" and row["before"]["key"] == "ITEM_CACHE"
        for row in changes
    )


def test_literal_query_and_response_model_fields_have_independent_comparison() -> None:
    old = dedent("""\
        from fastapi import APIRouter, Query
        from pydantic import BaseModel
        router = APIRouter()

        class Result(BaseModel):
            count: int
            ready: bool = True

        @router.get('/items', response_model=Result)
        def endpoint(limit: int = Query(10, alias='page_size')):
            return {'count': limit, 'ready': True}
    """)
    new = old.replace("    count: int\n", "").replace("Query(10,", "Query(...,")
    before = extract_impact("api.py", old.encode())
    after = extract_impact("api.py", new.encode())
    changes, findings, _ = compare_syntax(before, after, "api.py", "snapshot")
    wire = [row for row in changes if row["property"].startswith("api_")]
    assert [(row["property"], row["field"], row["comparison"]) for row in wire] == [
        ("api_query_contract", "page_size", "modified"),
        ("api_response_contract", "count", "removed"),
    ]
    assert wire[0]["before"]["required"] is False and wire[0]["after"]["required"] is True
    assert wire[1]["before"]["type"] == "int"
    assert findings == []

    unknown = new.replace("Query(..., alias='page_size')", "Query(alias=resolve_alias())")
    changes, _, gaps = compare_syntax(
        before, extract_impact("api.py", unknown.encode()), "api.py", "snapshot"
    )
    assert "API_QUERY_CONTRACT_INCOMPARABLE" in gaps
    assert not any(row["property"] == "api_query_contract" for row in changes)


def test_body_contract_and_response_assertions_keep_test_exposure_separate() -> None:
    old = dedent("""\
        import pytest
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from pydantic import BaseModel

        app = FastAPI()
        app.dependency_overrides = {}

        class Payload(BaseModel):
            amount: int

        class Result(BaseModel):
            identifier: int
            active: bool

        @app.post('/items', response_model=Result)
        def submit(item: Payload):
            return {'identifier': item.amount, 'active': True}

        @pytest.fixture
        def web():
            return TestClient(app)

        def test_response(web):
            res = web.post('/items', json={'amount': 1})
            result = res.json()
            assert result['identifier'] == 1

        def test_exposure(web):
            res = web.post('/items', json={'amount': 1})
            res = object()
            assert res.json()['identifier'] == 1

        def test_unrelated(web):
            web.post('/another', json={'amount': 1})
    """)
    new = old.replace("    amount: int\n", "    amount: int\n    currency: str\n")
    new = new.replace("    identifier: int\n", "")
    changes, findings, _ = compare_syntax(
        extract_impact("api.py", old.encode()),
        extract_impact("api.py", new.encode()),
        "api.py",
        "snapshot",
    )
    fields = {
        row.get("field"): row["property"] for row in changes if row["property"].startswith("api_")
    }
    assert fields == {"currency": "api_body_contract", "identifier": "api_response_contract"}
    assert {row["entity"] for row in findings if row["test_assertions"]} == {"test_response"}
    assert {
        row["entity"]
        for row in findings
        if row["consequence"] == "test_supplies_body_without_new_required_field"
    } == {
        "test_response",
        "test_exposure",
    }
    assert not any(row["entity"] == "test_unrelated" for row in findings)
    assert all(row["witness"]["override_declared"] for row in findings)


def test_scoped_database_column_reads_writes_and_transaction_order() -> None:
    old = dedent("""\
        from sqlalchemy import select
        from sqlalchemy.orm import DeclarativeBase, Session, mapped_column

        class Base(DeclarativeBase):
            pass

        class Item(Base):
            __tablename__ = 'items'
            amount = mapped_column(nullable=True)
            label = mapped_column(nullable=False)

        def write(session: Session):
            session.add(Item(amount=3))
            session.commit()
            return None

        def read(session: Session):
            return session.scalars(select(Item.amount))

        def another_column(session: Session):
            return session.scalars(select(Item.label))
    """)
    new = old.replace("nullable=True", "nullable=False").replace(
        "    session.commit()\n    return None",
        "    return None\n    session.commit()",
    )
    changes, findings, _ = compare_syntax(
        extract_impact("models.py", old.encode()),
        extract_impact("models.py", new.encode()),
        "models.py",
        "snapshot",
    )
    assert {row["entity"] for row in findings if row["consequence"] == "mapped_column_read"} == {
        "read"
    }
    assert {
        row["entity"] for row in findings if row["consequence"] == "mapped_column_write_input"
    } == {"write"}
    assert any(row["consequence"] == "declared_session_operation_order_changed" for row in findings)
    order = next(row for row in changes if row["property"] == "transaction_sequence")
    assert [step["operation"] for step in order["after"]] == ["add", "return"]
    removed = new.replace("    amount = mapped_column(nullable=False)\n", "")
    changes, findings, _ = compare_syntax(
        extract_impact("models.py", old.encode()),
        extract_impact("models.py", removed.encode()),
        "models.py",
        "snapshot",
    )
    assert any(
        row["entity"] == "items.amount" and row["comparison"] == "removed" for row in changes
    )
    assert {row["side"] for row in findings if row["consequence"] == "mapped_column_read"} == {
        "old"
    }


def test_exact_framework_pin_comparison_leaves_effective_version_unresolved() -> None:
    old = b'[project]\ndependencies = ["fastapi==0.115.0", "starlette>=0.9", "other==2.1"]\n'
    new = old.replace(b"0.115.0", b"0.121.0")
    changes, _, _ = compare_syntax(
        extract_impact("pyproject.toml", old),
        extract_impact("pyproject.toml", new),
        "pyproject.toml",
        "snapshot",
    )
    assert [item["entity"] for item in changes] == ["fastapi"]
    assert changes[0]["after"] == {
        "declared_version": "0.121.0",
        "group": "dependencies",
        "effective_version": "unresolved",
    }


def test_integer_literal_test_is_exposure_with_matching_input() -> None:
    old = (
        RESERVE
        + "\n\ndef test_hits_region():\n    reserve(7)\n\ndef test_outside():\n    reserve(12)\n"
    )
    new = old.replace("qty > 10", "qty > 5")
    _, findings, _ = compare_syntax(
        extract_impact("shop.py", old.encode()),
        extract_impact("shop.py", new.encode()),
        "shop.py",
        "snapshot",
    )
    hits = [row for row in findings if row.get("test_candidate")]
    assert [(row["entity"], row["matching_input"]) for row in hits] == [("test_hits_region", 7)]
    assert "TEST_EXECUTION_UNMEASURED" in hits[0]["witness"]["conditions"]


def test_unknown_alias_escape_blocks_a_later_field_claim() -> None:
    alias = "    value = reserve(7, notify=True)\n"
    before = RESERVE.replace(alias, alias + "    external(value)\n")
    changes, findings, _ = compare_syntax(
        extract_impact("shop.py", before.encode()),
        extract_impact("shop.py", before.replace('"accepted": True', '"accepted": False').encode()),
        "shop.py",
        "candidate",
    )
    assert any(change["property"] == "return_field" for change in changes)
    assert findings == []


def test_field_transfer_respects_completion_short_circuit_and_opaque_evaluation() -> None:
    before = RESERVE + dedent("""\

        def surviving_branch(flag):
            if flag:
                raise ValueError()
            else:
                value = reserve(2)
            return value['accepted']

        def exits_both_ways(flag):
            value = reserve(2)
            if flag:
                raise ValueError()
            else:
                return None
            return value['accepted']

        def short_circuit():
            value = reserve(2)
            return False and value['accepted']

        def opaque_alias():
            value = reserve(2)
            external(value) + 1
            return value['accepted']

        def sometimes_returns(flag):
            if flag:
                return reserve(2)

        def depends_on_partial_return():
            return sometimes_returns(True)['accepted']
    """)
    _, findings, gaps = compare_syntax(
        extract_impact("shop.py", before.encode()),
        extract_impact("shop.py", before.replace('"accepted": True', '"accepted": False').encode()),
        "shop.py",
        "snapshot",
    )
    assert "surviving_branch" in {row["entity"] for row in findings}
    assert not {"exits_both_ways", "short_circuit", "opaque_alias", "depends_on_partial_return"} & {
        row["entity"] for row in findings
    }
    surviving = next(row for row in findings if row["entity"] == "surviving_branch")
    assert surviving["witness"]["conditional_read"] is True


def test_integer_regions_require_a_continuing_unmodified_formal_input() -> None:
    def inspect(
        before: str, after: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
        return compare_syntax(
            extract_impact("shop.py", before.encode()),
            extract_impact("shop.py", after.encode()),
            "shop.py",
            "snapshot",
        )

    numeric = RESERVE.replace("qty > 10", "qty > -2")
    _, different, _ = inspect(numeric, numeric.replace("qty > -2", "qty > -3"))
    assert different[0]["regions"] == [
        {"min": -2, "max": -2, "sample": -2, "old": False, "new": True}
    ]

    rebound = numeric.replace("    if qty > -2:", "    qty = 1\n    if qty > -2:")
    _, findings, _ = inspect(rebound, rebound.replace("qty > -2", "qty > -3"))
    assert not any(row["mechanism"] == "control" for row in findings)

    moved = numeric.replace("reserve(qty,", "reserve(extra, qty,").replace("qty > -2", "qty > -3")
    _, findings, gaps = inspect(numeric, moved)
    assert "INTEGER_INPUT_BINDING_INCOMPARABLE" in gaps
    assert not any(row["mechanism"] == "control" for row in findings)


def test_assertion_relevance_checks_property_and_binding() -> None:
    before = RESERVE + dedent("""\

        def test_acceptance():
            assert reserve(7, notify=True)["accepted"] is True

        def test_quantity():
            assert reserve(7, notify=True)["quantity"] == 7
    """)
    _, findings, _ = compare_syntax(
        extract_impact("shop.py", before.encode()),
        extract_impact("shop.py", before.replace('"accepted": True', '"accepted": False').encode()),
        "shop.py",
        "candidate",
    )
    assert [item["entity"] for item in findings if item["test_assertions"]] == ["test_acceptance"]


def test_http_rejects_unconfirmed_hypothesis_and_does_not_echo_input() -> None:
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from buildlens.api.main import create_app

    route = f"/api/v1/projects/{uuid4()}/impact/analyses"
    with TestClient(create_app()) as client:
        payload = {
            "mode": "hypothesis",
            "before": {"snapshot_id": str(uuid4())},
            "locators": ["reserve.py"],
            "operations": [
                {"kind": "require_parameter", "target": "privateCandidate", "member": "notify"}
            ],
        }
        response = client.post(route, json=payload)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "IMPACT_INVALID_REQUEST"
        assert "privateCandidate" not in response.text
        assert client.post(route, content="{}").status_code == 415


def test_return_forwarding_keeps_only_the_relevant_invocation() -> None:
    before = RESERVE + dedent("""\

        def forward():
            return reserve(7, notify=True)

        def connected():
            return forward()["accepted"]

        def ignored():
            forward()
    """)
    _, findings, _ = compare_syntax(
        extract_impact("shop.py", before.encode()),
        extract_impact("shop.py", before.replace('"accepted": True', '"accepted": False').encode()),
        "shop.py",
        "candidate",
    )
    indirect = [item for item in findings if item["distance"] == "indirect"]
    assert [item["entity"] for item in indirect] == ["connected"]
    assert indirect[0]["witness"]["forwarding"][0]["caller"] == "forward"


def test_typescript_field_comparison_follows_direct_uses() -> None:
    before = dedent("""\
        export function reserve() {
            return {accepted: true, quantity: 4};
        }
        function consume() {
            const result = reserve();
            if (result.accepted) show();
        }
        function discard() {
            reserve();
        }
    """)
    after = before.replace("accepted: true", "accepted: false")
    _, findings, _ = compare_syntax(
        extract_impact("shop.ts", before.encode()),
        extract_impact("shop.ts", after.encode()),
        "shop.ts",
        "candidate",
    )
    assert [(item["entity"], item["mechanism"]) for item in findings] == [("consume", "data")]
