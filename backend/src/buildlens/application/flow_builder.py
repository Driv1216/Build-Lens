"""Stage 15 Milestone B: build the flow model (``flow-model.v1``) as a pure function of rows.

Order: Python and TS/JS scopes and callsites (resolution, construction, framework semantics),
FastAPI handlers from Stage 14 endpoints, Stage 14 span stitching of request sites, response
mapping to request sites, then digests. Fails closed above the node/edge limits.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from buildlens.application.flow_inputs import (
    Emitter,
    FlowInput,
    Stage14Element,
    call_key,
    scope_key,
)
from buildlens.application.flow_python import PythonFlow
from buildlens.application.flow_typescript import TypeScriptFlow
from buildlens.domain import flow_cfg as C
from buildlens.domain.flow import (
    HTTP_CAVEAT,
    MAX_EDGES,
    MAX_NODES,
    RULES,
    FlowBuild,
    graph_digest_of,
)


class FlowBuildError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# FastAPI yield-dependency teardown, verified from source at 0.105.0 … 0.157.0 (A0).
_VERIFIED = ((0, 105, 0), (0, 157, 0))
_ROWS: list[tuple[tuple[int, int, int], tuple[int, int, int], list[dict[str, str]]]] = [
    (
        (0, 0, 0),
        (0, 106, 0),
        [{"scope": "any", "teardown": "after_response_send", "background": "before_teardown"}],
    ),
    (
        (0, 106, 0),
        (0, 118, 0),
        [
            {
                "scope": "any",
                "teardown": "before_response_send",
                "background": "after_send_after_teardown",
            }
        ],
    ),
    (
        (0, 118, 0),
        (0, 121, 0),
        [{"scope": "any", "teardown": "after_response_send", "background": "before_teardown"}],
    ),
    (
        (0, 121, 0),
        (99, 0, 0),
        [
            {
                "scope": "function",
                "teardown": "before_response_send",
                "background": "after_send_after_teardown",
            },
            {
                "scope": "request",
                "teardown": "after_response_send",
                "background": "before_teardown",
            },
        ],
    ),
]
_PROBES = [
    (0, 105, 0),
    (0, 106, 0),
    (0, 117, 0),
    (0, 118, 0),
    (0, 120, 0),
    (0, 121, 0),
    (0, 157, 0),
]


def _version(text: str | None) -> tuple[int, int, int] | None:
    if not text:
        return None
    parts = [int(p) for p in re.findall(r"\d+", text)[:3]]
    return tuple((parts + [0, 0, 0])[:3]) if parts else None  # type: ignore[return-value]


def _satisfies(version: tuple[int, int, int], constraint: str) -> bool:
    for clause in constraint.split(","):
        match = re.match(r"\s*(==|>=|<=|~=|!=|>|<)?\s*([\w.*]+)", clause)
        if not match:
            continue
        op, bound = match.group(1) or "==", _version(match.group(2))
        if bound is None:
            continue
        if op == "~=":
            upper = (bound[0], bound[1] + 1, 0)
            if not (bound <= version < upper):
                return False
        elif not {
            "==": version == bound,
            ">=": version >= bound,
            "<=": version <= bound,
            "!=": version != bound,
            ">": version > bound,
            "<": version < bound,
        }[op]:
            return False
    return True


def _rows_for(version: tuple[int, int, int]) -> list[dict[str, str]]:
    return next(rows for low, high, rows in _ROWS if low <= version < high)


@dataclass(slots=True)
class Facts:
    """Shared framework facts for the language passes."""

    inp: FlowInput
    maps_table: dict[tuple[str, str], list[Stage14Element]] = field(default_factory=dict)
    app_conditions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _nodes: dict[str, Stage14Element] | None = None

    def stage14_node(self, key: str) -> Stage14Element | None:
        if self._nodes is None:
            self._nodes = {n.key: n for n in self.inp.stage14_nodes}
        return self._nodes.get(key)

    def fastapi_alternatives(self, locator: str) -> dict[str, Any]:
        directory = posixpath.dirname(locator)
        facts = [
            f
            for f in self.inp.framework_facts
            if f.ecosystem == "python" and f.package == "fastapi"
        ]
        facts = [
            f
            for f in facts
            if directory == f.directory
            or directory.startswith(f.directory + "/")
            or not f.directory
        ]
        facts.sort(key=lambda f: (f.source_kind == "declared", -len(f.directory)))
        if not facts:
            return {
                "version": None,
                "alternatives": [r for _, _, rows in _ROWS for r in rows],
                "conditions": ("FASTAPI_VERSION_UNKNOWN", "SEMANTICS_ALTERNATIVE"),
            }
        fact = facts[0]
        if fact.version:
            version = _version(fact.version)
            if version is None:
                return {
                    "version": fact.version,
                    "alternatives": [r for _, _, rows in _ROWS for r in rows],
                    "conditions": ("FASTAPI_VERSION_UNKNOWN", "SEMANTICS_ALTERNATIVE"),
                }
            conditions: tuple[str, ...] = (
                () if _VERIFIED[0] <= version <= _VERIFIED[1] else ("FASTAPI_VERSION_UNVERIFIED",)
            )
            return {
                "version": fact.version,
                "alternatives": _rows_for(version),
                "conditions": conditions,
            }
        probes = [p for p in _PROBES if _satisfies(p, fact.constraint or "")]
        groups = {id(_rows_for(p)): _rows_for(p) for p in probes}  # distinct version rows
        rows: list[dict[str, str]] = []
        for group in groups.values():
            for row in group:
                if row not in rows:
                    rows.append(row)
        alternatives = rows or [r for _, _, rs in _ROWS for r in rs]
        conditions = ("FASTAPI_VERSION_DECLARED_RANGE",) + (
            ("SEMANTICS_ALTERNATIVE",) if len(groups) != 1 else ()
        )
        return {"version": fact.constraint, "alternatives": alternatives, "conditions": conditions}


def build_flow(inp: FlowInput) -> FlowBuild:
    em = Emitter(inp.evidence_run.id, inp.architecture_run_id)
    facts = Facts(inp)
    endpoints = [n for n in inp.stage14_nodes if n.kind == "api_endpoint"]
    for edge in inp.stage14_edges:
        if edge.kind == "maps_table" and edge.source_key and edge.target_key:
            model = str(edge.attributes.get("model") or "")
            facts.maps_table.setdefault(
                (edge.source_key.removeprefix("module:"), model.rsplit(".", 1)[-1]), []
            ).append(edge)
    facts.app_conditions = _app_conditions(inp)
    python = PythonFlow(inp, em, facts)
    python.run()
    python.handlers(endpoints)
    typescript = TypeScriptFlow(inp, em, facts)
    typescript.run(endpoints)
    stitched = _stitch(inp, em, typescript)
    _respond(inp, em, typescript)
    if len(em.nodes) > MAX_NODES or len(em.edges) > MAX_EDGES:
        raise FlowBuildError("FLOW_LIMIT_EXCEEDED")
    for emitted in list(em.edges.values()):
        for digest in emitted.evidence:
            assert digest in em.evidence
        if emitted.target_key is not None and emitted.target_key not in em.nodes:
            em.diagnose("EDGE_TARGET_WITHOUT_NODE", emitted.key)
            em.node(emitted.target_key, "scope", emitted.target_key, None, {"placeholder": True})
    nodes = tuple(sorted(em.nodes.values(), key=lambda n: n.key))
    edges = tuple(sorted(em.edges.values(), key=lambda e: e.key))
    diagnostics = tuple(sorted(em.diagnostics, key=lambda d: (d.code, d.subject or "")))
    unresolved = Counter(
        c
        for e in edges
        if e.relation == "unresolved"
        for c in e.conditions
        if c.isupper() or ":" in c
    )
    states = Counter(inp.coverage.values())
    coverage = {
        "nodes": dict(sorted(Counter(n.kind for n in nodes).items())),
        "edges": dict(sorted(Counter(e.relation for e in edges).items())),
        "tiers": dict(sorted(Counter(e.tier for e in edges).items())),
        "unresolved": dict(unresolved.most_common(30)),
        "artifacts": dict(sorted(states.items())),
        "stitched_request_sites": stitched["stitched"],
        "unstitched_request_sites": stitched["unstitched"],
        "handlers": sum(1 for e in edges if e.relation == "handled_by"),
        "diagnostics": dict(sorted(Counter(d.code for d in diagnostics).items())),
    }
    complete = (
        inp.evidence_run.outcome == "complete"
        and stitched["unstitched"] == 0
        and not any(
            d.code in {"ENDPOINT_HANDLER_NOT_FOUND", "FLOW_SCOPE_TRUNCATED"} for d in diagnostics
        )
    )
    evidence = dict(em.evidence)
    return FlowBuild(
        nodes,
        edges,
        tuple(sorted(evidence.values(), key=lambda e: e.digest)),
        diagnostics,
        coverage,
        graph_digest_of(nodes, edges, diagnostics, evidence),
        graph_digest_of(nodes, edges, diagnostics, evidence, portable=True),
        "complete" if complete else "partial",
    )


def _app_conditions(inp: FlowInput) -> dict[str, tuple[str, ...]]:
    """Repository-wide application conditions, keyed by the outcome class they qualify.

    ``all`` qualifies every handler outcome (middleware may rewrite or short-circuit any
    response), ``raise`` the exceptional outcomes (a table-driven registration cannot be
    linked) and ``validation`` the framework 422 (a registered ``RequestValidationError``
    handler wins). Statically registered handlers are linked by ``PythonFlow`` (``raises_to``).
    Attribution is repository-wide, not per application: a stated over-approximation.
    """
    found: dict[str, set[str]] = {"all": set(), "raise": set(), "validation": set()}
    for sc in inp.scopes:
        if sc.language != "Python":
            continue
        for callsite in sc.scope.callsites:
            tail = (
                (callsite.callee.get("path") or [None])[-1]
                if callsite.callee.get("k") == "attr"
                else None
            )
            if tail == "add_middleware":
                found["all"].add("MIDDLEWARE_PRESENT_NOT_MODELLED")
            if tail == "add_exception_handler":
                first = (
                    next((a.get("shape") for a in callsite.arguments if a.get("pos") == 0), None)
                    or {}
                )
                name = (
                    first.get("id")
                    if first.get("k") == "name"
                    else (first.get("path") or [None])[-1]
                    if first.get("k") == "attr"
                    else None
                )
                bindings = [b for b in sc.scope.summary.get("bindings") or [] if b["name"] == name]
                if name is None or (
                    first.get("k") == "name" and any(b["kind"] == "for_target" for b in bindings)
                ):
                    found["raise"].add("EXCEPTION_HANDLER_TABLE_NOT_STATIC")
                elif name == "RequestValidationError":
                    found["validation"].add("VALIDATION_HANDLER_REGISTERED")
        for decorator in sc.scope.summary.get("decorators") or []:
            callee = decorator.get("callee") if decorator.get("k") == "call" else decorator
            if (callee or {}).get("k") == "attr" and (callee.get("path") or [None])[
                -1
            ] == "middleware":
                found["all"].add("MIDDLEWARE_PRESENT_NOT_MODELLED")
    return {k: tuple(sorted(v)) for k, v in found.items()}


def _stitch(inp: FlowInput, em: Emitter, typescript: TypeScriptFlow) -> dict[str, int]:
    """Join Stage 14 request sites to flow callsites by artifact and exact span (never by name)."""
    literals = {(lit.locator, lit.ordinal): lit for lit in inp.literals}
    spans: dict[tuple[str, tuple[Any, ...]], tuple[str, str, int]] = {}
    for sc in inp.scopes:
        for callsite in sc.scope.callsites:
            s = callsite.span
            spans[(sc.locator, (s.start_line, s.start_column, s.end_line, s.end_column))] = (
                sc.locator,
                sc.scope.key,
                callsite.ordinal,
            )
    counts = {"stitched": 0, "unstitched": 0}
    clients = {n.key: n for n in inp.stage14_nodes if n.kind == "client_call"}
    for key, element in sorted(clients.items()):
        match = re.match(r"client:(.+):(\d+)$", key)
        literal = literals.get((match.group(1), int(match.group(2)))) if match else None
        site = spans.get((literal.locator, literal.span)) if literal else None
        ev = em.stage14_ev(element)
        attributes = {
            k: element.attributes.get(k)
            for k in ("sink", "wrapper", "method")
            if k in element.attributes
        }
        em.node(
            key,
            "http_request",
            element.attributes.get("method") or key,
            element.locator,
            {**attributes, "source": "stage14"},
            [ev],
        )
        if site is None:
            counts["unstitched"] += 1
            em.diagnose("STITCH_SPAN_NOT_FOUND", key)
            continue
        counts["stitched"] += 1
        em.edge(
            "issues_request",
            call_key(*site),
            key,
            "derived",
            RULES["stage14_stitching"],
            (),
            {"sink": attributes.get("sink")},
            [ev],
        )
    for edge in inp.stage14_edges:
        if (
            edge.kind != "calls_endpoint"
            or not edge.source_key
            or not edge.target_key
            or edge.source_key not in clients
        ):
            continue
        endpoint = next((n for n in inp.stage14_nodes if n.key == edge.target_key), None)
        if endpoint is not None and endpoint.key not in em.nodes:
            em.node(
                endpoint.key,
                "api_endpoint",
                endpoint.attributes.get("function") or endpoint.key,
                endpoint.locator,
                {
                    "paths": endpoint.attributes.get("paths"),
                    "methods": endpoint.attributes.get("methods"),
                },
                [em.stage14_ev(endpoint)],
            )
        em.edge(
            "calls_endpoint",
            edge.source_key,
            edge.target_key,
            "inferred",
            RULES["stage14_stitching"],
            (*edge.conditions, HTTP_CAVEAT),
            {"api_matching": edge.attributes.get("api_matching")},
            [em.stage14_ev(edge)],
        )
    return counts


def _respond(inp: FlowInput, em: Emitter, typescript: TypeScriptFlow) -> None:
    """``responds``: each handler outcome maps to the request site's continuation."""
    outcomes: dict[str, list[str]] = defaultdict(list)
    handlers: dict[str, str] = {}
    for edge in em.edges.values():
        if edge.relation == "exits_with" and edge.target_key:
            outcomes[edge.source_key].append(edge.target_key)
        if edge.relation == "handled_by" and edge.target_key:
            handlers[edge.source_key] = edge.target_key
    for edge in list(em.edges.values()):
        if edge.relation != "calls_endpoint" or not edge.target_key:
            continue
        request = em.nodes.get(edge.source_key)
        handler = handlers.get(edge.target_key)
        if request is None or handler is None:
            continue
        mapping, conditions = _continuation(request.attributes, typescript)
        for outcome in outcomes.get(handler, []):
            status = em.nodes[outcome].attributes.get("status")
            extra: tuple[str, ...]
            if status is None:
                continuation, extra = "fulfilled_or_rejected", ("RESPONSE_STATUS_UNKNOWN",)
            else:
                continuation, extra = (
                    (mapping["2xx"] if 200 <= int(status) < 300 else mapping["other"]),
                    (),
                )
            em.edge(
                "responds",
                outcome,
                edge.source_key,
                "inferred",
                RULES["fastapi_responses"],
                (*conditions, *extra, HTTP_CAVEAT),
                {"continuation": continuation, "status": status},
                list(edge.evidence),
            )


