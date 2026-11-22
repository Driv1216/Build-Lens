"""S8 designed resource attacks: extraction caps and traversal must stay bounded."""

from __future__ import annotations

import pytest

from buildlens.application.flow_trace import TraceEngine, TraceGraph, _conjoin
from buildlens.domain.flow import FlowEdge, FlowNode
from buildlens.domain.flow_evidence import CALLSITES_PER_SCOPE
from buildlens.domain.flow_trace import Atom, TraceQuery, decide_dnf
from buildlens.infrastructure.structural.python_flow import extract_python_flow
from buildlens.infrastructure.structural.typescript_flow import extract_ts_flow


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_ten_thousand_calls_are_truncated_without_claiming_complete_coverage(language: str) -> None:
    if language == "python":
        result = extract_python_flow(("def bomb():\n" + "    target()\n" * 10_000).encode(), {})
    else:
        result = extract_ts_flow(
            "bomb.ts", ("function bomb() {\n" + "target();\n" * 10_000 + "}\n").encode(), []
        )
    assert result.outcome == "partial"
    assert any(s.truncated == "FLOW_CALLSITE_LIMIT" for s in result.scopes)
    assert all(len(s.callsites) <= CALLSITES_PER_SCOPE for s in result.scopes)


def test_deep_python_expression_fails_closed() -> None:
    result = extract_python_flow(("x = " + "(" * 2000 + "1" + ")" * 2000).encode(), {})
    assert result.outcome == "failed" and not result.scopes


def _graph(count: int, cycle: bool, relation: str = "invokes") -> TraceGraph:
    nodes = {f"n{i}": FlowNode(f"n{i}", "scope", str(i), None, {}) for i in range(count)}
    edges = [
        FlowEdge(
            f"e{i}",
            relation,
            "n0" if not cycle else f"n{i}",
            f"n{i + 1}" if i + 1 < count else "n0",
            "derived",
            "test",
            (),
            {},
        )
        for i in range(count if cycle else count - 1)
    ]
    return TraceGraph.of("run", "graph", nodes, edges)


def test_recursion_scc_and_generator_shaped_chain_stop_at_trace_bounds() -> None:
    engine = TraceEngine(_graph(120, True), lambda _: {})
    trace = engine.forward(TraceQuery("n0", max_depth=12, max_hops=40))
    assert trace["status"] == "truncated_depth" and len(trace["hops"]) <= 40
    generator_chain = TraceEngine(_graph(120, True, "resumes"), lambda _: {}).forward(
        TraceQuery("n0", max_depth=12, max_hops=40)
    )
    assert generator_chain["status"] == "truncated_depth"


def test_fanout_bomb_stops_at_the_hop_budget() -> None:
    trace = TraceEngine(_graph(2000, False), lambda _: {}).forward(TraceQuery("n0", max_hops=32))
    assert trace["status"] == "truncated_limit" and len(trace["hops"]) <= 32


def test_approximated_dnf_never_refutes_a_dropped_alternative() -> None:
    contradictory = (
        Atom("eq", ("s", "v", (0,)), True, ("int", 1)),
        Atom("eq", ("s", "v", (0,)), True, ("int", 2)),
    )
    assert decide_dnf([contradictory] * 16, approximate=True) == "undetermined"
    assert decide_dnf([contradictory] * 16, approximate=False) == "refuted_in_fragment"
    terms, approximate = _conjoin([contradictory] * 20, [contradictory] * 20, False)
    assert len(terms) == 16 and approximate
    assert decide_dnf(terms, approximate) == "undetermined"
