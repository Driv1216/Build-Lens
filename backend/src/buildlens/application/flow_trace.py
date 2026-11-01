"""Stage 15 Milestone C: the ``FlowTrace`` engine (``flow-trace.v1``).

A pure, bounded walk over one verified flow model run plus the scope CFGs it cites. It reads
PostgreSQL only through the loaders it is given (INV-1) and never infers execution order from
source order: independently scheduled work runs in its own lane (``ORDER_NOT_DETERMINED``)
unless a semantics contract states an order (plan §3.5).

Each step keeps the four verdicts apart (``domain.flow_trace``): the edge's own resolution,
CFG reachability in its scope, the accumulated control-dependence path condition, and
feasibility in the sound fragment. A structural path is never presented as an execution.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from buildlens.application.flow_inputs import ScopeInput, ScopeView
from buildlens.domain import flow_cfg as C
from buildlens.domain.flow import FAMILIES, HTTP_CAVEAT, FlowEdge, FlowNode, tier_rank
from buildlens.domain.flow_trace import (
    ASSUMPTIONS_JS,
    ASSUMPTIONS_PY,
    DEADLINE_SECONDS,
    DNF_TERMS,
    FLOW_TRACE_VERSION,
    WITNESS_NOTE,
    Atom,
    TraceQuery,
    atom_json,
    decide_dnf,
    literal_key,
)

ScopeLoader = Callable[[list[tuple[str, str]]], dict[tuple[str, str], ScopeInput]]

# Relations that run (or may run) the target's body, in the order a scope's children appear.
_DESCEND = {
    "invokes",
    "awaits",
    "invokes_until_first_await",
    "iterates",
    "resumes",
    "throws_into",
    "invokes_callback",
    "dispatch_candidate",
    "registers",
    "may_rerun_effect",
    "continues_with",
    "injects",
    "enters",
    "issues_request",
    "calls_endpoint",
    "handled_by",
    "raises_to",
    "exits_with",
    "response_boundary",
    "performs",
}
# Relations recorded as leaf steps: effects, boundaries, creations whose body does not run.
_LEAF = {
    "on_table",
    "in_transaction",
    "schedules_render",
    "navigates",
    "may_refetch",
    "crosses_boundary",
    "constructs",
    "creates_coroutine",
    "creates_generator",
    "creates_async_generator",
    "responds",
    "unresolved",
}
_BACK = {
    "invokes",
    "awaits",
    "invokes_until_first_await",
    "iterates",
    "resumes",
    "throws_into",
    "invokes_callback",
    "dispatch_candidate",
    "registers",
    "may_rerun_effect",
    "continues_with",
    "injects",
    "enters",
    "handled_by",
    "calls_endpoint",
    "issues_request",
    "contains_callsite",
    "raises_to",
    "exits_with",
}
# Unresolved callsites whose callee could be anything (they block "no callers" claims).
_DYNAMIC = {
    "DYNAMIC_CALLEE",
    "SUBSCRIPT_CALLEE",
    "CALL_RESULT_RECEIVER",
    "CALLABLE_PARAMETER",
    "CALLABLE_PARAM",
    "DYNAMIC_RECEIVER",
    "HANDLER_FROM_PROPS",
    "DYNAMIC_HANDLER",
    "PROP_CALLBACK_UNBOUND",
    "STAR_IMPORT",
}
_BOUNDARY_REASONS = (
    "BUILTIN",
    "GLOBAL",
    "EXTERNAL_PACKAGE:",
    "CALLABLE_EXT_INSTANCE",
    "EXTERNAL_CALL_RESULT",
)
_ORDERED_LANES = {
    "main": "SEQUENTIAL_BY_CFG_WITHIN_A_SCOPE",
    "framework_response_send:teardown": "ORDERED_BY_FASTAPI_VERSION_TABLE",
    "tanstack_mutation": "ORDERED_BY_TANSTACK_CONTRACT",
}
_BUILTIN_VALUE_TYPES = {"int", "str", "bool", "float", "bytes", "tuple"}


# ----------------------------------------------------------------------------- the graph


@dataclass(slots=True)
class TraceGraph:
    """One run's nodes and edges with adjacency, built once per run (cached by the service)."""

    run_id: str
    graph_digest: str
    nodes: dict[str, FlowNode]
    out: dict[str, list[FlowEdge]]
    inc: dict[str, list[FlowEdge]]
    handlers: set[str]
    dependencies: set[str]
    unresolved_tails: dict[tuple[str, str], int]
    dynamic_languages: set[str]
    descendants: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    @classmethod
    def of(
        cls, run_id: str, graph_digest: str, nodes: dict[str, FlowNode], edges: list[FlowEdge]
    ) -> TraceGraph:
        out: dict[str, list[FlowEdge]] = defaultdict(list)
        inc: dict[str, list[FlowEdge]] = defaultdict(list)
        handlers: set[str] = set()
        dependencies: set[str] = set()
        tails: dict[tuple[str, str], int] = defaultdict(int)
        dynamic: set[str] = set()
        for e in edges:
            out[e.source_key].append(e)
            if e.target_key:
                inc[e.target_key].append(e)
            if e.relation == "handled_by" and e.target_key:
                handlers.add(e.target_key)
            if e.relation == "injects" and e.target_key:
                dependencies.add(e.target_key)
            if e.relation == "unresolved" and e.source_key in nodes:
                site = nodes[e.source_key]
                language = str(site.attributes.get("language") or "")
                if set(e.conditions) & _DYNAMIC:
                    dynamic.add(language)
                tails[(language, _tail(site.label))] += 1
        descendants: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for key in sorted(nodes):
            if not key.startswith("scope:"):
                continue
            locator, scope = _split(key)
            parts = scope.split(".")
            for cut in range(1, len(parts)):
                descendants[f"scope:{locator}#{'.'.join(parts[:cut])}"].append((locator, scope))
        return cls(
            run_id, graph_digest, dict(nodes), dict(out), dict(inc), handlers, dependencies,
            dict(tails), dynamic, dict(descendants),
        )  # fmt: skip

    def entries(self, kind: str | None = None) -> list[FlowNode]:
        return sorted(
            (
                n
                for n in self.nodes.values()
                if n.kind == "entry_point" and (kind is None or n.attributes.get("kind") == kind)
            ),
            key=lambda n: (str(n.attributes.get("kind")), n.label, n.key),
        )


