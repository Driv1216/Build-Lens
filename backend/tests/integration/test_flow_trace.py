"""Stage 15 Milestone C: FlowTrace oracles against real PostgreSQL and the real worker.

Designed whole-path, response, branch and feasibility oracles; byte-identical recomputation
(INV-7); no structural witness presented as execution; opaque atoms never refuted; absence
gating; pinning with re-verification; zero source reads (INV-1); the HTTP contract.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from buildlens.api.main import create_app
from buildlens.application.flow import FlowIntegrityError, FlowStaleError
from buildlens.application.flow_trace_service import FlowTraceService, TraceGraphCache
from buildlens.core.config import Settings
from buildlens.domain.flow import HTTP_CAVEAT
from buildlens.domain.flow_trace import TraceQuery
from tests.flow_support import FLOW_REPOSITORY, TRACE_REPOSITORY, FlowStack
from tests.integration.test_flow_model import Graph
from tests.semantic_support import write

pytestmark = pytest.mark.integration


def _service(stack: FlowStack) -> FlowTraceService:
    return FlowTraceService(stack.flow, stack.store, cache=TraceGraphCache())


def _trace(stack: FlowStack, entry: str, direction: str = "forward") -> dict[str, Any]:
    return _service(stack).trace(
        stack.project_id,
        stack.snapshot_id,
        None,
        TraceQuery(entry, direction),  # type: ignore[arg-type]
    )


def _entry(service: FlowTraceService, stack: FlowStack, kind: str, label: str) -> str:
    items = service.entries(stack.project_id, stack.snapshot_id, None, kind)["items"]
    return str(next(e["key"] for e in items if e["label"] == label))


class Walk:
    """Lookups over one trace result."""

    def __init__(self, trace: dict[str, Any]) -> None:
        self.t = trace
        self.hops = {h["id"]: h for h in trace["hops"]}

    def to(self, step: dict[str, Any]) -> dict[str, Any]:
        return self.hops.get(step["to"], {}) if step["to"] else {}

    def steps(self, relation: str | None = None, **where: Any) -> list[dict[str, Any]]:
        found = []
        for s in self.t["steps"]:
            if relation and s["relation"] != relation:
                continue
            target = self.to(s) or {}
            if all(
                (target.get(k) if k in {"label", "node", "kind", "layer"} else s.get(k)) == v
                for k, v in where.items()
            ):
                found.append(s)
        return found

    def labels(self) -> list[str]:
        return [
            self.hops[s["to"]]["label"] for s in self.t["steps"] if s["to"] and not s.get("shared")
        ]


def test_whole_path_from_a_dom_event_to_the_database_and_back(
    pg_engine: Engine, tmp_path: Path
) -> None:
    stack = FlowStack(pg_engine, tmp_path, FLOW_REPOSITORY)
    stack.build()
    service = _service(stack)
    click = _entry(service, stack, "dom_event", "<button onClick>")
    trace = service.trace(stack.project_id, stack.snapshot_id, None, TraceQuery(click))
    w = Walk(trace)
    assert trace["contract"] == "flow-trace.v2" and trace["verification"] == "verified"

    # The ordered journey (first visits), frontend → HTTP → route → dependencies → service →
    # data → response → frontend continuation.
    labels = w.labels()
    journey = [
        "createItem",
        "POST",  # the request site (Stage 14 client_call)
        "create_item",  # the endpoint, then its handler
        "get_service",
        "get_db",
        "400 (raise)",  # ``raise HTTPException`` precedes ``svc.create`` in CFG order
        "create",
        "add()",
        "items",
        "commit()",
        "201 (return)",
        "listItems",
        "setState",
    ]
    positions = [labels.index(label) for label in journey]
    assert positions == sorted(positions), list(zip(journey, positions, strict=True))
    assert {"Framework response send", "notify"} <= set(labels)
    layers = {w.to(s)["label"]: w.to(s)["layer"] for s in trace["steps"] if s["to"]}
    assert layers["POST"] == "HTTP" and layers["get_db"] == "Dependencies"
    assert layers["items"] == "Data" and layers["notify"] == "Service"

    # Lanes: the click lane, background after the send, teardown by the version table, and
    # the effect re-run lane; independent lanes claim no order.
    lanes = {lane["id"]: lane["order"] for lane in trace["lanes"]}
    assert lanes["dom_event:onClick"] == "ORDER_NOT_DETERMINED"
    assert lanes["fastapi_background"] == "ORDER_NOT_DETERMINED"
    assert lanes["framework_response_send:teardown"] == "ORDERED_BY_FASTAPI_VERSION_TABLE"
    assert lanes["react_commit"] == "ORDER_NOT_DETERMINED"
    background = w.steps("response_boundary", label="notify")
    assert background and all(s["lane"] == "fastapi_background" for s in background)
    teardown = [s for s in w.steps("performs", label="close()") if s["lane"].endswith("teardown")]
    assert teardown and teardown[0]["reachability"]["verdict"] == "reachable_in_model"
    assert w.steps("may_rerun_effect") and all(
        s["lane"] == "react_commit" for s in w.steps("may_rerun_effect")
    )

    # Every HTTP hop is inferred and caveated; the trace says so.
    http = w.steps("calls_endpoint") + w.steps("responds")
    assert http and all(
        s["resolution"]["tier"] == "inferred" and HTTP_CAVEAT in s["resolution"]["conditions"]
        for s in http
    )
    assert HTTP_CAVEAT in trace["caveats"] and trace["path_tier"] == "inferred"

    # Response oracle: exit-specific outcomes mapped to the frontend continuation.
    responds = {
        (w.hops[s["from"]]["label"], s["attributes"]["continuation"])
        for s in w.steps("responds", label="POST")
    }
    assert {
        ("201 (return)", "fulfilled"),
        ("400 (raise)", "rejected"),
        ("422 (validation)", "rejected"),
        ("? (unhandled)", "fulfilled_or_rejected"),
    } <= responds

    # Database effects on the path: staged, then a commit on the same session is reached
    # before the response (never "saved").
    [effect] = trace["effects"]
    assert effect["class"] == "stage" and effect["tables"] == ["db_table:items"]
    assert effect["state"] == "commit_reached"
    assert effect["commit_position"]["code"] == "COMMIT_BEFORE_RESPONSE"
    assert "COMMIT_MAY_RAISE" in effect["conditions"]

    # Branch oracle: the raise path is guarded by ``not name``; the background task is
    # registered only after that guard, so on the 400 path its registration is refuted.
    raise_step = next(s for s in w.steps("raises_to") if w.to(s)["label"] == "400 (raise)")
    [term] = raise_step["path_condition"]["terms"]
    assert [(a["op"], a["value"]) for a in term] == [("opaque", False)]  # ``not name`` true
    on_raise = [
        s
        for s in background
        if any(
            w.hops[x["from"]]["label"] == "400 (raise)"
            for x in trace["steps"]
            if x["to"] == s["from"]
        )
    ]
    assert on_raise and on_raise[0]["feasibility"]["verdict"] == "refuted_in_fragment"
    assert all(
        s["feasibility"]["verdict"] != "refuted_in_fragment"
        for s in background
        if s not in on_raise
    )

    # INV-7: recomputation is byte-identical, also through a fresh cache.
    again = _trace(stack, click)
    assert again["trace_digest"] == trace["trace_digest"]
    assert json.dumps(again, sort_keys=True) == json.dumps(trace, sort_keys=True)
    # Structure is never presented as execution.
    assert "executes" not in json.dumps(trace) and trace["levels"]["L3"].endswith(
        "not an execution"
    )


def test_feasibility_oracles_in_the_sound_fragment(pg_engine: Engine, tmp_path: Path) -> None:
    stack = FlowStack(pg_engine, tmp_path, TRACE_REPOSITORY)
    run = stack.build()
    g = Graph(stack, run)

    def verdicts(locator: str, name: str) -> list[str]:
        trace = _trace(stack, g.scope(locator, name))
        return [
            s["feasibility"]["verdict"]
            for s in trace["steps"]
            if s["relation"] == "invokes" and Walk(trace).to(s)["label"] == "target"
        ]

    py, ts = "backend/app/feasible.py", "frontend/src/feasible.ts"
    # ``v is None`` then ``v is not None``: identity cannot be overridden → refuted.
    assert verdicts(py, "nones") == ["refuted_in_fragment", "consistent_in_fragment"]
    # ``x = 1``: the reaching definition is a builtin literal, so ``==`` is the builtin.
    assert verdicts(py, "literal") == ["refuted_in_fragment", "consistent_in_fragment"]
    # A class with ``__eq__``: overridable equality is opaque → never refuted.
    assert verdicts(py, "overridden") == ["undetermined"]
    # JS: strict equality on an admissible local; typeof is fixed.
    assert verdicts(ts, "strict") == ["refuted_in_fragment"]
    assert verdicts(ts, "types") == ["refuted_in_fragment"]
    # Loose ``==`` coerces, a getter may return anything, two calls are two evaluations, and a
    # closure-rebound variable is inadmissible: all undetermined, none refuted.
    assert verdicts(ts, "loose") == ["undetermined"]
    assert verdicts(ts, "getter") == ["undetermined"]
    assert verdicts(ts, "twice") == ["undetermined"]
    assert verdicts(ts, "rebound") == ["undetermined"]
    strict = _trace(stack, g.scope(ts, "strict"))
    refuted = next(
        s for s in strict["steps"] if s["feasibility"]["verdict"] == "refuted_in_fragment"
    )
    assert "HOST_OBJECT_FALSY_EXCLUDED" in refuted["feasibility"]["assumptions"]
    ops = [a["op"] for a in refuted["path_condition"]["terms"][0]]
    assert ops == ["eq", "eq"]


SHARED_HELPER = (
    "def target() -> None:\n    pass\n\n\n"
    "def leaf() -> None:\n    target()\n\n\n"
    "def helper() -> None:\n    leaf()\n\n\n"
    "def routes(v):\n"
    "    if v is None:\n"
    "        if v is not None:\n"
    "            helper()\n"
    "    helper()\n"
)


def test_a_shared_hop_keeps_every_route_condition(pg_engine: Engine, tmp_path: Path) -> None:
    # Two routes reach ``helper``: the first is contradictory (``v is None`` and
    # ``v is not None``), the second is unconditional. Both ``helper`` activations call
    # ``leaf`` from the same callsite, so ``leaf`` is one shared hop. Its condition is the
    # disjunction of both routes, so ``leaf -> target`` must not be refuted.
    files = {**TRACE_REPOSITORY, "backend/app/shared.py": SHARED_HELPER}
    stack = FlowStack(pg_engine, tmp_path, files)
    run = stack.build()
    trace = _trace(stack, Graph(stack, run).scope("backend/app/shared.py", "routes"))
    walk = Walk(trace)

    def verdicts(label: str) -> list[str]:
        return [s["feasibility"]["verdict"] for s in walk.steps("invokes", label=label)]

    # Each route into ``helper`` keeps its own condition: one refuted, one consistent.
    assert sorted(verdicts("helper")) == ["consistent_in_fragment", "refuted_in_fragment"]
    # The first ``helper -> leaf`` step stays on the refuted route; the shared one does not.
    into_leaf = walk.steps("invokes", label="leaf")
    assert len(into_leaf) == 2 and sum(bool(s.get("shared")) for s in into_leaf) == 1
    assert {s["feasibility"]["verdict"] for s in into_leaf} == {
        "refuted_in_fragment",
        "consistent_in_fragment",
    }
    # Below the shared hop, the condition is both routes as alternatives, never one
    # conjunction: the unconditional route absorbs the contradictory one.
    (below,) = walk.steps("invokes", label="target")
    assert below["feasibility"]["verdict"] == "consistent_in_fragment"
    assert below["path_condition"] == {"terms": [], "approximate": False}
    # Recomputation stays byte-identical (INV-7).
    again = _trace(stack, Graph(stack, run).scope("backend/app/shared.py", "routes"))
    assert again["trace_digest"] == trace["trace_digest"]
    # A trace pinned under the earlier contract (first-route conditions) re-verifies as
    # stale, not as an integrity failure: the rules changed, the evidence did not.
    service = _service(stack)
    query = TraceQuery(Graph(stack, run).scope("backend/app/shared.py", "routes"))
    digest = service.pin(stack.project_id, stack.snapshot_id, None, query)["trace_digest"]
    assert service.pinned(stack.project_id, stack.snapshot_id, digest)["status"] == "current"
    with pg_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE flow_trace_records SET result = "
                "jsonb_set(result, '{contract}', '\"flow-trace.v1\"') WHERE trace_digest = :d"
            ),
            {"d": digest},
        )
    old = service.pinned(stack.project_id, stack.snapshot_id, digest)
    assert old["status"] == "stale" and old["freshness_reasons"] == ["TRACE_CONTRACT_CHANGED"]


def test_commit_in_dependency_teardown_is_after_the_response_and_absence_is_gated(
    pg_engine: Engine, tmp_path: Path
) -> None:
    stack = FlowStack(pg_engine, tmp_path, TRACE_REPOSITORY)
    run = stack.build()
    service = _service(stack)
    trace = service.trace(
        stack.project_id,
        stack.snapshot_id,
        None,
        TraceQuery(_entry(service, stack, "http_endpoint", "POST /api/tx")),
    )
    [effect] = trace["effects"]
    assert effect["state"] == "commit_reached"
    assert "SESSION_IDENTITY_BY_PARAMETER" in effect["conditions"]
    assert effect["commit_position"]["code"] == "COMMIT_AFTER_RESPONSE_SENT"  # 0.141.1 request

    # Backward "no callers" claims are gated: allowed for Python (complete coverage and no
    # dynamic Python callsites), refused for TS (``check()`` calls a callable parameter).
    g = Graph(stack, run)
    lonely = _trace(stack, g.scope("backend/app/feasible.py", "lonely"), "backward")
    [claim] = [a for a in lonely["absence"] if a["hop"] == "H0"]
    assert claim == {"hop": "H0", "verdict": "no_callers_in_model", "reasons": []}
    orphan = _trace(stack, g.scope("frontend/src/feasible.ts", "orphan"), "backward")
    [gated] = [a for a in orphan["absence"] if a["hop"] == "H0"]
    assert gated["verdict"] == "ABSENCE_NOT_DETERMINABLE"
    assert "DYNAMIC_CALLSITES_PRESENT" in gated["reasons"]
    # Backward from the handler reaches its endpoint, request site and the click entry.
    handler = _trace(stack, g.scope("backend/app/api.py", "create_item"), "backward")
    reached = {h["kind"] for h in handler["hops"]}
    assert {"api_endpoint", "http_request", "callsite", "entry_point"} <= reached

    # A structural path is one witness, labelled as such.
    entry = _entry(service, stack, "dom_event", "<button onClick>")
    witness = service.path(stack.project_id, stack.snapshot_id, None, entry, "db_table:items")
    assert witness["found"] and witness["level"] == "L3_structural_witness"
    assert "other paths may exist" in witness["note"]
    assert witness["steps"][-1]["target"] == "db_table:items"
    assert HTTP_CAVEAT in witness["caveats"]


def test_pinning_reverifies_and_traces_read_no_source(pg_engine: Engine, tmp_path: Path) -> None:
    stack = FlowStack(pg_engine, tmp_path, FLOW_REPOSITORY)
    run = stack.build()
    shutil.rmtree(tmp_path)  # INV-1: traces, paths and pins never read source bytes
    service = _service(stack)
    entry = _entry(service, stack, "http_endpoint", "POST /api/items")
    pinned = service.pin(stack.project_id, stack.snapshot_id, None, TraceQuery(entry))
    digest = pinned["trace_digest"]
    with pg_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM flow_trace_records")).scalar() == 1
    assert service.pinned(stack.project_id, stack.snapshot_id, digest)["status"] == "current"

    # Tampering with an element of the pinned trace: integrity_failed, and live reads fail.
    edge = next(s["edge"] for s in pinned["steps"] if s["relation"] == "injects")
    with pg_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE flow_edges SET tier = 'observed' WHERE flow_run_id = :r AND edge_key = :k"
            ),
            {"r": run.id, "k": edge},
        )
    fresh = FlowTraceService(stack.flow, stack.store, cache=TraceGraphCache())
    assert fresh.pinned(stack.project_id, stack.snapshot_id, digest)["status"] == "integrity_failed"
    with pytest.raises(FlowIntegrityError):
        service.trace(stack.project_id, stack.snapshot_id, None, TraceQuery(entry))
    with pg_engine.begin() as connection:
        connection.execute(
            text("UPDATE flow_edges SET tier = 'derived' WHERE flow_run_id = :r AND edge_key = :k"),
            {"r": run.id, "k": edge},
        )
    assert fresh.pinned(stack.project_id, stack.snapshot_id, digest)["status"] == "current"


def test_flow_trace_http_contract(pg_engine: Engine, tmp_path: Path) -> None:
    write(tmp_path, FLOW_REPOSITORY)
    app = create_app(Settings(database_url=pg_engine.url.render_as_string(hide_password=False)))
    with TestClient(app) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "Trace", "root_path": str(tmp_path)}
        ).json()["id"]
        base = f"/api/v1/projects/{project}/snapshots"
        scope = f"{base}/{client.post(base, json={}).json()['id']}"
        flow = scope + "/flow"
        assert client.get(flow + "/entries").json()["detail"]["code"] == "FLOW_REQUIRED"
        for step in ("/structure", "/processing", "/semantic/catalog", "/architecture"):
            assert client.post(scope + step, json={}).status_code == 200, step
        assert client.post(flow + "/evidence", json={}).status_code == 200
        assert client.post(flow, json={}).status_code == 200
        listed = client.get(flow + "/entries?kind=http_endpoint").json()
        assert {e["label"] for e in listed["items"]} == {"POST /api/items", "GET /api/items"}
        entry = next(e["key"] for e in listed["items"] if e["label"] == "POST /api/items")
        # Natural-language discovery proposes candidates with reasons; nothing is selected.
        found = client.get(flow + "/entries", params={"q": "create item", "mode": "lexical"})
        assert found.status_code == 200, found.text
        body = found.json()
        assert body["mode"] == "lexical" and "user chooses" in body["selection"]
        assert entry in [e["key"] for e in body["items"]]
        assert all({"hit_rank", "distance"} <= set(e["reason"]) for e in body["items"])
        traced = client.get(flow + "/trace", params={"entry": entry})
        assert traced.status_code == 200, traced.text
        trace = traced.json()
        assert trace["verification"] == "verified" and trace["status"] in {
            "complete",
            "incomplete_evidence",
        }
        assert client.get(flow + "/trace", params={"entry": "nope"}).status_code == 404
        bad = client.get(flow + "/trace", params={"entry": entry, "direction": "sideways"})
        assert bad.status_code == 422
        path = client.get(flow + "/path", params={"source": entry, "target": "db_table:items"})
        assert path.json()["found"] is True
        rejected = client.post(
            flow + "/traces", content="{}", headers={"Content-Type": "text/plain"}
        )
        assert rejected.status_code == 415
        pinned = client.post(flow + "/traces", json={"entry": entry})
        assert pinned.status_code == 200, pinned.text
        digest = pinned.json()["trace_digest"]
        assert pinned.json()["trace_digest"] == trace["trace_digest"]
        assert client.get(f"{flow}/traces/{digest}").json()["status"] == "current"
        assert client.get(f"{flow}/traces/{'0' * 64}").status_code == 404
        # A later structural run: live traces fail closed; the pin reports stale with reasons.
        assert client.post(scope + "/structure", json={}).status_code == 200
        stale = client.get(flow + "/trace", params={"entry": entry})
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "FLOW_STALE"
        record = client.get(f"{flow}/traces/{digest}").json()
        assert (
            record["status"] == "stale"
            and "STRUCTURAL_AUTHORITY_CHANGED" in (record["freshness_reasons"])
        )


def test_stale_flow_model_blocks_traces(pg_engine: Engine, tmp_path: Path) -> None:
    stack = FlowStack(pg_engine, tmp_path, FLOW_REPOSITORY)
    stack.build()
    service = _service(stack)
    entry = _entry(service, stack, "http_endpoint", "GET /api/items")
    stack.stack.structural.analyse(stack.project_id, stack.snapshot_id)
    with pytest.raises(FlowStaleError):
        service.trace(stack.project_id, stack.snapshot_id, None, TraceQuery(entry))
