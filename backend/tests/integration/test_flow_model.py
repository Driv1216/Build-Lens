"""Stage 15 Milestone B: the flow model against real PostgreSQL, the real worker and Stage 14.

Designed fixtures (resolution, construction, async, React, FastAPI dependencies and responses,
SQLAlchemy) with designed verdicts; adversarial non-links that must produce zero edges; the
tamper suite (INV-6); short-circuit and portable-digest reproduction (INV-5); non-interference
(INV-3); freshness (INV-4); and the HTTP contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from alembic import command
from buildlens.api.main import create_app
from buildlens.application.flow import FlowIntegrityError, FlowStaleError
from buildlens.core.config import Settings
from buildlens.domain.flow import HTTP_CAVEAT
from buildlens.infrastructure.persistence.models import Base, FlowNodeRecord
from tests.flow_support import (
    DESIGNED_REPOSITORY,
    FLOW_REPOSITORY,
    SEMANTICS_REPOSITORY,
    FlowStack,
)
from tests.semantic_support import write

pytestmark = pytest.mark.integration

NEW_COLUMNS = {"project_versions": {"active_flow_evidence_run_id", "active_flow_run_id"}}


class Graph:
    """Lookup helpers over one built flow model run (by name, never by ordinal)."""

    def __init__(self, stack: FlowStack, run: Any) -> None:
        self.edges = stack.edges(run)
        with stack.stack.factory() as session:
            self.nodes = {
                r.node_key: r
                for r in session.scalars(
                    select(FlowNodeRecord).where(FlowNodeRecord.flow_run_id == run.id)
                )
            }

    def scope(self, locator: str, *path: str) -> str:
        """Scope key of ``path`` (e.g. ``("Shape", "size")``) declared in ``locator``."""
        prefix = f"scope:{locator}#m"
        for name in path:
            matches = [
                k
                for k, n in self.nodes.items()
                if k.startswith(prefix + ".")
                and k.count(".") == prefix.count(".") + 1
                and n.label == name
            ]
            assert len(matches) == 1, (locator, path, matches)
            prefix = matches[0]
        return prefix

    def calls(self, scope: str, label: str) -> list[str]:
        """Callsite keys contained in ``scope`` whose callee label is ``label``."""
        return sorted(
            e.target_key
            for e in self.edges
            if e.relation == "contains_callsite"
            and e.source_key == scope
            and self.nodes[e.target_key].label == label
        )

    def call(self, scope: str, label: str) -> str:
        found = self.calls(scope, label)
        assert len(found) == 1, (scope, label, found)
        return found[0]

    def out(self, source: str, relation: str | None = None) -> list[Any]:
        return [
            e
            for e in self.edges
            if e.source_key == source and (relation is None or e.relation == relation)
        ]

    def into(self, target: str, relation: str | None = None) -> list[Any]:
        return [
            e
            for e in self.edges
            if e.target_key == target and (relation is None or e.relation == relation)
        ]

    def one(self, source: str, relation: str, target: str | None = None) -> Any:
        found = [e for e in self.out(source, relation) if target is None or e.target_key == target]
        assert len(found) == 1, (source, relation, target, found)
        return found[0]


def _fingerprint(engine: Engine) -> dict[str, str]:
    values: dict[str, str] = {}
    with engine.connect() as connection:
        for name, table in Base.metadata.tables.items():
            if name.startswith("flow_"):
                continue
            columns = [c.name for c in table.columns if c.name not in NEW_COLUMNS.get(name, set())]
            projection = ", ".join(f'"{c}"' for c in columns)
            values[name] = connection.execute(
                text(
                    f"SELECT md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), '')) "
                    f'FROM (SELECT {projection} FROM "{name}") r'
                )
            ).scalar_one()
    return values


def test_designed_cases_have_designed_verdicts(pg_engine: Engine, tmp_path: Path) -> None:
    stack = FlowStack(pg_engine, tmp_path, DESIGNED_REPOSITORY)
    run = stack.build()
    assert run.outcome == "complete"
    assert stack.store.diagnostics(run.id) == []
    g = Graph(stack, run)
    cases = "backend/app/cases.py"
    builder = g.scope(cases, "construct_all")

    def constructs(label: str) -> tuple[Any, list[Any]]:
        site = g.call(builder, label)
        return g.one(site, "constructs"), g.out(site, "invokes")

    def phases(invokes: list[Any]) -> set[str]:
        return {e.attributes.get("phase") for e in invokes}

    # Construction (py-construction.v1, plan §3.3.2).
    edge, invokes = constructs("Color()")
    assert edge.tier == "derived" and "ENUM_CALL_IS_LOOKUP" in edge.conditions and not invokes
    site = g.call(builder, "Managed()")
    assert g.one(site, "constructs").tier == "unknown"
    assert g.one(site, "constructs").conditions == ("METACLASS_CALL_OVERRIDE",)
    assert [e.target_key for e in g.out(site, "dispatch_candidate")] == [
        g.scope(cases, "Meta", "__call__")
    ]
    assert not g.out(site, "invokes")
    edge, invokes = constructs("Swapped()")
    assert "NEW_RETURNS_OTHER_TYPE" in edge.conditions and edge.tier == "inferred"
    assert phases(invokes) == {"__new__"}  # no __init__ when __new__ returns another type
    edge, invokes = constructs("Allocated()")
    assert phases(invokes) == {"__new__", "__init__"}
    assert all(e.tier == "derived" for e in invokes)  # __new__ returns super().__new__(cls)
    _, invokes = constructs("Point()")
    assert [(e.attributes["phase"], e.conditions) for e in invokes] == [
        ("__post_init__", ("DATACLASS_GENERATED_INIT",))
    ]
    assert invokes[0].target_key == g.scope(cases, "Point", "__post_init__")
    assert constructs("Manual()")[1] == []  # init=False: no generated __init__
    _, invokes = constructs("Explicit()")
    assert phases(invokes) == {"__init__"}  # explicit __init__: no __post_init__ link

    # Resolution: self-dispatch with override candidates; reaching definitions.
    area = g.call(g.scope(cases, "Shape", "area"), "self.size()")
    assert g.one(area, "invokes").target_key == g.scope(cases, "Shape", "size")
    candidate = g.one(area, "dispatch_candidate")
    assert candidate.target_key == g.scope(cases, "Square", "size")
    assert candidate.conditions == ("VIRTUAL_DISPATCH_CANDIDATES",)
    go = g.call(g.scope(cases, "receivers"), "svc.go()")
    assert [e.target_key for e in g.out(go, "resolves_to")] == [g.scope(cases, "Beta", "go")]

    # Async (py-async-semantics.v2): creating is not running.
    fetch = g.scope(cases, "fetch")
    never = g.call(g.scope(cases, "never_awaited"), "fetch()")
    assert {e.relation for e in g.out(never) if e.target_key == fetch} == {
        "resolves_to",
        "creates_coroutine",
    }
    iterated = g.call(g.scope(cases, "never_iterated"), "countdown()")
    assert g.one(iterated, "creates_generator").target_key == g.scope(cases, "countdown")
    assert not g.out(iterated, "invokes")
    awaited = g.scope(cases, "awaited")
    task = g.one(g.call(awaited, "asyncio.create_task()"), "registers")
    assert task.target_key == fetch and task.attributes["scheduler"] == "asyncio_task"
    assert "ORDER_NOT_DETERMINED" in task.conditions
    assert any(g.out(site, "awaits") for site in g.calls(awaited, "fetch()"))

    # SQLAlchemy (sqlalchemy-ops.v2): operation classes; raw SQL attributes no table.
    raw = g.call(g.scope(cases, "raw"), "session.execute()")
    operation = g.one(raw, "performs")
    assert "RAW_SQL_NOT_PARSED" in operation.conditions
    assert not g.out(operation.target_key, "on_table")
    staged = g.one(g.call(g.scope(cases, "staged"), "session.add()"), "performs")
    assert staged.attributes["class"] == "stage"
    table = g.one(staged.target_key, "on_table")
    assert table.target_key == "db_table:items" and g.nodes["db_table:items"].kind == "db_table"
    service = "backend/app/service.py"
    commit = g.call(g.scope(service, "ItemService", "create"), "self.session.commit()")
    assert g.one(commit, "performs").attributes["class"] == "commit"

    # FastAPI dependencies (fastapi-semantics.v1): yield dependency teardown at 0.141.1.
    api = "backend/app/api.py"
    create = g.scope(api, "create_item")
    get_service, get_db = g.scope(api, "get_service"), g.scope("backend/app/db.py", "get_db")
    assert {e.attributes["phase"] for e in g.out(create, "injects")} == {"setup"}
    assert g.one(create, "injects").target_key == get_service
    teardown = next(e for e in g.out(get_service, "injects") if e.attributes["phase"] == "teardown")
    assert teardown.target_key == get_db and teardown.attributes["fastapi"] == "0.141.1"
    assert teardown.attributes["alternatives"] == [
        {"scope": "request", "teardown": "after_response_send", "background": "before_teardown"}
    ]
    background = g.one(g.call(create, "tasks.add_task()"), "registers")
    assert background.target_key == g.scope(api, "notify")
    assert background.attributes["scheduler"] == "fastapi_background"

    # Responses (fastapi-responses.v1): exit-specific outcomes, never an unconditional 500.
    def outcomes(handler: str) -> dict[str, Any]:
        return {
            g.nodes[e.target_key].attributes["exit"]
            + (
                ":" + e.target_key.rsplit(":", 1)[-1] if ":status:" in e.target_key else ""
            ): g.nodes[e.target_key].attributes
            for e in g.out(handler, "exits_with")
        }

    created = outcomes(create)
    assert created["return"]["status"] == 201 and created["raise"]["status"] == 400
    assert created["validation"]["status"] == 422
    assert created["unhandled"]["status"] is None and created["unhandled"]["alternatives"] == [500]
    responses = "backend/app/responses.py"
    assert outcomes(g.scope(responses, "plain"))["return"]["status"] == 200  # POST default 200
    as_json = outcomes(g.scope(responses, "as_json"))["return"]
    assert as_json["status"] == 202 and as_json["returned_instance"] is True
    dynamic = outcomes(g.scope(responses, "dynamic"))
    assert {v["status"] for k, v in dynamic.items() if k.startswith("return")} == {200, 203}
    unhandled = g.one(create, "exits_with", f"response:{create}:unhandled")
    assert "DEFAULT_SERVER_ERROR_HANDLING_ASSUMED" in unhandled.conditions

    # Back to the frontend: the wrapper throws on !ok, so non-2xx outcomes reject.
    responded = {
        e.attributes["status"]: e
        for e in g.edges
        if e.relation == "responds"
        and create in e.source_key
        and e.target_key == "client:frontend/src/items.ts:0"  # the fetch wrapper's site
    }
    assert responded[201].attributes["continuation"] == "fulfilled"
    assert "OK_GUARD_RECOGNIZED" in responded[201].conditions
    assert responded[400].attributes["continuation"] == "rejected"
    assert responded[None].attributes["continuation"] == "fulfilled_or_rejected"
    assert all(HTTP_CAVEAT in e.conditions and e.tier == "inferred" for e in responded.values())

    # React (react-semantics.v2), TanStack Query (D14), react-hook-form (D15), aliases (D9).
    panel_file = "frontend/src/Panel.tsx"
    panel = g.scope(panel_file, "Panel")
    registered = {
        (e.attributes["scheduler"], e.target_key): e
        for e in g.out(panel, "registers")
        if e.attributes.get("scheduler", "").startswith("dom_event")
    }
    on_save = next(t for (s, t) in registered if s == "dom_event:onClick" and t.endswith(".4"))
    use_callback = g.call(panel, "useCallback()")
    assert not [e for e in g.out(use_callback) if e.target_key == on_save]  # memoised, not run
    memo = g.one(g.call(panel, "useMemo()"), "invokes")
    assert memo.attributes["phase"] == "render"
    assert "RECOMPUTE_WHEN_DEPS_CHANGE_OR_CACHE_DISCARDED" in memo.conditions
    setter = [e for e in g.edges if e.relation == "schedules_render" and panel_file in e.source_key]
    assert setter and all("RENDER_MAY_BAIL_OUT" in e.conditions for e in setter)
    child = g.scope(panel_file, "Child")
    assert g.one(child, "binds_prop_callback").target_key == on_save
    prop_calls = [
        e
        for e in g.edges
        if e.relation == "invokes_callback" and "PROP_CALLBACK_BINDING" in e.conditions
    ]
    assert [e.target_key for e in prop_calls] == [on_save]
    mutate = g.one(g.call(on_save, "mutation.mutate()"), "invokes_callback")
    assert mutate.attributes == {
        "method": "mutate",
        "scheduler": "tanstack_mutation",
        "rejection_propagates": False,
    }
    success = g.one(mutate.target_key, "continues_with")
    assert success.attributes == {"level": "hook", "outcome": "fulfilled"}
    submit = registered[next(k for k in registered if k[0] == "dom_event:onSubmit")]
    assert {"HANDLER_IS_CALL_RESULT", "ON_VALID", "VALIDATION_MAY_BLOCK"} <= set(submit.conditions)
    create_call = g.call(mutate.target_key, "createItem()")  # inside mutationFn
    alias = g.one(create_call, "resolves_to")
    assert alias.target_key == g.scope("frontend/src/items.ts", "createItem")
    assert alias.conditions == ("TSCONFIG_PATHS_TRUSTED",) and alias.tier == "derived"

    # Request descriptors (D10): a factory-bound hey-api site links only to matching methods.
    sdk = "frontend/src/sdk.ts"
    request = g.one(g.call(g.scope(sdk, "createItemSdk"), "client.post()"), "issues_request")
    linked = g.out(request.target_key, "calls_endpoint")
    assert [e.target_key.split("#")[1].split(":")[0] for e in linked] == ["create_item"]
    assert all(
        {HTTP_CAVEAT, "DESCRIPTOR_REQUEST_SITE"} <= set(e.conditions) and e.tier == "inferred"
        for e in linked
    )

    # Every HTTP hop is inferred and carries the Stage 14 evaluation caveat (D1).
    http = [e for e in g.edges if e.relation in {"calls_endpoint", "responds"}]
    assert http and all(e.tier == "inferred" and HTTP_CAVEAT in e.conditions for e in http)

    # Adversarial non-links: zero forbidden edges.
    lookalike = g.scope(cases, "lookalike")
    helper = g.call(lookalike, "helper()")
    assert [e.relation for e in g.out(helper)] == ["unresolved"]  # not other.helper (no import)
    assert not g.into(g.scope("backend/app/other.py", "helper"), "resolves_to")
    decoy_caller = g.call(g.scope("backend/app/decoys.py", "caller"), "helper()")
    assert [e.relation for e in g.out(decoy_caller)] == ["unresolved"]  # homoglyph not equated
    assert not g.into(g.scope("backend/app/decoys.py", "һelper"), "resolves_to")
    assert not g.out(g.call(lookalike, "getattr()"), "resolves_to")
    assert not g.out(g.call(g.scope(sdk, "decoy"), "fake.GET()"), "issues_request")
    later = next(t for (s, t) in registered if s == "dom_event:onClick" and t != on_save)
    assert not g.out(g.call(later, "other.mutate()"), "invokes_callback")  # other-query
    assert not [e for e in g.edges if e.relation == "scheduled_by" and "createItemSdk" in str(e)]
    decoy = g.scope("frontend/src/Decoy.tsx", "Decoy")
    assert not [e for e in g.out(decoy, "registers") if "ON_VALID" in e.conditions]
    assert [e.conditions for e in g.out(decoy, "unresolved")] == [("HANDLER_IS_CALL_RESULT",)]

    assert stack.flow.integrity(stack.project_id, stack.snapshot_id, None)["verified"] is True


def test_remaining_semantics_have_designed_verdicts(pg_engine: Engine, tmp_path: Path) -> None:
    stack = FlowStack(pg_engine, tmp_path, SEMANTICS_REPOSITORY)
    run = stack.build()
    assert run.outcome == "complete" and stack.store.diagnostics(run.id) == []
    g = Graph(stack, run)
    gen = "backend/app/generators.py"
    numbers = g.scope(gen, "numbers")

    # Generators (py-async-semantics.v2): creation, iteration, resumption, throwing in.
    consume = g.scope(gen, "consume")
    looped, stored = g.calls(consume, "numbers()")
    assert g.one(looped, "iterates").target_key == numbers
    assert g.one(stored, "creates_generator").target_key == numbers
    assert not g.out(stored, "iterates")  # ``g = numbers()`` alone runs nothing
    assert g.one(g.call(consume, "next()"), "resumes").target_key == numbers
    assert g.one(g.call(consume, "g.send()"), "resumes").attributes["method"] == "send"
    close = g.one(g.call(consume, "g.close()"), "throws_into")
    assert close.attributes == {"method": "close", "exception": "GeneratorExit"}

    # @contextmanager: the with item resumes the generator to its yield, then resumes or throws.
    opened = g.call(g.scope(gen, "use_manager"), "opened()")
    phases = {e.attributes["phase"]: e for e in g.out(opened, "resumes")}
    assert set(phases) == {"enter", "exit"}
    assert phases["exit"].attributes["exceptional"] == "resume_throw"
    assert "SUPPRESSION_DECIDED_BY_GENERATOR" in phases["exit"].conditions

    # Decorators, NoReturn, never-returning callees, process exit.
    callers = g.scope(gen, "callers")
    wrapped = g.one(g.call(callers, "wrapped()"), "invokes")
    assert wrapped.tier == "inferred" and "DECORATOR_MAY_WRAP" in wrapped.conditions
    assert g.one(g.call(callers, "plain()"), "invokes").conditions == ()
    fail = g.one(g.call(callers, "fail()"), "invokes")
    assert fail.attributes["normal_continuation"] is False
    assert "NORETURN_ANNOTATION_TRUSTED" in fail.conditions
    spin = g.one(g.call(callers, "spin()"), "invokes")
    assert "CALLEE_NEVER_RETURNS" in spin.conditions
    exit_ = g.one(g.call(callers, "sys.exit()"), "crosses_boundary")
    assert "PROCESS_EXIT" in exit_.conditions and exit_.attributes["normal_continuation"] is False

    # Protocol receivers: structural candidates only for complete implementations.
    save = g.call(g.scope(gen, "persist"), "store.save()")
    assert "PROTOCOL_METHOD_STUB" in g.one(save, "invokes").conditions
    assert [e.target_key for e in g.out(save, "dispatch_candidate")] == [
        g.scope(gen, "SqlStore", "save")
    ]  # HalfStore lacks ``load``

    # Star and conditional imports.
    star = "backend/app/star.py"
    assert g.out(g.call(g.scope(star, "starred"), "helper()"), "unresolved")[0].conditions == (
        "STAR_IMPORT",
    )
    dumps = g.out(g.call(g.scope(star, "dumps"), "json.dumps()"), "crosses_boundary")
    assert {e.target_key for e in dumps} == {"external:json", "external:ujson"}  # both bindings
    assert all("CONDITIONAL_IMPORT" in e.conditions for e in dumps)

    # Transaction scopes: only same-session operations inside the body are in the transaction.
    transactional = g.scope(gen, "transactional")
    begin = g.call(transactional, "session.begin()")
    boundary = f"txn:{begin}"
    assert g.nodes[boundary].kind == "transaction_boundary"
    inside, outside = g.calls(transactional, "session.add()")
    assert g.one(f"dbop:{inside}", "in_transaction").target_key == boundary
    assert not g.out(f"dbop:{outside}", "in_transaction")
    factory = g.scope(gen, "factory_transaction")
    added = g.call(factory, "session.add()")
    assert g.one(f"dbop:{added}", "in_transaction").target_key == (
        f"txn:{g.call(factory, 'factory.begin()')}"
    )

    # Non-links: a repository ``begin()`` is no transaction; a list is not a generator.
    decoys = g.scope(gen, "decoys")
    assert not g.out(g.call(decoys, "….begin()"), "performs")
    assert not g.out(f"dbop:{g.call(decoys, 'session.add()')}", "in_transaction")
    assert not [e for e in g.edges if e.relation == "iterates" and decoys in e.source_key]

    # FastAPI: exceptions below the handler, static handlers, the response boundary.
    adv = "backend/app/advanced.py"
    guarded = g.scope(adv, "guarded")
    raised = {
        g.nodes[e.target_key].attributes["raised_at"]: e
        for e in g.out(guarded, "exits_with")
        if "raised_at" in g.nodes[e.target_key].attributes
    }
    in_dependency = g.call(g.scope(adv, "current_user"), "HTTPException()")
    in_callee = g.call(g.scope(adv, "check"), "HTTPException()")
    assert set(raised) == {in_dependency, in_callee}
    assert "RAISED_IN_DEPENDENCY" in raised[in_dependency].conditions
    assert "RAISED_IN_CALLEE" in raised[in_callee].conditions
    assert g.nodes[raised[in_dependency].target_key].attributes["status"] == 401
    assert g.one(in_dependency, "raises_to").target_key == raised[in_dependency].target_key
    on_http, on_validation = g.scope(adv, "on_http"), g.scope(adv, "on_validation")
    assert g.one(raised[in_callee].target_key, "raises_to").target_key == on_http
    assert g.one(f"response:{guarded}:validation", "raises_to").target_key == on_validation
    assert not g.out(f"response:{guarded}:unhandled", "raises_to")  # no catch-all registered
    validation = g.one(guarded, "exits_with", f"response:{guarded}:validation")
    assert "DEPENDENCY_SIDE_EFFECTS_ON_422_POSSIBLE" in validation.conditions
    cache = {e.attributes["param"]: e.attributes["use_cache"] for e in g.out(guarded, "injects")}
    assert cache == {"user": True, "session": False}
    task = g.one(g.call(guarded, "tasks.add_task()"), "registers")
    assert {
        "BACKGROUND_TASKS_DROPPED_BY_RESPONSE",
        "BACKGROUND_TASK_USES_DEPENDENCY_RESOURCE",
    } <= set(task.conditions)
    send = "scheduler:framework_response_send"
    sent = {e.source_key for e in g.into(send, "response_boundary")}
    assert {e.target_key for e in g.out(guarded, "exits_with")} <= sent
    steps = {
        e.attributes["step"]: e
        for e in g.out(send, "response_boundary")
        if e.attributes["handler"] == guarded
    }
    assert steps["teardown"].target_key == g.scope("backend/app/db.py", "get_db")
    assert steps["teardown"].attributes["sides"] == ["after_response_send"]  # 0.141.1, request
    assert steps["background"].attributes["after"] == "response_send"
    assert steps["background"].attributes["relative_to_teardown"] == ["before_teardown"]

    # React effects: cleanup, possible re-runs by dependency, never "runs".
    fx = "frontend/src/Effects.tsx"
    ticker = g.scope(fx, "Ticker")
    registered = [
        e
        for site in g.calls(ticker, "useEffect()")
        for e in g.out(site, "registers")
        if e.attributes["scheduler"] == "react_commit"
    ]
    effects = {tuple(e.attributes["deps"]): e.target_key for e in registered}
    ticking = effects[("tick",)]
    cleanup = g.one(ticking, "registers")
    assert cleanup.attributes["scheduler"] == "react_commit:cleanup"
    timer = [
        e for e in g.edges if e.relation == "registers" and e.attributes.get("scheduler") == "timer"
    ]
    assert len(timer) == 1 and "ORDER_NOT_DETERMINED" in timer[0].conditions
    reruns = [e for e in g.edges if e.relation == "may_rerun_effect" and fx in e.source_key]
    assert [(e.target_key, e.attributes["state"]) for e in reruns] == [(ticking, "tick")]
    assert "DEPENDENCY_CHANGE_NOT_DETERMINED" in reruns[0].conditions  # never "runs"
    assert not g.into(effects[()], "may_rerun_effect")  # [] deps: mount only
    refetch = [e for e in g.edges if e.relation == "may_refetch"]
    assert len(refetch) == 1 and refetch[0].target_key == "scheduler:tanstack_query"
    reactions = [
        e
        for e in g.edges
        if e.relation == "registers" and e.attributes.get("scheduler") == "microtask"
    ]
    assert len(reactions) == 1 and "THENABLE_ASSUMED" in reactions[0].conditions

    # TS construction: constructor chains, implicit and explicit super, return override.
    cls = "frontend/src/classes.ts"
    build = g.scope(cls, "build")
    made = {
        g.nodes[e.target_key].label: (e, g.out(e.source_key, "invokes"))
        for site in g.calls(build, "Child()")
        + g.calls(build, "Explicit()")
        + g.calls(build, "Weird()")
        for e in g.out(site, "constructs")
    }
    child, child_invokes = made["Child"]
    assert "FIELD_INITIALISERS_NOT_LOWERED" in child.conditions
    assert [e.attributes["phase"] for e in child_invokes] == ["inherited_constructor"]
    base_constructor = child_invokes[0].target_key
    assert g.calls(base_constructor, "this.init()")  # Base's constructor body
    _, explicit_invokes = made["Explicit"]
    explicit_constructor = explicit_invokes[0].target_key
    super_call = g.one(g.call(explicit_constructor, "super()"), "invokes")
    assert (
        super_call.target_key == base_constructor and "SUPER_CONSTRUCTOR" in super_call.conditions
    )
    weird, _ = made["Weird"]
    assert weird.tier == "unknown" and "CONSTRUCTOR_RETURN_OVERRIDE" in weird.conditions

    assert stack.flow.integrity(stack.project_id, stack.snapshot_id, None)["verified"] is True


def test_tampering_with_elements_evidence_or_cited_rows_fails_closed(
    pg_engine: Engine, tmp_path: Path
) -> None:
    stack = FlowStack(pg_engine, tmp_path)
    run = stack.build()
    g = Graph(stack, run)
    create = g.scope("backend/app/api.py", "create_item")
    edge = g.one(create, "injects")
    read = lambda: stack.flow.node(stack.project_id, stack.snapshot_id, None, create)  # noqa: E731
    assert read()["node"].key == create

    def tamper(sql: str, params: dict[str, Any], expected: str) -> None:
        with pg_engine.begin() as connection:
            connection.execute(text(sql), params)
        try:
            with pytest.raises(FlowIntegrityError) as raised:
                read()
            assert any(f.startswith(expected) for f in raised.value.failures), raised.value.failures
            report = stack.flow.integrity(stack.project_id, stack.snapshot_id, None)
            assert report["verified"] is False and report["failure_count"] >= 1
        finally:
            restore()

    snapshot: dict[str, Any] = {}
    with pg_engine.connect() as connection:
        for table in ("flow_evidence_items", "flow_callsites"):
            snapshot[table] = connection.execute(text(f"SELECT * FROM {table}")).mappings().all()

    def restore() -> None:
        with pg_engine.begin() as connection:
            connection.execute(
                text("UPDATE flow_nodes SET label = :l WHERE flow_run_id = :r AND node_key = :k"),
                {"l": g.nodes[create].label, "r": run.id, "k": create},
            )
            connection.execute(
                text("UPDATE flow_edges SET tier = :t WHERE flow_run_id = :r AND edge_key = :k"),
                {"t": edge.tier, "r": run.id, "k": edge.key},
            )
            for row in snapshot["flow_evidence_items"]:
                connection.execute(
                    text(
                        "UPDATE flow_evidence_items SET derived = CAST(:d AS jsonb) "
                        "WHERE flow_run_id = :r AND digest = :g"
                    ),
                    {"d": json.dumps(row["derived"]), "r": row["flow_run_id"], "g": row["digest"]},
                )
            for row in snapshot["flow_callsites"]:
                connection.execute(
                    text(
                        "UPDATE flow_callsites SET cfg_node = :c WHERE flow_evidence_run_id = :r "
                        "AND artifact_analysis_id = :a AND scope_key = :s AND ordinal = :o"
                    ),
                    {
                        "c": row["cfg_node"],
                        "r": row["flow_evidence_run_id"],
                        "a": row["artifact_analysis_id"],
                        "s": row["scope_key"],
                        "o": row["ordinal"],
                    },
                )
        assert read()["node"].key == create

    # A semantic field of a node, then of an edge.
    tamper(
        "UPDATE flow_nodes SET label = 'forged' WHERE flow_run_id = :r AND node_key = :k",
        {"r": run.id, "k": create},
        "NODE_DIGEST_MISMATCH",
    )
    tamper(
        "UPDATE flow_edges SET tier = 'observed' WHERE flow_run_id = :r AND edge_key = :k",
        {"r": run.id, "k": edge.key},
        "EDGE_DIGEST_MISMATCH",
    )
    # An evidence item the edge cites.
    tamper(
        'UPDATE flow_evidence_items SET derived = \'{"rule": "forged"}\'::jsonb '
        "WHERE flow_run_id = :r AND digest = ANY(:d)",
        {"r": run.id, "d": list(edge.evidence)},
        "EVIDENCE_DIGEST_MISMATCH",
    )
    # A flow evidence row cited through a scope item: the scope digest no longer verifies.
    tamper(
        "UPDATE flow_callsites SET cfg_node = cfg_node + 1 "
        "WHERE flow_evidence_run_id = :r AND scope_key IN ("
        "SELECT scope_key FROM flow_scopes "
        "WHERE flow_evidence_run_id = :r AND name = 'create_item')",
        {"r": run.flow_evidence_run_id},
        "CITED_ROW_CHANGED:flow_scopes",
    )
    # A cited Stage 14 element (the endpoint the handler edge cites).
    handled = g.into(create, "handled_by")[0]
    with pg_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE architecture_nodes SET node_digest = repeat('0', 64) "
                "WHERE architecture_run_id = :r AND node_key = :k"
            ),
            {"r": run.architecture_run_id, "k": handled.source_key},
        )
    failures = stack.store.verify(run, [], [handled.key])
    assert any(f.startswith("CITED_ROW_CHANGED:architecture_nodes") for f in failures)
    # A deleted edge breaks the graph digest even though every remaining element verifies.
    with pg_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM flow_edges WHERE flow_run_id = :r AND edge_key = :k"),
            {"r": run.id, "k": edge.key},
        )
    assert "ELEMENT_MISSING" in stack.store.verify(run, [], [edge.key])
    assert stack.store.integrity(run)["verified"] is False


def test_rebuild_short_circuits_and_portable_digest_reproduces(
    pg_engine: Engine, tmp_path: Path
) -> None:
    first = FlowStack(pg_engine, tmp_path / "a")
    run = first.build()
    assert run.graph_digest and run.portable_digest and run.revision == 1
    assert first.build().id == run.id  # unchanged inputs: the active run is returned
    # A second project over identical bytes: every database id differs, the portable digest
    # (keys, semantics and id-free evidence) does not.
    copy = FlowStack(pg_engine, tmp_path / "b")
    other = copy.build()
    assert other.id != run.id and other.flow_evidence_run_id != run.flow_evidence_run_id
    assert other.portable_digest == run.portable_digest
    assert other.graph_digest != run.graph_digest


def test_flow_model_is_non_interfering_and_goes_stale_with_its_inputs(
    pg_engine: Engine, tmp_path: Path
) -> None:
    stack = FlowStack(pg_engine, tmp_path)
    before = _fingerprint(pg_engine)
    run = stack.build()
    # INV-3: pre-existing tables are byte-identical over their pre-existing columns.
    assert _fingerprint(pg_engine) == before
    with pg_engine.connect() as connection:
        pointer = connection.execute(
            text("SELECT active_flow_run_id FROM project_versions WHERE id = :id"),
            {"id": stack.snapshot_id},
        ).scalar_one()
    assert pointer == run.id
    assert stack.stack.catalog.status(stack.project_id, stack.snapshot_id).freshness == "fresh"
    assert stack.architecture.status(stack.project_id, stack.snapshot_id).freshness == "fresh"
    assert stack.evidence.status(stack.project_id, stack.snapshot_id).freshness == "fresh"
    status = stack.flow.status(stack.project_id, stack.snapshot_id)
    assert status.freshness == "fresh" and status.inputs_ready

    # INV-4: a later structural run stales every input; current reads fail closed.
    stack.stack.structural.analyse(stack.project_id, stack.snapshot_id)
    status = stack.flow.status(stack.project_id, stack.snapshot_id)
    assert (
        status.freshness == "stale" and "STRUCTURAL_AUTHORITY_CHANGED" in status.freshness_reasons
    )
    with pytest.raises(FlowStaleError):
        stack.flow.edges(stack.project_id, stack.snapshot_id, None)
    history = stack.flow.edges(stack.project_id, stack.snapshot_id, run.id, limit=5)
    assert history["edges"] and "STRUCTURAL_AUTHORITY_CHANGED" in history["freshness_reasons"]


def test_flow_model_http_contract(pg_engine: Engine, tmp_path: Path) -> None:
    write(tmp_path, FLOW_REPOSITORY)
    app = create_app(Settings(database_url=pg_engine.url.render_as_string(hide_password=False)))
    with TestClient(app) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "Flow", "root_path": str(tmp_path)}
        ).json()["id"]
        base = f"/api/v1/projects/{project}/snapshots"
        scope = f"{base}/{client.post(base, json={}).json()['id']}"
        flow = scope + "/flow"
        assert client.get(flow).json()["freshness"] == "none"
        assert client.get(flow + "/edges").json()["detail"]["code"] == "FLOW_REQUIRED"
        for step in ("/structure", "/processing", "/semantic/catalog", "/architecture"):
            assert client.post(scope + step, json={}).status_code == 200, step
        assert client.post(flow + "/evidence", json={}).status_code == 200
        rejected = client.post(flow, content="{}", headers={"Content-Type": "text/plain"})
        assert rejected.status_code == 415
        built = client.post(flow, json={})
        assert built.status_code == 200, built.text
        body = built.json()
        assert body["freshness"] == "fresh" and body["active_run"]["outcome"] == "complete"
        run_id = body["active_run"]["run_id"]
        assert body["active_run"]["contract"]["api_matching_evaluation"] == "not_human_evaluated"
        page = client.get(flow + "/edges?relation=calls_endpoint").json()
        assert page["run_id"] == run_id and page["verification"] == "verified"
        assert page["total"] == 2 and all(HTTP_CAVEAT in e["conditions"] for e in page["items"])
        endpoint = page["items"][0]["target_key"]
        node = client.get(flow + "/node", params={"key": endpoint}).json()
        assert node["node"]["kind"] == "api_endpoint" and node["outgoing"]
        assert {e["family"] for e in node["incoming"]} == {"resolution"}
        assert client.get(flow + "/node", params={"key": "nope"}).status_code == 404
        assert client.get(flow + "/integrity").json()["verified"] is True
        assert client.get(flow + "/diagnostics").json()["items"] == []
        assert client.get(f"{flow}/runs/{run_id}").json()["run_id"] == run_id
        assert client.get(flow + "/runs").json()["total"] == 1
        # A later structural run: current reads fail closed, history stays readable.
        assert client.post(scope + "/structure", json={}).status_code == 200
        stale = client.get(flow + "/edges")
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "FLOW_STALE"
        labelled = client.get(f"{flow}/edges?run_id={run_id}&limit=1").json()
        assert "STRUCTURAL_AUTHORITY_CHANGED" in labelled["freshness_reasons"]
        rebuilt = client.post(flow, json={})
        assert rebuilt.status_code == 409  # inputs are stale; nothing rebuilds them implicitly


def test_truncated_scope_is_diagnosed_and_the_run_is_partial(
    pg_engine: Engine, tmp_path: Path
) -> None:
    calls = "".join(f"    step({i})\n" for i in range(1100))  # above the 1,024 per-scope cap
    files = {**FLOW_REPOSITORY, "backend/app/big.py": "def step(i):\n    return i\n\n\n"
             "def big():\n" + calls}  # fmt: skip
    stack = FlowStack(pg_engine, tmp_path, files)
    run = stack.build()
    assert run.outcome == "partial"  # truncated evidence never yields a complete model
    truncated = [d for d in stack.store.diagnostics(run.id) if d.code == "FLOW_SCOPE_TRUNCATED"]
    assert [d.subject for d in truncated] == [Graph(stack, run).scope("backend/app/big.py", "big")]
    assert truncated[0].attributes == {"limit": "FLOW_CALLSITE_LIMIT"}


def test_downgrade_refuses_while_flow_model_runs_exist(pg_engine: Engine, tmp_path: Path) -> None:
    FlowStack(pg_engine, tmp_path).build()
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "alembic"))
    with pytest.raises(Exception, match="Stage 15 flow model runs or module facts exist"):
        command.downgrade(config, "0017_flow_evidence")
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0019_flow_explanations"
        )