# ----------------------------------------------------------------------------- per-scope facts


@dataclass(slots=True)
class _ScopeFacts:
    """CFG-derived facts of one scope: reachability, guards, admissibility of locals."""

    key: str
    view: ScopeView
    language: str
    normal: set[int]
    unblocked: set[int]
    full: set[int]
    dependence: dict[int, tuple[C.Dependence, ...]]
    inadmissible: set[str]
    setup: set[int]
    teardown: set[int]
    guards: dict[int, tuple[list[tuple[Atom, ...]], bool]] = field(default_factory=dict)
    cyclic: dict[int, bool] = field(default_factory=dict)

    def in_loop(self, node: int) -> bool:
        if not self.cyclic:
            self.cyclic = {n: False for n in range(self.view.graph.count)}
            for n in _cyclic_nodes(self.view.graph):
                self.cyclic[n] = True
        return self.cyclic.get(node, False)


def _cyclic_nodes(graph: C.Graph) -> set[int]:
    """Nodes on a cycle (an SCC larger than one node, or a self loop): iterative Tarjan."""
    succ = [[t for t, _, _ in edges] for edges in graph.successors()]
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    found: set[int] = set()
    counter = 0
    for root in range(graph.count):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, i = work.pop()
            if i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            if i < len(succ[node]):
                work.append((node, i + 1))
                nxt = succ[node][i]
                if nxt not in index:
                    work.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            if low[node] == index[node]:
                component = []
                while True:
                    top = stack.pop()
                    on_stack.discard(top)
                    component.append(top)
                    if top == node:
                        break
                if len(component) > 1 or node in succ[node]:
                    found.update(component)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return found


def _reach(graph: C.Graph, start: int, blocked: set[int], stop: set[int]) -> set[int]:
    """Nodes reachable over normal edges from ``start``; ``blocked`` nodes have no normal
    continuation and ``stop`` nodes are entered but not left."""
    succ = graph.successors(normal_only=True)
    seen, stack = {start}, [start]
    while stack:
        node = stack.pop()
        if node in blocked or (node in stop and node != start):
            continue
        for target, _, _ in succ[node]:
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


# ----------------------------------------------------------------------------- the engine


@dataclass(slots=True)
class _Hop:
    id: str
    node: str
    context: str | None
    depth: int
    lane: str
    terms: list[tuple[Atom, ...]]
    approximate: bool
    phase: str | None = None  # "setup" / "teardown": the part of a generator body that runs


