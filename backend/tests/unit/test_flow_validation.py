"""Stage 15 Milestone E: ``flow-validator.v1`` against a designed package (attacker claims)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from buildlens.application.flow_validation import validate

CAVEAT = "HTTP_LINK_INDEPENDENT_EVALUATION_PENDING"


def _step(sid: str, src: str, dst: str | None, relation: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": sid,
        "from": src,
        "to": dst,
        "relation": relation,
        "tier": extra.pop("tier", "derived"),
        "conditions": extra.pop("conditions", []),
        "reachability": "reachable_in_model",
        "path_condition": {"terms": [], "approximate": False},
        "feasibility": "consistent_in_fragment",
        "lane": extra.pop("lane", "main"),
        "segment": None,
        "attributes": extra.pop("attributes", {}),
    }


PACKAGE: dict[str, Any] = {
    "version": "flow-package.v1",
    "trace_digest": "d" * 64,
    "direction": "forward",
    "status": "incomplete_evidence",
    "truncated": False,
    "path_tier": "inferred",
    "caveats": [CAVEAT],
    "levels": {},
    "lanes": [
        {"id": "main", "order": "SEQUENTIAL_BY_CFG_WITHIN_A_SCOPE"},
        {"id": "fastapi_background", "order": "ORDER_NOT_DETERMINED"},
        {"id": "framework_response_send:teardown", "order": "ORDERED_BY_FASTAPI_VERSION_TABLE"},
    ],
    "hops": [
        {
            "id": "H0",
            "label": "createItem",
            "kind": "function",
            "layer": "Frontend",
            "lane": "main",
            "locator": "web/items.ts",
        },
        {
            "id": "H1",
            "label": "POST",
            "kind": "http_request",
            "layer": "HTTP",
            "lane": "main",
            "locator": "web/items.ts",
        },
        {
            "id": "H2",
            "label": "create_item",
            "kind": "function",
            "layer": "Route",
            "lane": "main",
            "locator": "app/api.py",
        },
        {
            "id": "H3",
            "label": "add()",
            "kind": "db_operation",
            "layer": "Data",
            "lane": "main",
            "locator": "app/service.py",
        },
        {
            "id": "H4",
            "label": "201 (return)",
            "kind": "response_outcome",
            "layer": "Route",
            "lane": "main",
            "locator": "app/api.py",
        },
        {
            "id": "H5",
            "label": "? (unhandled)",
            "kind": "response_outcome",
            "layer": "Route",
            "lane": "main",
            "locator": "app/api.py",
        },
        {
            "id": "H6",
            "label": "notify",
            "kind": "function",
            "layer": "Service",
            "lane": "fastapi_background",
            "locator": "app/api.py",
        },
        {
            "id": "H7",
            "label": "get_db",
            "kind": "function",
            "layer": "Dependencies",
            "lane": "framework_response_send:teardown",
            "locator": "app/db.py",
        },
    ],
    "steps": [
        _step("S0", "H0", "H1", "issues_request"),
        _step("S1", "H1", "H2", "calls_endpoint", tier="inferred", conditions=[CAVEAT]),
        _step("S2", "H2", "H3", "performs", tier="inferred"),
        _step("S3", "H2", "H4", "exits_with", attributes={"exit": "return"}),
        _step("S4", "H2", "H5", "exits_with", tier="inferred", attributes={"exit": "unhandled"}),
        _step(
            "S5",
            "H6",
            None,
            "unresolved",
            tier="unknown",
            lane="fastapi_background",
            conditions=["BUILTIN"],
        ),
        _step(
            "S6",
            "H7",
            None,
            "unresolved",
            tier="unknown",
            lane="framework_response_send:teardown",
            conditions=["BUILTIN"],
        ),
    ],
    "gaps": [{"step": "S5", "reasons": ["RECEIVER_TYPE_UNKNOWN"]}],
    "effects": [
        {"step": "S2", "class": "stage", "tables": ["db_table:items"], "state": "commit_reached"}
    ],
    "excerpts": [
        {
            "id": "E1",
            "hop": "H2",
            "locator": "app/api.py",
            "start_line": 10,
            "text": "def create_item(name):\n    item = svc.create(name)\n    return item\n",
        },
    ],
    "hop_labels": {},
}


def _check(*claims: dict[str, Any], unknowns: list[dict[str, Any]] | None = None) -> Any:
    return validate(json.dumps({"claims": list(claims), "unknowns": unknowns or []}), PACKAGE)


def claim(
    text: str, steps: list[str], level: str = "path", evidence: list[Any] | None = None
) -> dict[str, Any]:
    return {"level": level, "text": text, "steps": steps, "evidence": evidence or []}


HONEST = [
    claim(
        "createItem issues a request that may reach create_item, an inferred link pending independent evaluation.",  # noqa: E501
        ["S0", "S1"],
    ),
    claim(
        "On this path create_item reaches a commit for the items table; the commit may still raise.",  # noqa: E501
        ["S2"],
        "effect",
    ),
    claim("When create_item returns normally the outcome is 201.", ["S3"], "path"),
    claim("What notify does with its argument is unknown here.", ["S5"], "resolution"),
    claim(
        "create_item is linked to its service call.",
        ["S2"],
        "summary",
        [{"id": "E1", "quote": "item = svc.create(name)"}],
    ),
]


def test_honest_claims_pass() -> None:
    result = _check(
        *HONEST, unknowns=[{"text": "The status of an unhandled exception is not determined."}]
    )
    assert result.status == "accepted"
    assert len(result.claims) == len(HONEST) and result.rejection_codes == ()
    assert result.claims[4].citations[0]["id"] == "E1"


@pytest.mark.parametrize(
    ("attacker", "code"),
    [
        (claim("createItem may reach delete_everything.", ["S0"]), "UNGROUNDED_IDENTIFIER"),
        (claim("createItem may reach create_item.", ["S99"]), "EDGE_NOT_IN_TRACE"),
        (claim("createItem is linked to notify.", ["S0"]), "EDGE_NOT_IN_TRACE"),
        (claim("create_item executes its service call.", ["S2"]), "LEVEL_OVERSTATED"),
        (claim("The request is called by createItem.", ["S0"]), "LEVEL_OVERSTATED"),
        (claim("The request definitely reaches create_item.", ["S1"]), "TIER_OVERSTATED"),
        (claim("createItem calls create_item over HTTP.", ["S0", "S1"]), "TIER_OVERSTATED"),
        (claim("The get_db teardown happens before notify.", ["S5", "S6"]), "ORDER_NOT_IN_TRACE"),
        (claim("notify prints the item name.", ["S5"]), "BEHAVIOR_IN_GAP"),
        (claim("The new item is saved to the items table.", ["S2"], "effect"), "COMMIT_OVERSTATED"),
        (claim("create_item commits the item.", ["S2"], "effect"), "COMMIT_OVERSTATED"),
        (
            claim("create_item reaches a commit when it returns 201.", ["S3"], "effect"),
            "COMMIT_OVERSTATED",
        ),
        (claim("After the request the page re-renders.", ["S0"]), "RENDER_OVERSTATED"),
        (claim("An unhandled error makes create_item return 500.", ["S4"]), "STATUS_OVERSTATED"),
        (claim("create_item returns 404 when the item is missing.", ["S3"]), "STATUS_OVERSTATED"),
        (claim("create_item handles names safely.", ["S3"]), "LEXICON_VERDICT"),
        (claim("create_item returns the item‮.", ["S3"]), "BIDI_CONTROL"),
        (
            claim(
                "create_item uses its service.",
                ["S2"],
                "summary",
                [{"id": "E9", "quote": "item = svc.create(name)"}],
            ),
            "EVIDENCE_UNKNOWN",
        ),
        (
            claim(
                "create_item uses its service.",
                ["S2"],
                "summary",
                [{"id": "E1", "quote": "svc.delete(name)"}],
            ),
            "QUOTE_NOT_FOUND",
        ),
        ({"level": "path", "text": "x", "steps": ["S0"]}, "CLAIM_STRUCTURE"),
        (claim("too many steps", ["S0", "S1", "S2", "S3", "S4", "S5", "S6"]), "CLAIM_STRUCTURE"),
    ],
)
def test_attacker_claims_are_rejected(attacker: dict[str, Any], code: str) -> None:
    result = _check(*HONEST, attacker)
    assert result.status == "accepted_with_rejections"
    assert len(result.claims) == len(HONEST)  # the attacker claim is dropped, honest ones stay
    assert result.rejection_codes == (f"claim{len(HONEST)}:{code}",)


def test_whole_output_shape_and_empty_results() -> None:
    assert validate("not json", PACKAGE).rejection_codes == ("OUTPUT_INVALID",)
    assert (
        validate(json.dumps({"claims": [], "unknowns": [], "extra": 1}), PACKAGE).status
        == "rejected"
    )
    assert _check().status == "insufficient_evidence"
    only_bad = _check(claim("create_item executes.", ["S2"]))
    assert only_bad.status == "rejected" and only_bad.claims == ()
    bad_unknown = _check(*HONEST, unknowns=[{"text": "First. Second."}])
    assert bad_unknown.rejection_codes == ("unknown0:UNKNOWN_NOT_ONE_SENTENCE",)
