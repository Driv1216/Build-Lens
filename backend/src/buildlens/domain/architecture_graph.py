"""Deterministic graph algorithms for the architecture model (no external dependencies).

Every algorithm is iterative (no recursion limit on deep import chains), visits neighbours in
sorted order and breaks ties lexicographically, so results and witness paths are
reproducible. Edges are identified by key so that every witness hop cites a persisted edge.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class GraphEdge:
    key: str
    source: str
    target: str
    import_class: str | None = None


class Graph:
    """Directed multigraph restricted to an allowed set of import classes.

    Parallel edges between one pair collapse to the edge with the smallest key, which is the
    edge cited by witness paths.
    """

    def __init__(
        self,
        edges: Iterable[GraphEdge],
        classes: frozenset[str] | None = None,
        nodes: Iterable[str] = (),
    ) -> None:
        out: dict[str, dict[str, str]] = {}
        inc: dict[str, dict[str, str]] = {}
        self.nodes: set[str] = set(nodes)
        for edge in edges:
            if classes is not None and edge.import_class not in classes:
                continue
            self.nodes.add(edge.source)
            self.nodes.add(edge.target)
            current = out.setdefault(edge.source, {}).get(edge.target)
            if current is None or edge.key < current:
                out[edge.source][edge.target] = edge.key
                inc.setdefault(edge.target, {})[edge.source] = edge.key
        self.out: dict[str, tuple[tuple[str, str], ...]] = {
            node: tuple(sorted(targets.items())) for node, targets in out.items()
        }
        self.inc: dict[str, tuple[tuple[str, str], ...]] = {
            node: tuple(sorted(sources.items())) for node, sources in inc.items()
        }

    def successors(self, node: str) -> tuple[tuple[str, str], ...]:
        return self.out.get(node, ())

    def predecessors(self, node: str) -> tuple[tuple[str, str], ...]:
        return self.inc.get(node, ())

    def neighbours(self, node: str, direction: Literal["out", "in"]) -> tuple[tuple[str, str], ...]:
        return self.successors(node) if direction == "out" else self.predecessors(node)

    def has_self_loop(self, node: str) -> bool:
        return any(target == node for target, _ in self.successors(node))


def strongly_connected_components(graph: Graph) -> list[tuple[str, ...]]:
    """Iterative Tarjan. Each component is sorted; components are sorted by first member.

    Every node is returned, including singletons; callers decide which components matter.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[tuple[str, ...]] = []
    counter = 0
    for root in sorted(graph.nodes):
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, position = work.pop()
            if position == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            successors = graph.successors(node)
            recurse = False
            while position < len(successors):
                target = successors[position][0]
                position += 1
                if target not in index:
                    work.append((node, position))
                    work.append((target, 0))
                    recurse = True
                    break
                if target in on_stack:
                    low[node] = min(low[node], index[target])
            if recurse:
                continue
            if low[node] == index[node]:
                members: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    members.append(member)
                    if member == node:
                        break
                components.append(tuple(sorted(members)))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return sorted(components)


def cyclic_components(graph: Graph) -> list[tuple[str, ...]]:
    """Components with more than one member (self-loops are reported separately)."""
    return [component for component in strongly_connected_components(graph) if len(component) > 1]


def shortest_cycle(graph: Graph, start: str, allowed: set[str]) -> tuple[str, ...] | None:
    """Edge keys of a shortest cycle through ``start`` inside ``allowed`` (BFS, sorted)."""
    parent: dict[str, tuple[str, str]] = {}
    queue: deque[str] = deque([start])
    seen = {start}
    while queue:
        node = queue.popleft()
        for target, key in graph.successors(node):
            if target not in allowed:
                continue
            if target == start:
                path = [key]
                cursor = node
                while cursor != start:
                    previous, edge = parent[cursor]
                    path.append(edge)
                    cursor = previous
                return tuple(reversed(path))
            if target not in seen:
                seen.add(target)
                parent[target] = (node, key)
                queue.append(target)
    return None


SearchStatus = Literal["complete", "truncated_depth", "truncated_limit", "deadline"]