class TraceEngine:
    def __init__(
        self,
        graph: TraceGraph,
        load_scopes: ScopeLoader,
        coverage: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.g = graph
        self._load = load_scopes
        self._facts: dict[str, _ScopeFacts | None] = {}
        self._coverage = coverage or {}
        self._clock = clock
        self.used_scopes: dict[str, str] = {}  # scope key -> scope digest the trace used

    # ----------------------------------------------------------------- scope facts

    def facts(self, scope: str) -> _ScopeFacts | None:
        if scope in self._facts:
            return self._facts[scope]
        node = self.g.nodes.get(scope)
        if node is None or not scope.startswith("scope:") or node.attributes.get("placeholder"):
            self._facts[scope] = None
            return None
        locator, key = _split(scope)
        loaded = self._load([(locator, key), *self.g.descendants.get(scope, [])])
        own = loaded.get((locator, key))
        if own is None or not own.scope.cfg:
            self._facts[scope] = None
            return None
        self.used_scopes[scope] = own.scope.digest
        view = ScopeView.of(own)
        graph = view.graph
        blocked = {
            c.cfg_node
            for c in own.scope.callsites
            if any(
                e.attributes.get("normal_continuation") is False
                for e in self.g.out.get(_call(scope, c.ordinal), [])
            )
        }
        yields = {n["id"] for n in own.scope.cfg["nodes"] if n.get("kind") == "yield"}
        normal = graph.reachable(C.ENTRY, normal_only=True)
        succ = graph.successors()
        after_yield = {t for y in yields for t, _, _ in succ[y]}
        teardown: set[int] = set()
        for start in after_yield:
            teardown |= _reach(graph, start, blocked, set())
        inadmissible: set[str] = set()
        summary = own.scope.summary
        if summary.get("dynamic") or summary.get("uses_with"):
            inadmissible.add("*")
        inadmissible |= set(summary.get("nonlocal") or []) | set(summary.get("global") or [])
        for child in loaded.values():
            if child is own:
                continue
            inadmissible |= set(child.scope.summary.get("nonlocal") or [])
            declared = {
                b["name"]
                for b in child.scope.summary.get("bindings") or []
                if b["kind"] != "assign"
            }
            inadmissible |= {
                b["name"]
                for b in child.scope.summary.get("bindings") or []
                if b["kind"] == "assign" and b["name"] not in declared
            }
        facts = _ScopeFacts(
            scope,
            view,
            own.language,
            normal,
            _reach(graph, C.ENTRY, blocked, set()),
            graph.reachable(C.ENTRY, normal_only=False),
            C.control_dependence(graph),
            inadmissible,
            _reach(graph, C.ENTRY, blocked, yields) if yields else set(),
            teardown,
        )
        self._facts[scope] = facts
        return facts

    def reachability(self, facts: _ScopeFacts | None, node: int | None) -> dict[str, Any]:
        if facts is None or node is None:
            return {"verdict": "scope_reached"}
        if node not in facts.full:
            return {"verdict": "unreachable_static", "code": "UNREACHABLE_STATIC"}
        if node in facts.unblocked:
            return {"verdict": "reachable_in_model"}
        if node in facts.normal:
            return {"verdict": "unreachable_after_non_returning_call"}
        return {
            "verdict": "reachable_if_raises",
            "raised_at_cfg_nodes": list(C.exceptional_sources(facts.view.graph, node)),
        }

    def local_guards(
        self, facts: _ScopeFacts | None, node: int | None
    ) -> tuple[list[tuple[Atom, ...]], bool]:
        if facts is None or node is None:
            return [()], False
        if node in facts.guards:
            return facts.guards[node]
        found = C.guards(facts.view.graph, node, facts.dependence, DNF_TERMS)
        terms = [tuple(self.atom(facts, d) for d in term) for term in found.terms] or [()]
        facts.guards[node] = (terms, found.approximate)
        return facts.guards[node]

    def atom(self, facts: _ScopeFacts, dep: C.Dependence) -> Atom:
        value = self._atom(facts, dep)
        if facts.in_loop(dep.branch):
            return value
        return Atom(
            value.op, value.var, value.value, value.lit, value.text, value.reason,
            (facts.key, dep.branch),
        )  # fmt: skip

    def _atom(self, facts: _ScopeFacts, dep: C.Dependence) -> Atom:
        payload = facts.view.input.scope.cfg["nodes"][dep.branch].get("atom") or {}
        if dep.kind not in {C.TRUE, C.FALSE}:
            return Atom("opaque", None, True, reason=f"BRANCH_{dep.kind.upper()}")
        polarity = dep.kind == C.TRUE
        op, name = payload.get("op"), payload.get("var")
        if op == "const":
            return Atom("const", None, polarity, lit=literal_key(_lit(payload.get("lit"))))
        if op in {None, "opaque", "isinstance", "instanceof"} or not isinstance(name, str):
            return Atom("opaque", None, polarity, reason=str(payload.get("reason") or op))
        var = self.variable(facts, name, dep.branch)
        if var is None:
            return Atom("opaque", None, polarity, reason="VARIABLE_NOT_ADMISSIBLE")
        negated = bool(payload.get("neg"))
        value = polarity != negated
        python = facts.language == "Python"
        if op == "is_none" or op == "nullish":
            return Atom("nullish", var, value)
        if op == "typeof":
            return Atom("typeof", var, value, text=str(payload.get("type")))
        if op in {"is_lit", "strict_eq"}:
            return Atom("eq", var, value, lit=literal_key(_lit(payload.get("lit"))))
        if op in {"eq", "truthy"}:
            if python and not self._value_typed(facts, name, dep.branch):
                return Atom("opaque", None, polarity, reason="OVERRIDABLE_OPERATOR")
            if op == "truthy":
                return Atom("truthy", var, polarity)
            return Atom("eq", var, value, lit=literal_key(_lit(payload.get("lit"))))
        return Atom("opaque", None, polarity, reason=str(op))

    def variable(
        self, facts: _ScopeFacts, name: str, node: int
    ) -> tuple[str, str, tuple[int, ...]] | None:
        """(scope, name, reaching definitions) for an admissible local, else ``None``."""
        if "*" in facts.inadmissible or name in facts.inadmissible:
            return None
        if name not in facts.view.by_name:
            return None
        reaching = facts.view.reaching_at(name, node)
        if not reaching:
            return None
        return (facts.key, name, tuple(sorted(b["ordinal"] for b in reaching)))

    def _value_typed(self, facts: _ScopeFacts, name: str, node: int) -> bool:
        """Python ``==``/truthiness are builtin only when every reaching definition is a literal
        or a builtin value-type constructor (annotations do not establish this)."""
        reaching = facts.view.reaching_at(name, node)
        if not reaching:
            return False
        for b in reaching:
            value = b.get("value") or {}
            if b["kind"] not in {"assign", "annassign"}:
                return False
            if value.get("k") == "lit":
                continue
            callee = value.get("callee") or {}
            if (
                value.get("k") == "call"
                and callee.get("k") == "name"
                and callee.get("id") in _BUILTIN_VALUE_TYPES
                and callee.get("id") not in facts.view.by_name
            ):
                continue
            return False
        return True

    def known_values(self, facts: _ScopeFacts | None, node: int | None) -> tuple[Atom, ...]:
        """A local whose every reaching definition is the same literal has that value."""
        if facts is None or node is None:
            return ()
        values: list[Atom] = []
        for name in sorted(facts.view.by_name):
            var = self.variable(facts, name, node)
            if var is None:
                continue
            keys = {
                literal_key(_lit(b.get("value")))
                for b in facts.view.reaching_at(name, node)
                if b["kind"] in {"assign", "annassign", "declare"}
                and (b.get("value") or {}).get("k") == "lit"
            }
            if len(keys) == 1 and None not in keys and len(var[2]) == 1:
                values.append(Atom("eq", var, True, lit=next(iter(keys))))
        return tuple(values)

    # ----------------------------------------------------------------- forward

    def forward(self, query: TraceQuery) -> dict[str, Any]:
        return _Walk(self, query).forward()

    def backward(self, query: TraceQuery) -> dict[str, Any]:
        return _Walk(self, query).backward()

    def path(self, source: str, target: str, max_depth: int, max_hops: int) -> dict[str, Any]:
        """One shortest structural witness from ``source`` to ``target`` (L3 only)."""
        start = self.g.nodes.get(source)
        if start is None or target not in self.g.nodes:
            return {"found": False, "reason": "NODE_NOT_FOUND", "steps": []}
        parents: dict[str, FlowEdge | None] = {source: None}
        frontier, depth = [source], 0
        while frontier and depth < max_depth and len(parents) < max_hops:
            nxt: list[str] = []
            for current in frontier:
                for e in sorted(self.g.out.get(current, []), key=lambda x: x.key):
                    if (
                        e.relation not in _DESCEND | _LEAF | {"contains_callsite"}
                        or e.relation in {"unresolved", "responds"}
                        or not e.target_key
                    ):
                        continue
                    if e.target_key in parents:
                        continue
                    parents[e.target_key] = e
                    if e.target_key == target:
                        frontier = []
                        break
                    nxt.append(e.target_key)
                if target in parents:
                    break
            if target in parents:
                break
            frontier, depth = nxt, depth + 1
        if target not in parents:
            return {
                "found": False,
                "reason": "NO_STRUCTURAL_PATH_WITHIN_BOUNDS",
                "steps": [],
                "note": "absence within bounds is not proof that no execution path exists",
            }
        chain: list[FlowEdge] = []
        cursor = target
        while parents[cursor] is not None:
            edge = parents[cursor]
            assert edge is not None
            chain.append(edge)
            cursor = edge.source_key
        chain.reverse()
        return {
            "found": True,
            "level": "L3_structural_witness",
            "note": WITNESS_NOTE,
            "steps": [_edge_json(e) for e in chain],
            "path_tier": _weakest([e.tier for e in chain]),
            "caveats": sorted({HTTP_CAVEAT for e in chain if HTTP_CAVEAT in e.conditions}),
        }

    # ----------------------------------------------------------------- absence

    def absence(self, scope: str) -> dict[str, Any]:
        """Backward "no callers" claims are coverage-gated (plan §3.8)."""
        node = self.g.nodes[scope]
        reasons: list[str] = []
        artifacts = self._coverage.get("artifacts") or {}
        if any(k != "extracted" and v for k, v in artifacts.items()):
            reasons.append("COVERAGE_INCOMPLETE")
        language = str(node.attributes.get("language") or "")
        if language in self.g.dynamic_languages:
            reasons.append("DYNAMIC_CALLSITES_PRESENT")
        if self.g.unresolved_tails.get((language, node.label)):
            reasons.append("UNRESOLVED_CALLSITE_WITH_SAME_NAME")
        if node.attributes.get("kind") == "module":
            reasons.append("MODULE_LOADED_BY_IMPORT_OR_RUNTIME")
        return {
            "verdict": "ABSENCE_NOT_DETERMINABLE" if reasons else "no_callers_in_model",
            "reasons": reasons,
        }


class _Walk:
    """One trace computation: hops, steps, segments, lanes, gaps, effects."""

    def __init__(self, engine: TraceEngine, query: TraceQuery) -> None:
        self.e, self.q, self.g = engine, query, engine.g
        self.hops: list[_Hop] = []
        self.steps: list[dict[str, Any]] = []
        self.segments: list[dict[str, Any]] = []
        self.gaps: list[dict[str, Any]] = []
        self.boundaries: list[dict[str, Any]] = []
        self.absence: list[dict[str, Any]] = []
        self.seen: dict[tuple[str, str | None, str | None], str] = {}
        self.lanes: dict[str, dict[str, Any]] = {}
        self.status_flags: set[str] = set()
        self.deadline = engine._clock() + DEADLINE_SECONDS
        self.languages: set[str] = set()

    # ----------------------------------------------------------------- hops and steps

    def hop(
        self,
        node: str,
        context: str | None,
        depth: int,
        lane: str,
        terms: list[tuple[Atom, ...]],
        approximate: bool,
        phase: str | None = None,
    ) -> _Hop:
        value = _Hop(f"H{len(self.hops)}", node, context, depth, lane, terms, approximate, phase)
        self.hops.append(value)
        self.seen[(node, context, phase)] = value.id
        self.lanes.setdefault(
            lane,
            {
                "id": lane,
                "order": _ORDERED_LANES.get(lane, "ORDER_NOT_DETERMINED"),
            },
        )
        return value

    def budget(self, depth: int) -> bool:
        if self.e._clock() > self.deadline:
            self.status_flags.add("deadline")
            return False
        if len(self.hops) >= self.q.max_hops:
            self.status_flags.add("truncated_limit")
            return False
        if depth >= self.q.max_depth:
            self.status_flags.add("truncated_depth")
            return False
        return True

    def step(
        self,
        parent: _Hop,
        edge: FlowEdge,
        target: _Hop | None,
        reach: dict[str, Any],
        terms: list[tuple[Atom, ...]],
        approximate: bool,
        lane: str,
        **extra: Any,
    ) -> dict[str, Any]:
        verdict = decide_dnf(terms, approximate)
        language = self.language_of(parent.node)
        value = {
            "id": f"S{len(self.steps)}",
            "from": parent.id,
            "to": target.id if target else None,
            "edge": edge.key,
            "relation": edge.relation,
            "family": FAMILIES.get(edge.relation, "other"),
            "resolution": {
                "tier": edge.tier,
                "rule": edge.rule,
                "conditions": list(edge.conditions),
            },
            "reachability": reach,
            "path_condition": {
                "terms": [[atom_json(a) for a in term] for term in terms if term],
                "approximate": approximate,
            },
            "feasibility": {
                "verdict": verdict,
                "fragment": "flow-feasibility.v1",
                "assumptions": list(ASSUMPTIONS_JS if language != "Python" else ASSUMPTIONS_PY),
            },
            "attributes": dict(edge.attributes),
            "lane": lane,
            "evidence": list(edge.evidence),
            **extra,
        }
        self.steps.append(value)
        return value

    def language_of(self, key: str) -> str:
        node = self.g.nodes.get(key)
        if node is None:
            return ""
        language = node.attributes.get("language")
        if language:
            return str(language)
        return "Python" if (node.locator or "").endswith(".py") else "TypeScript"

    # ----------------------------------------------------------------- forward walk

    def forward(self) -> dict[str, Any]:
        root = self.g.nodes.get(self.q.entry)
        if root is None:
            raise KeyError(self.q.entry)
        start = self.hop(root.key, None, 0, _entry_lane(root), [()], False)
        self.expand(start, (start.node,))
        return self.result()

    def expand(self, hop: _Hop, active: tuple[str, ...]) -> None:
        node = self.g.nodes.get(hop.node)
        if node is None:
            return
        if node.attributes.get("truncated"):
            self.gaps.append({"hop": hop.id, "kind": "truncated_scope", "node": hop.node})
        if node.attributes.get("placeholder"):
            self.gaps.append({"hop": hop.id, "kind": "opaque_function", "node": hop.node})
        if hop.node.startswith("scope:"):
            self.expand_scope(hop, active)
        else:
            for edge in self.ordered(hop.node, hop):
                self.follow(hop, edge, None, None, active)

    def ordered(self, key: str, hop: _Hop) -> list[FlowEdge]:
        edges = [
            e
            for e in self.g.out.get(key, [])
            if e.relation in _DESCEND | _LEAF and e.relation != "resolves_to"
        ]
        if key == "scheduler:framework_response_send":
            # Only the current request's teardowns and background tasks.
            handler = _handler_of(self.hops, hop, self.g)
            edges = [e for e in edges if e.attributes.get("handler") in {handler, None}]
            edges = [e for e in edges if e.attributes.get("step") != "send"]
        return sorted(edges, key=_edge_order)

    def expand_scope(self, hop: _Hop, active: tuple[str, ...]) -> None:
        facts = self.e.facts(hop.node)
        if facts is not None:
            self.languages.add(facts.language)
        outgoing = self.g.out.get(hop.node, [])
        setup = sorted(
            (
                e
                for e in outgoing
                if e.relation == "injects" and e.attributes.get("phase") == "setup"
            ),
            key=lambda e: e.key,
        )
        for edge in setup:
            self.follow(hop, edge, facts, None, active)
        sites = sorted(
            (e for e in outgoing if e.relation == "contains_callsite" and e.target_key),
            key=lambda e: int(e.attributes.get("ordinal") or 0),
        )
        later = sorted(
            (
                e
                for e in outgoing
                if e.relation in {"registers", "invokes", "performs"}
                and e.attributes.get("phase") in {None, "definition"}
            ),
            key=_edge_order,
        )
        for edge in sites:
            site = self.g.nodes.get(edge.target_key or "")
            if site is None:
                continue
            cfg_node = site.attributes.get("cfg_node")
            if not self.in_phase(facts, hop, cfg_node, site.attributes.get("copies")):
                continue
            representative = _representative(facts, cfg_node, site.attributes.get("copies"))
            for site_edge in self.ordered(site.key, hop):
                self.follow(hop, site_edge, facts, representative, active, via=site.key)
        for edge in later:
            self.follow(hop, edge, facts, edge.attributes.get("cfg_node"), active)
        if hop.phase != "setup":
            for edge in sorted(
                (e for e in outgoing if e.relation == "exits_with"), key=lambda e: e.key
            ):
                self.follow(hop, edge, facts, None, active)

    def in_phase(
        self, facts: _ScopeFacts | None, hop: _Hop, cfg_node: Any, copies: Any = ()
    ) -> bool:
        """The part of a generator body a phase runs (``finally`` copies count)."""
        if facts is None or hop.phase is None or not isinstance(cfg_node, int):
            return True
        nodes = {cfg_node, *(c for c in copies or () if isinstance(c, int))}
        if hop.phase == "setup":
            return not facts.setup or bool(nodes & facts.setup)
        if hop.phase == "teardown":
            return bool(nodes & facts.teardown)
        return True

    def follow(
        self,
        hop: _Hop,
        edge: FlowEdge,
        facts: _ScopeFacts | None,
        cfg_node: Any,
        active: tuple[str, ...],
        via: str | None = None,
    ) -> None:
        if not self.budget(hop.depth + 1):
            return
        node = cfg_node if isinstance(cfg_node, int) else None
        if edge.relation == "response_boundary" and edge.attributes.get("callsite"):
            # A background task is registered only where its ``add_task`` call is reached.
            registered = self.g.nodes.get(str(edge.attributes["callsite"]))
            handler = str(edge.attributes.get("handler") or "")
            if registered is not None and isinstance(registered.attributes.get("cfg_node"), int):
                facts, node = self.e.facts(handler), registered.attributes["cfg_node"]
        local, approximate = self.e.local_guards(facts, node)
        known = self.e.known_values(facts, node) if local != [()] else ()
        terms, approximate = _conjoin(
            hop.terms, [(*t, *known) for t in local], hop.approximate or approximate
        )
        reach = self.e.reachability(facts, node)
        lane = _lane(hop.lane, edge)
        extra: dict[str, Any] = {"callsite": via} if via else {}
        line = self.g.nodes[via].attributes.get("line") if via and via in self.g.nodes else None
        if isinstance(line, int):
            extra["line"] = line
        target_key = edge.target_key
        if edge.relation == "unresolved" or target_key is None:
            step = self.step(hop, edge, None, reach, terms, approximate, lane, **extra)
            reasons = list(edge.conditions)
            record = {"step": step["id"], "reasons": reasons, "callsite": via or edge.source_key}
            if all(r.startswith(_BOUNDARY_REASONS) for r in reasons):
                self.boundaries.append(record)
            else:
                self.gaps.append({**record, "kind": "unresolved_callsite"})
            return
        if edge.relation in _LEAF:
            target = self.leaf(target_key, hop, lane, terms, approximate)
            if edge.relation.startswith("creates_"):
                extra["body_runs"] = False
            self.step(hop, edge, target, reach, terms, approximate, lane, **extra)
            return
        phase = _phase(edge)
        # Each outcome expands its own response boundary (its own path condition).
        context = (
            via
            if target_key.startswith("scope:")
            else edge.source_key
            if edge.relation == "response_boundary"
            else None
        )
        existing = self.seen.get((target_key, context, phase))
        if target_key in active:
            self.step(
                hop,
                edge,
                self.hop_by_id(existing),
                reach,
                terms,
                approximate,
                lane,
                cycle_ref=True,
                **extra,
            )
            return
        if existing is not None:
            self.step(
                hop,
                edge,
                self.hop_by_id(existing),
                reach,
                terms,
                approximate,
                lane,
                shared=True,
                **extra,
            )
            return
        target = self.hop(target_key, context, hop.depth + 1, lane, terms, approximate, phase)
        step = self.step(hop, edge, target, reach, terms, approximate, lane, **extra)
        self.segment(hop, edge, step, via)
        if edge.relation == "dispatch_candidate":
            step["candidate"] = True
        self.expand(target, (*active, target_key))

    def leaf(
        self,
        key: str,
        parent: _Hop,
        lane: str,
        terms: list[tuple[Atom, ...]],
        approximate: bool,
    ) -> _Hop:
        existing = self.seen.get((key, None, None))
        if existing is not None:
            found = self.hop_by_id(existing)
            if found is not None:
                return found
        return self.hop(key, None, parent.depth + 1, lane, terms, approximate)

    def hop_by_id(self, hop_id: str | None) -> _Hop | None:
        if hop_id is None:
            return None
        return self.hops[int(hop_id[1:])]

    def segment(self, hop: _Hop, edge: FlowEdge, step: dict[str, Any], via: str | None) -> None:
        """Ambiguity splits the trace: one segment per alternative, never a choice."""
        source = via or edge.source_key
        siblings = [
            e
            for e in self.g.out.get(source, [])
            if e.relation
            in {
                "invokes",
                "awaits",
                "invokes_until_first_await",
                "dispatch_candidate",
                "calls_endpoint",
                "invokes_callback",
            }
            and "phase" not in e.attributes
            and e.target_key
            and e.target_key.startswith(("scope:", "endpoint:"))
        ]
        targets = sorted({e.target_key for e in siblings if e.target_key})
        reason = None
        if len(targets) > 1:
            reason = "AMBIGUOUS_CANDIDATE"
        if "SEMANTICS_ALTERNATIVE" in edge.conditions:
            reason = "SEMANTICS_ALTERNATIVE"
        if reason is None:
            return
        existing = next(
            (s for s in self.segments if s["at"] == source and s["reason"] == reason), None
        )
        if existing is None:
            existing = {
                "id": f"G{len(self.segments)}",
                "at": source,
                "reason": reason,
                "alternatives": targets,
                "steps": [],
            }
            self.segments.append(existing)
        existing["steps"].append(step["id"])
        step["segment"] = existing["id"]

    # ----------------------------------------------------------------- backward walk

    def backward(self) -> dict[str, Any]:
        root = self.g.nodes.get(self.q.entry)
        if root is None:
            raise KeyError(self.q.entry)
        start = self.hop(root.key, None, 0, "main", [()], False)
        frontier = [start]
        while frontier:
            current = frontier.pop(0)
            incoming = sorted(
                (e for e in self.g.inc.get(current.node, []) if e.relation in _BACK),
                key=lambda e: e.key,
            )
            if current.node.startswith("scope:") and not [
                e for e in incoming if e.relation != "contains_callsite"
            ]:
                self.absence.append({"hop": current.id, **self.e.absence(current.node)})
            for edge in incoming:
                if not self.budget(current.depth + 1):
                    break
                existing = self.seen.get((edge.source_key, None, None))
                if existing is not None:
                    self.step(
                        current, edge, self.hop_by_id(existing), {"verdict": "scope_reached"},
                        [()], False, "main", shared=True, direction="backward",
                    )  # fmt: skip
                    continue
                source = self.hop(edge.source_key, None, current.depth + 1, "main", [()], False)
                self.step(
                    current, edge, source, {"verdict": "scope_reached"}, [()], False, "main",
                    direction="backward",
                )  # fmt: skip
                if self.g.nodes.get(edge.source_key, None) is None or (
                    self.g.nodes[edge.source_key].kind == "entry_point"
                ):
                    continue
                frontier.append(source)
        return self.result()

    # ----------------------------------------------------------------- result

    def result(self) -> dict[str, Any]:
        if "deadline" in self.status_flags:
            status = "deadline"
        elif "truncated_limit" in self.status_flags:
            status = "truncated_limit"
        elif "truncated_depth" in self.status_flags:
            status = "truncated_depth"
        elif self.gaps:
            status = "incomplete_evidence"
        else:
            status = "complete"
        tiers = [s["resolution"]["tier"] for s in self.steps if s["relation"] != "unresolved"]
        caveats = sorted(
            {
                c
                for s in self.steps
                for c in s["resolution"]["conditions"]
                if c in {HTTP_CAVEAT, "SEMANTICS_ALTERNATIVE", "FASTAPI_VERSION_UNKNOWN"}
            }
        )
        hops = []
        for h in self.hops:
            node = self.g.nodes.get(h.node)
            hops.append(
                {
                    "id": h.id,
                    "node": h.node,
                    "kind": node.kind if node else None,
                    "label": node.label if node else None,
                    "locator": node.locator if node else None,
                    "context": h.context,
                    "depth": h.depth,
                    "lane": h.lane,
                    "layer": _layer(node, h.node, self.g),
                    "identity": node.attributes.get("identity") if node else None,
                    **({"phase": h.phase} if h.phase else {}),
                }
            )
        return {
            "contract": FLOW_TRACE_VERSION,
            "direction": self.q.direction,
            "status": status,
            "hops": hops,
            "steps": self.steps,
            "segments": self.segments,
            "lanes": sorted(self.lanes.values(), key=lambda lane: lane["id"]),
            "gaps": self.gaps,
            "boundaries": self.boundaries,
            "caveats": caveats,
            "path_tier": _weakest(tiers),
            "effects": _effects(self) if self.q.direction == "forward" else [],
            "absence": self.absence,
            "levels": {
                "L1": "resolution (tier, rule, conditions)",
                "L2": "static reachability in the model",
                "L3": "structural path in the over-approximate model; not an execution",
                "L4": "feasibility in the sound fragment (flow-feasibility.v1)",
            },
        }


# ----------------------------------------------------------------------------- database effects


def _effects(walk: _Walk) -> list[dict[str, Any]]:
    """Path-level database effects (``sqlalchemy-ops.v2``): staged / sent / commit reached /
    rolled back, and the commit's position relative to the response."""
    g = walk.g
    operations: list[dict[str, Any]] = []
    sides: dict[str, list[str]] = {}
    for s in walk.steps:
        if s["relation"] == "response_boundary" and s.get("to"):
            hop = walk.hop_by_id(s["to"])
            edge_attributes = next(
                (
                    e.attributes
                    for e in g.out.get("scheduler:framework_response_send", [])
                    if e.key == s["edge"]
                ),
                {},
            )
            if hop is not None and edge_attributes.get("step") == "teardown":
                sides[hop.node] = list(edge_attributes.get("sides") or [])
    for s in walk.steps:
        if s["relation"] != "performs" or not s.get("to"):
            continue
        hop = walk.hop_by_id(s["to"])
        node = g.nodes.get(hop.node) if hop else None
        if node is None or node.kind not in {"db_operation", "transaction_boundary"}:
            continue
        parent = walk.hop_by_id(s["from"])
        operations.append(
            {
                "step": s["id"],
                "node": node.key,
                "class": node.attributes.get("class") or "transaction_scope",
                "callsite": s.get("callsite"),
                "scope": parent.node if parent else None,
                "lane": s["lane"],
                "phase": parent.phase if parent else None,
                "tables": sorted(
                    e.target_key or "" for e in g.out.get(node.key, []) if e.relation == "on_table"
                ),
                "transaction": next(
                    (
                        e.target_key
                        for e in g.out.get(node.key, [])
                        if e.relation == "in_transaction"
                    ),
                    None,
                ),
            }
        )
    commits = [o for o in operations if o["class"] == "commit"]
    rollbacks = [o for o in operations if o["class"] == "rollback"]
    effects: list[dict[str, Any]] = []
    for op in operations:
        if op["class"] not in {"stage", "dml_executed", "flush"}:
            continue
        same = [c for c in commits if _same_session(walk, op, c)]
        injected = [c for c in commits if _injected_session(walk, op, c)]
        conditions: list[str] = []
        if op["transaction"]:
            state = "commit_reached"
            conditions.append("TRANSACTION_SCOPE_COMMITS_ON_NORMAL_COMPLETION")
            position = _position(op, sides)
        elif same:
            state = "commit_reached"
            conditions.append("COMMIT_MAY_RAISE")
            position = _position(same[0], sides)
        elif injected:
            state = "commit_reached"
            conditions += ["COMMIT_MAY_RAISE", "SESSION_IDENTITY_BY_PARAMETER"]
            position = _position(injected[0], sides)
        elif commits:
            state = "commit_reached_session_unknown"
            conditions.append("SESSION_IDENTITY_UNKNOWN")
            position = _position(commits[0], sides)
        else:
            state = "staged" if op["class"] == "stage" else "sent_uncommitted"
            position = None
        if any(_same_session(walk, op, r) for r in rollbacks):
            conditions.append("ROLLED_BACK_ON_PATH")
        effects.append(
            {
                "step": op["step"],
                "operation": op["node"],
                "class": op["class"],
                "tables": op["tables"],
                "state": state,
                "commit_position": position,
                "conditions": sorted(conditions),
                "claim": "a commit is reached on the path; it may still raise"
                if state.startswith("commit")
                else "no commit on this path",
            }
        )
    return effects


def _same_session(walk: _Walk, a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Same scope, same receiver shape (same reaching definitions for a local), and ``b``'s
    callsite reachable from ``a``'s over normal CFG edges."""
    if a["scope"] != b["scope"] or not a["callsite"] or not b["callsite"]:
        return False
    facts = walk.e.facts(a["scope"])
    if facts is None:
        return False
    sites = {c.ordinal: c for c in facts.view.input.scope.callsites}
    first, second = sites.get(_ordinal(a["callsite"])), sites.get(_ordinal(b["callsite"]))
    if first is None or second is None:
        return False
    left, right = _receiver(first.callee), _receiver(second.callee)
    if left is None or left != right:
        return False
    root = (first.callee.get("root") or {}).get("id")
    if root and first.callee.get("root", {}).get("k") == "name":
        a_defs = {d["ordinal"] for d in facts.view.reaching_at(root, first.cfg_node)}
        b_defs = {d["ordinal"] for d in facts.view.reaching_at(root, second.cfg_node)}
        if a_defs != b_defs:
            return False
    return second.cfg_node in _reach(facts.view.graph, first.cfg_node, set(), set())


def _injected_session(walk: _Walk, op: dict[str, Any], commit: dict[str, Any]) -> bool:
    """The operation's receiver is a parameter injected by the dependency whose own body
    (setup or teardown) commits: the same session by parameter (inferred)."""
    if not op["callsite"] or not op["scope"] or not commit["scope"]:
        return False
    facts = walk.e.facts(op["scope"])
    if facts is None:
        return False
    site = next(
        (c for c in facts.view.input.scope.callsites if c.ordinal == _ordinal(op["callsite"])),
        None,
    )
    root = (site.callee.get("root") or {}) if site else {}
    if root.get("k") != "name":
        return False
    return any(
        e.relation == "injects"
        and e.attributes.get("param") == root.get("id")
        and e.target_key == commit["scope"]
        for e in walk.g.out.get(op["scope"], [])
    )


def _position(op: dict[str, Any], sides: dict[str, list[str]]) -> dict[str, Any]:
    if op["lane"] == "fastapi_background":
        return {"code": "COMMIT_AFTER_RESPONSE_SENT", "via": "background_task"}
    if op["phase"] == "teardown" or op["lane"] == "framework_response_send:teardown":
        values = sides.get(op["scope"] or "", [])
        codes = sorted(
            {
                "COMMIT_AFTER_RESPONSE_SENT"
                if side == "after_response_send"
                else "COMMIT_BEFORE_RESPONSE_SEND"
                for side in values
            }
        )
        return {"code": codes[0] if len(codes) == 1 else "SEMANTICS_ALTERNATIVE",
                "alternatives": codes, "via": "dependency_teardown"}  # fmt: skip
    return {"code": "COMMIT_BEFORE_RESPONSE", "via": "handler_or_callee"}


# ----------------------------------------------------------------------------- helpers


def _representative(facts: _ScopeFacts | None, node: Any, copies: Any) -> Any:
    """A ``finally`` body is lowered once per completion kind; report the copy on a normal
    path when there is one (the others are reached only when something raises)."""
    if facts is None or not isinstance(node, int):
        return node
    for candidate in (node, *(c for c in copies or () if isinstance(c, int))):
        if candidate in facts.unblocked:
            return candidate
    return node


def _conjoin(
    left: list[tuple[Atom, ...]], right: list[tuple[Atom, ...]], approximate: bool
) -> tuple[list[tuple[Atom, ...]], bool]:
    terms: list[tuple[Atom, ...]] = []
    for a in left or [()]:
        for b in right or [()]:
            terms.append((*a, *b))
            if len(terms) > DNF_TERMS:
                return terms[:DNF_TERMS], True
    return terms, approximate


def _lane(current: str, edge: FlowEdge) -> str:
    if edge.relation in {"registers", "may_rerun_effect"}:
        scheduler = edge.attributes.get("scheduler")
        if edge.relation == "may_rerun_effect":
            return "react_commit"
        return str(scheduler) if scheduler else current
    if edge.relation == "continues_with":
        return "tanstack_mutation"
    if edge.relation == "invokes_callback" and edge.attributes.get("scheduler"):
        return str(edge.attributes["scheduler"])
    if edge.relation == "response_boundary":
        step = edge.attributes.get("step")
        if step == "teardown":
            return "framework_response_send:teardown"
        if step == "background":
            return "fastapi_background"
    return current


def _entry_lane(node: FlowNode) -> str:
    kind = node.attributes.get("kind")
    if kind == "dom_event":
        return f"dom_event:{node.attributes.get('event')}"
    if kind == "effect":
        return str(node.attributes.get("scheduler") or "react_commit")
    return "main"


def _phase(edge: FlowEdge) -> str | None:
    if edge.relation == "injects":
        return "setup"
    if edge.relation == "response_boundary" and edge.attributes.get("step") == "teardown":
        return "teardown"
    if edge.relation == "resumes" and edge.attributes.get("phase") in {"enter", "exit"}:
        return "setup" if edge.attributes["phase"] == "enter" else "teardown"
    return None


_ORDER = {
    "injects": 0,
    "enters": 0,
    "handled_by": 0,
    "calls_endpoint": 0,
    "issues_request": 1,
    "invokes": 2,
    "awaits": 2,
    "invokes_until_first_await": 2,
    "dispatch_candidate": 3,
    "resumes": 3,
    "iterates": 3,
    "throws_into": 3,
    "invokes_callback": 4,
    "continues_with": 4,
    "performs": 5,
    "constructs": 6,
    "registers": 7,
    "may_rerun_effect": 8,
    "raises_to": 9,
    "response_boundary": 10,
}


def _edge_order(e: FlowEdge) -> tuple[Any, ...]:
    return (
        _ORDER.get(e.relation, 20),
        e.attributes.get("cfg_node") if isinstance(e.attributes.get("cfg_node"), int) else -1,
        e.key,
    )


def _handler_of(hops: list[_Hop], hop: _Hop, g: TraceGraph) -> str | None:
    """The handler scope whose outcome led to the response-send scheduler (nearest earlier)."""
    for candidate in reversed(hops[: int(hop.id[1:]) + 1]):
        if candidate.node in g.handlers:
            return candidate.node
    return None


def _layer(node: FlowNode | None, key: str, g: TraceGraph) -> str:
    if node is None:
        return "Unknown"
    if node.kind in {"http_request", "api_endpoint"}:
        return "HTTP"
    if node.kind in {"db_operation", "db_table", "transaction_boundary"}:
        return "Data"
    if node.kind == "integration_boundary":
        return "External"
    if node.kind in {"scheduler", "entry_point"}:
        return "Scheduler" if node.kind == "scheduler" else "Entry"
    if key in g.handlers or node.kind == "response_outcome":
        return "Route"
    if key in g.dependencies:
        return "Dependencies"
    if (node.locator or "").endswith((".ts", ".tsx", ".js", ".jsx")):
        return "Frontend"
    if node.kind in {"state_update", "navigation", "storage_effect"}:
        return "Frontend"
    return "Service"


def _weakest(tiers: Iterable[str]) -> str:
    values = list(tiers)
    return max(values, key=tier_rank) if values else "observed"


def _edge_json(e: FlowEdge) -> dict[str, Any]:
    return {
        "edge": e.key,
        "relation": e.relation,
        "source": e.source_key,
        "target": e.target_key,
        "tier": e.tier,
        "rule": e.rule,
        "conditions": list(e.conditions),
    }


def _split(key: str) -> tuple[str, str]:
    body = key.removeprefix("scope:")
    locator, _, scope = body.rpartition("#")
    return locator, scope


def _call(scope: str, ordinal: int) -> str:
    locator, key = _split(scope)
    return f"call:{locator}#{key}:{ordinal}"


def _ordinal(callsite: str) -> int:
    return int(callsite.rsplit(":", 1)[-1])


def _receiver(callee: dict[str, Any]) -> tuple[Any, ...] | None:
    if callee.get("k") != "attr":
        return None
    root = callee.get("root") or {}
    if root.get("k") not in {"name", "this"}:
        return None
    return (root.get("k"), root.get("id"), *list(callee.get("path") or [])[:-1])


def _tail(label: str) -> str:
    return label.removesuffix("()").rsplit(".", 1)[-1]


def _lit(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if k not in {"k", "path"}}