def _continuation(
    attributes: dict[str, Any], typescript: TypeScriptFlow
) -> tuple[dict[str, str], tuple[str, ...]]:
    sink = attributes.get("sink")
    client = attributes.get("client")
    if client == "openapi-fetch":
        return {"2xx": "fulfilled", "other": "fulfilled"}, ("OPENAPI_FETCH_RESOLVES_WITH_ERROR",)
    if sink == "axios":
        return {"2xx": "fulfilled", "other": "rejected"}, ("AXIOS_DEFAULT_VALIDATE_STATUS",)
    if sink == "fetch":
        wrapper = attributes.get("wrapper")
        if wrapper and _wrapper_throws_on_not_ok(wrapper, typescript):
            return {"2xx": "fulfilled", "other": "rejected"}, ("OK_GUARD_RECOGNIZED",)
        if wrapper:
            return {"2xx": "fulfilled", "other": "fulfilled_or_rejected"}, (
                "RESPONSE_HANDLING_UNKNOWN",
            )
        return {"2xx": "fulfilled", "other": "fulfilled"}, ("FETCH_RESOLVES_FOR_ANY_STATUS",)
    return {"2xx": "fulfilled_or_rejected", "other": "fulfilled_or_rejected"}, (
        "RESPONSE_HANDLING_UNKNOWN",
    )


def _wrapper_throws_on_not_ok(wrapper: str, typescript: TypeScriptFlow) -> bool:
    """The wrapper raises on a path guarded by ``<x>.ok`` being false (the CFG, not text)."""
    locator, _, name = wrapper.partition("#")
    for (loc, _), view in typescript.views.items():
        if loc != locator or view.input.scope.name != name or not view.input.scope.cfg:
            continue
        cfg = view.input.scope.cfg
        graph = view.graph
        for node in cfg["nodes"]:
            if node["kind"] != "raise":
                continue
            for term in C.guards(graph, node["id"]).terms:
                for dep in term:
                    atom = cfg["nodes"][dep.branch].get("atom") or {}
                    if (atom.get("path") or [None])[-1] == "ok" and dep.kind == C.FALSE:
                        return True
    return False


__all__ = ["FlowBuildError", "build_flow", "scope_key"]