@dataclass(slots=True)
class Reach:
    depth: int
    via: str | None  # predecessor node on the witness path
    edge: str | None  # edge key of the last hop


@dataclass(slots=True)
class SearchResult:
    origins: frozenset[str]
    reached: dict[str, Reach] = field(default_factory=dict)
    status: SearchStatus = "complete"
    frontier: int = 0

    def path(self, node: str) -> tuple[str, ...]:
        """Edge keys from the nearest origin to ``node`` (empty for an origin)."""
        edges: list[str] = []
        cursor = node
        while cursor not in self.origins:
            reach = self.reached[cursor]
            assert reach.via is not None and reach.edge is not None
            edges.append(reach.edge)
            cursor = reach.via
        return tuple(reversed(edges))


def breadth_first(
    graph: Graph,
    origin: str | Sequence[str],
    direction: Literal["out", "in"],
    max_depth: int,
    limit: int,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> SearchResult:
    """Bounded BFS. ``status`` says whether the search was exhaustive; a truncated search
    never supports a claim of non-reachability."""
    origins = sorted({origin} if isinstance(origin, str) else set(origin))
    result = SearchResult(frozenset(origins), {item: Reach(0, None, None) for item in origins})
    queue: deque[str] = deque(origins)
    while queue:
        if deadline is not None and clock() > deadline:
            result.status = "deadline"
            result.frontier = len(queue)
            return result
        node = queue.popleft()
        depth = result.reached[node].depth
        for target, key in graph.neighbours(node, direction):
            if target in result.reached:
                continue
            if depth + 1 > max_depth:
                result.status = "truncated_depth"
                result.frontier += 1
                continue
            if len(result.reached) - len(origins) >= limit:
                result.status = "truncated_limit"
                result.frontier = len(queue) + 1
                return result
            result.reached[target] = Reach(depth + 1, node, key)
            queue.append(target)
    return result


def shortest_path(
    graph: Graph,
    origin: str,
    target: str,
    max_depth: int,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[tuple[str, ...] | None, SearchStatus]:
    """Edge keys of a shortest path, and whether a negative answer is exhaustive."""
    if origin == target:
        return (), "complete"
    reached: dict[str, tuple[str, str]] = {}
    depth = {origin: 0}
    queue: deque[str] = deque([origin])
    status: SearchStatus = "complete"
    while queue:
        if deadline is not None and clock() > deadline:
            return None, "deadline"
        node = queue.popleft()
        for successor, key in graph.successors(node):
            if successor in depth:
                continue
            if depth[node] + 1 > max_depth:
                status = "truncated_depth"
                continue
            depth[successor] = depth[node] + 1
            reached[successor] = (node, key)
            if successor == target:
                edges: list[str] = []
                cursor = target
                while cursor != origin:
                    previous, edge = reached[cursor]
                    edges.append(edge)
                    cursor = previous
                return tuple(reversed(edges)), "complete"
            queue.append(successor)
    return None, status


def condensation_order(graph: Graph) -> dict[str, int]:
    """Longest-path rank of every node on the SCC condensation (members share a rank)."""
    components = strongly_connected_components(graph)
    owner = {
        member: position for position, component in enumerate(components) for member in component
    }
    successors: dict[int, set[int]] = {position: set() for position in range(len(components))}
    indegree = dict.fromkeys(successors, 0)
    for position, component in enumerate(components):
        for member in component:
            for target, _ in graph.successors(member):
                other = owner[target]
                if other != position and other not in successors[position]:
                    successors[position].add(other)
                    indegree[other] += 1
    rank = dict.fromkeys(successors, 0)
    ready = sorted(position for position, value in indegree.items() if value == 0)
    queue: deque[int] = deque(ready)
    while queue:
        position = queue.popleft()
        for other in sorted(successors[position]):
            rank[other] = max(rank[other], rank[position] + 1)
            indegree[other] -= 1
            if indegree[other] == 0:
                queue.append(other)
    return {member: rank[owner[member]] for member in owner}


def edges_on_path(keys: Sequence[str], by_key: Mapping[str, GraphEdge]) -> list[GraphEdge]:
    return [by_key[key] for key in keys]
