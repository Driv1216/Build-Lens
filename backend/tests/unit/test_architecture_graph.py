"""Stage 14 graph algorithms against brute-force oracles and adversarial shapes."""

from __future__ import annotations

import random

from buildlens.domain.architecture import CLASS_SETS, class_set_of
from buildlens.domain.architecture_graph import (
    Graph,
    GraphEdge,
    breadth_first,
    condensation_order,
    cyclic_components,
    shortest_cycle,
    shortest_path,
    strongly_connected_components,
)


def _closure(nodes: list[str], edges: list[GraphEdge]) -> dict[str, set[str]]:
    reach = {node: {node} for node in nodes}
    for edge in edges:
        reach[edge.source].add(edge.target)
    changed = True
    while changed:
        changed = False
        for node in nodes:
            extra = set().union(*(reach[other] for other in reach[node])) - reach[node]
            if extra:
                reach[node] |= extra
                changed = True
    return reach


def _random_graph(seed: int, size: int, density: float) -> tuple[list[str], list[GraphEdge]]:
    rng = random.Random(seed)
    nodes = [f"n{index:03d}" for index in range(size)]
    edges = [
        GraphEdge(f"e:{a}:{b}", a, b, "unconditional")
        for a in nodes
        for b in nodes
        if a != b and rng.random() < density
    ]
    return nodes, edges


def test_scc_matches_mutual_reachability_on_random_graphs() -> None:
    for seed in range(60):
        nodes, edges = _random_graph(seed, 18, 0.08 + (seed % 5) * 0.03)
        graph = Graph(edges, nodes=nodes)
        closure = _closure(nodes, edges)
        expected = sorted(
            {tuple(sorted(o for o in nodes if o in closure[n] and n in closure[o])) for n in nodes}
        )
        assert strongly_connected_components(graph) == expected


def test_scc_is_iterative_on_deep_chains_and_huge_cycles() -> None:
    chain = [GraphEdge(f"e{i:05d}", f"m{i:05d}", f"m{i + 1:05d}") for i in range(10_000)]
    assert len(cyclic_components(Graph(chain))) == 0
    ring = [GraphEdge(f"r{i:04d}", f"m{i:04d}", f"m{(i + 1) % 5000:04d}") for i in range(5000)]
    components = cyclic_components(Graph(ring))
    assert len(components) == 1 and len(components[0]) == 5000


def test_shortest_cycle_and_paths_are_deterministic_witnesses() -> None:
    edges = [
        GraphEdge("ab", "a", "b"),
        GraphEdge("bc", "b", "c"),
        GraphEdge("ca", "c", "a"),
        GraphEdge("ba", "b", "a"),
        GraphEdge("ab2", "a", "b"),  # parallel edge: the smaller key wins
    ]
    graph = Graph(edges)
    assert shortest_cycle(graph, "a", {"a", "b", "c"}) == ("ab", "ba")
    assert shortest_cycle(graph, "a", {"a", "c"}) is None
    path, status = shortest_path(graph, "a", "c", 32)
    assert path == ("ab", "bc") and status == "complete"
    missing, status = shortest_path(graph, "c", "z", 32)
    assert missing is None and status == "complete"


def test_bounded_search_reports_truncation_instead_of_absence() -> None:
    chain = Graph([GraphEdge(f"e{i}", f"m{i}", f"m{i + 1}") for i in range(10)])
    deep = breadth_first(chain, "m0", "out", 3, 500)
    assert set(deep.reached) == {"m0", "m1", "m2", "m3"} and deep.status == "truncated_depth"
    assert deep.path("m3") == ("e0", "e1", "e2")
    wide = breadth_first(chain, "m0", "out", 32, 2)
    assert wide.status == "truncated_limit"
    complete = breadth_first(chain, "m0", "out", 32, 500)
    assert complete.status == "complete" and len(complete.reached) == 11
    upstream = breadth_first(chain, "m5", "in", 32, 500)
    assert set(upstream.reached) == {f"m{i}" for i in range(6)}
    ticks = iter([0.0, 10.0])
    late = breadth_first(chain, "m0", "out", 32, 500, deadline=1.0, clock=lambda: next(ticks))
    assert late.status == "deadline"
    path, status = shortest_path(chain, "m0", "m9", 3)
    assert path is None and status == "truncated_depth"


def test_class_restriction_and_condensation_rank() -> None:
    edges = [
        GraphEdge("ab", "a", "b", "unconditional"),
        GraphEdge("ba", "b", "a", "type_level"),
        GraphEdge("bc", "b", "c", "deferred"),
    ]
    assert cyclic_components(Graph(edges, CLASS_SETS["S2"])) == []
    assert cyclic_components(Graph(edges, CLASS_SETS["S3"])) == [("a", "b")]
    rank = condensation_order(Graph(edges, CLASS_SETS["S4"]))
    assert rank["a"] == rank["b"] == 0 and rank["c"] == 1
    assert class_set_of(["unconditional"]) == "S0"
    assert class_set_of(["unconditional", "presumed_unconditional"]) == "S1"
    assert class_set_of(["elision_dependent"]) == "S2"
    assert class_set_of(["deferred", "unconditional"]) == "S4"
