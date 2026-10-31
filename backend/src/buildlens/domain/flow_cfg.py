"""Stage 15 statement-level control-flow graphs (``flow-cfg.v1``), language-neutral.

A CFG belongs to one scope. Language passes (``python_flow``, ``typescript_flow``) lower syntax
into it through :class:`CfgBuilder`; this module owns the shared, exact parts of the lowering:

* abrupt completions (``return``, ``raise``, ``break``, ``continue``) are routed outward through
  the enclosing frames. A ``finally`` body is lowered once **per completion kind that reaches
  it** (the classic duplication lowering), so every path in the graph is a real path: there is
  no unpaired "entered by return, left by fall-through" path, and post-dominance is exact;
* exceptional edges go to the innermost handler dispatch, whose ``unmatched`` edge keeps
  propagating unless a handler is a catch-all;
* post-dominators (iterative Cooper–Harvey–Kennedy over the reversed **normal** CFG) and control
  dependence (Ferrante–Ottenstein–Warren), so a guard is a real control dependence, never
  syntactic ancestry.

Exceptional edges are excluded from post-dominance on purpose: with them, every node would
depend on "no earlier call raised". Exceptional reachability is reported separately.

The graph is plain data (canonical JSON) so it can be stored, digested and re-checked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

CFG_VERSION = "flow-cfg.v1"

NORMAL = "normal"
TRUE = "true"
FALSE = "false"
CASE = "case"
NO_MATCH = "no_match"
EXCEPTION = "exception"
LOOP_BACK = "loop_back"
LOOP_EXIT = "loop_exit"
BREAK = "break"
CONTINUE = "continue"
FALLTHROUGH = "fallthrough"
RESUME_NORMAL = "resume_normal"
RESUME_THROW = "resume_throw"
RETURN = "return"
RAISE = "raise"

EDGE_KINDS = frozenset(
    {
        NORMAL,
        TRUE,
        FALSE,
        CASE,
        NO_MATCH,
        EXCEPTION,
        LOOP_BACK,
        LOOP_EXIT,
        BREAK,
        CONTINUE,
        FALLTHROUGH,
        RESUME_NORMAL,
        RESUME_THROW,
        RETURN,
        RAISE,
    }
)
# Edges that only an exception can take; excluded from post-dominance and guards.
EXCEPTIONAL = frozenset({EXCEPTION, RESUME_THROW, RAISE})
# Edges leaving a branch node whose label is the branch outcome.
BRANCHING = frozenset({TRUE, FALSE, CASE, NO_MATCH, LOOP_EXIT})

ENTRY, EXIT, RAISE_EXIT = 0, 1, 2


class CfgLimitError(Exception):
    """The scope exceeded the node cap; the pass records it as truncated."""


@dataclass(slots=True)
class _Frame:
    kind: str  # "loop" | "finally" | "handlers" | "switch"
    label: str | None = None
    break_target: int | None = None
    continue_target: int | None = None
    dispatch: int | None = None
    lower_finally: Callable[[int], int | None] | None = None
    copies: dict[str, int] = field(default_factory=dict)
    # Pending jumps out of a finally copy, wired once the copy's end is known.
    exits: dict[str, int | None] = field(default_factory=dict)


class CfgBuilder:
    """Accumulates nodes and edges; node ids are dense integers in creation order."""

    def __init__(self, max_nodes: int) -> None:
        self.max_nodes = max_nodes
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[list[Any]] = []
        self._frames: list[_Frame] = []
        for kind in ("entry", "exit", "raise_exit"):
            self.node(kind)

    # -- primitives -------------------------------------------------------------------------
    def node(self, kind: str, span: list[int] | None = None, **payload: Any) -> int:
        if len(self.nodes) >= self.max_nodes:
            raise CfgLimitError
        value: dict[str, Any] = {"id": len(self.nodes), "kind": kind}
        if span is not None:
            value["span"] = span
        value.update({key: item for key, item in payload.items() if item is not None})
        self.nodes.append(value)
        return int(value["id"])

    def edge(self, source: int, target: int, kind: str = NORMAL, label: str | None = None) -> None:
        assert kind in EDGE_KINDS, kind
        item: list[Any] = [source, target, kind]
        if label is not None:
            item.append(label)
        if item not in self.edges:
            self.edges.append(item)

    def follow(
        self, source: int | None, kind: str, span: list[int] | None = None, **payload: Any
    ) -> int:
        """A new node reached from ``source`` by a normal edge (an unreachable start if None)."""
        target = self.node(kind, span, **payload)
        if source is not None:
            self.edge(source, target, NORMAL)
        return target

    # -- frames ------------------------------------------------------------------------------
    def push(self, frame: _Frame) -> _Frame:
        self._frames.append(frame)
        return frame

    def pop(self, frame: _Frame) -> None:
        assert self._frames and self._frames[-1] is frame
        self._frames.pop()

    def loop(self, label: str | None, break_target: int, continue_target: int) -> _Frame:
        return self.push(_Frame("loop", label, break_target, continue_target))

    def switch(self, label: str | None, break_target: int) -> _Frame:
        return self.push(_Frame("switch", label, break_target))

    def handlers(self, dispatch: int) -> _Frame:
        return self.push(_Frame("handlers", dispatch=dispatch))

    def finally_(self, lower: Callable[[int], int | None]) -> _Frame:
        return self.push(_Frame("finally", lower_finally=lower))

    # -- abrupt completions ------------------------------------------------------------------
    def jump(self, source: int, completion: str, label: str | None = None) -> None:
        """Route ``return``/``raise``/``break``/``continue`` from ``source`` outward.

        ``completion`` is the jump kind; ``label`` selects a labelled loop/switch. The edge
        kind recorded at each hop is the completion kind, so a path can be read back exactly.
        """
        self._route(source, completion, label, len(self._frames))

    def _route(self, source: int, completion: str, label: str | None, depth: int) -> None:
        for index in range(depth - 1, -1, -1):
            frame = self._frames[index]
            if frame.kind == "handlers" and completion == RAISE:
                assert frame.dispatch is not None
                self.edge(source, frame.dispatch, EXCEPTION)
                return
            if frame.kind in {"loop", "switch"} and completion in {BREAK, CONTINUE}:
                if label is not None and frame.label != label:
                    continue
                if completion == CONTINUE and frame.kind == "switch":
                    continue
                target = frame.break_target if completion == BREAK else frame.continue_target
                assert target is not None
                self.edge(source, target, completion, label)
                return
            if frame.kind == "finally":
                key = f"{completion}:{label or ''}"
                entry = frame.copies.get(key)
                if entry is None:
                    entry = self.node("finally_entry", completion=completion)
                    frame.copies[key] = entry
                    # The copy is lowered with only the frames outside this finally active,
                    # so jumps inside the finally body route correctly.
                    saved = self._frames
                    self._frames = saved[:index]
                    try:
                        assert frame.lower_finally is not None
                        end = frame.lower_finally(entry)
                        if end is not None:
                            self._route(end, completion, label, index)
                    finally:
                        self._frames = saved
                self.edge(source, entry, EXCEPTION if completion == RAISE else completion, label)
                return
        if completion == RETURN:
            self.edge(source, EXIT, RETURN)
        elif completion == RAISE:
            self.edge(source, RAISE_EXIT, RAISE)
        # A break/continue with no target (a syntax error the parser accepted) is dropped.

    def may_raise(self, source: int) -> None:
        """An exceptional edge from a node that can raise (calls, awaits, asserts)."""
        self.jump(source, RAISE)

    def normal_completion_through_finally(self, source: int | None, frame: _Frame) -> int | None:
        """Normal completion of a ``try`` body with ``finally``: run the normal copy."""
        if source is None:
            return None
        key = f"{NORMAL}:"
        entry = frame.copies.get(key)
        if entry is None:
            entry = self.node("finally_entry", completion=NORMAL)
            frame.copies[key] = entry
            saved = self._frames
            index = saved.index(frame)
            self._frames = saved[:index]
            try:
                assert frame.lower_finally is not None
                frame.exits[key] = frame.lower_finally(entry)
            finally:
                self._frames = saved
        self.edge(source, entry, NORMAL)
        return frame.exits[key]

    def graph(self) -> dict[str, Any]:
        return {
            "version": CFG_VERSION,
            "nodes": self.nodes,
            "edges": sorted(self.edges, key=lambda item: (item[0], item[1], item[2], *item[3:])),
        }


# ----------------------------------------------------------------------------- analyses


@dataclass(frozen=True, slots=True)
class Graph:
    """Read-only view of a stored CFG."""

    count: int
    edges: tuple[tuple[int, int, str, str | None], ...]

    @classmethod
    def of(cls, cfg: dict[str, Any]) -> Graph:
        return cls(
            len(cfg["nodes"]),
            tuple(
                (int(e[0]), int(e[1]), str(e[2]), str(e[3]) if len(e) > 3 else None)
                for e in cfg["edges"]
            ),
        )

    def successors(self, normal_only: bool = False) -> list[list[tuple[int, str, str | None]]]:
        value: list[list[tuple[int, str, str | None]]] = [[] for _ in range(self.count)]
        for source, target, kind, label in self.edges:
            if normal_only and kind in EXCEPTIONAL:
                continue
            value[source].append((target, kind, label))
        return value

    def reachable(self, start: int = ENTRY, normal_only: bool = False) -> set[int]:
        succ = self.successors(normal_only)
        seen = {start}
        stack = [start]
        while stack:
            for target, _, _ in succ[stack.pop()]:
                if target not in seen:
                    seen.add(target)
                    stack.append(target)
        return seen


VIRTUAL_EXIT = -1


def postdominators(graph: Graph) -> dict[int, int | None]:
    """Immediate post-dominators over the normal CFG (Cooper–Harvey–Kennedy, reversed).

    Every node that has no normal successor (``exit``, ``raise_exit`` and nodes that only
    raise) is joined to a virtual exit. Nodes that cannot reach it (infinite loops) have no
    post-dominator and are returned with ``None``.
    """
    succ = [[t for t, _, _ in edges] for edges in graph.successors(normal_only=True)]
    pred: list[list[int]] = [[] for _ in range(graph.count)]
    for source, targets in enumerate(succ):
        for target in targets:
            pred[target].append(source)
    sinks = [n for n in range(graph.count) if not succ[n]]
    # Reverse-graph DFS from the virtual exit: order nodes by reverse post-order.
    order: list[int] = []
    seen: set[int] = set()
    for sink in sinks:
        if sink in seen:
            continue
        stack: list[tuple[int, int]] = [(sink, 0)]
        seen.add(sink)
        while stack:
            current, index = stack[-1]
            if index < len(pred[current]):
                stack[-1] = (current, index + 1)
                nxt = pred[current][index]
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, 0))
            else:
                stack.pop()
                order.append(current)
    rpo = list(reversed(order))
    position = {node: i for i, node in enumerate(rpo)}
    ipdom: dict[int, int] = {}

    def pos(node: int) -> int:
        return -1 if node == VIRTUAL_EXIT else position[node]

    def intersect(a: int, b: int) -> int:
        while a != b:
            while pos(a) > pos(b):
                a = ipdom[a]
            while pos(b) > pos(a):
                b = ipdom[b]
        return a

    for sink in sinks:
        ipdom[sink] = VIRTUAL_EXIT
    changed = True
    while changed:
        changed = False
        for node in rpo:
            if node in sinks:
                continue
            candidates = [s for s in succ[node] if s in ipdom]
            if not candidates:
                continue
            new = candidates[0]
            for other in candidates[1:]:
                new = intersect(other, new)
            if ipdom.get(node) != new:
                ipdom[node] = new
                changed = True
    result: dict[int, int | None] = {}
    for node in range(graph.count):
        value = ipdom.get(node, VIRTUAL_EXIT)
        result[node] = None if value == VIRTUAL_EXIT else value
    return result


@dataclass(frozen=True, slots=True)
class Dependence:
    branch: int
    kind: str
    label: str | None


def control_dependence(graph: Graph) -> dict[int, tuple[Dependence, ...]]:
    """Ferrante–Ottenstein–Warren: node ``n`` depends on branch edge ``a -> b`` when ``n``
    post-dominates ``b`` but does not strictly post-dominate ``a``. Normal CFG only."""
    ipdom = postdominators(graph)
    depends: dict[int, list[Dependence]] = {n: [] for n in range(graph.count)}
    for source, edges in enumerate(graph.successors(normal_only=True)):
        if len(edges) < 2:
            continue
        stop = ipdom.get(source)
        for target, kind, label in edges:
            runner: int | None = target
            visited: set[int] = set()
            while runner is not None and runner != stop and runner not in visited:
                visited.add(runner)
                depends[runner].append(Dependence(source, kind, label))
                runner = ipdom.get(runner)
    return {
        node: tuple(sorted(set(values), key=lambda d: (d.branch, d.kind, d.label or "")))
        for node, values in depends.items()
    }


GUARD_TERMS = 16


@dataclass(frozen=True, slots=True)
class Guards:
    """A node's guards as a disjunction of conjunctions of branch outcomes.

    ``terms`` is empty for an unguarded node. ``approximate`` is set when the disjunction
    exceeded ``GUARD_TERMS`` (``GUARD_SET_APPROXIMATE``) or a dependence cycle (loops) was cut.
    """

    terms: tuple[tuple[Dependence, ...], ...]
    approximate: bool = False


def guards(
    graph: Graph,
    node: int,
    dependence: dict[int, tuple[Dependence, ...]] | None = None,
    limit: int = GUARD_TERMS,
) -> Guards:
    """Transitive control-dependence chains from the scope entry to ``node``.

    Each direct dependence contributes its branch outcome plus the guards of the branch node;
    distinct direct dependences (a merge) are alternatives. Loop-carried dependences (a loop
    header depending on its own outcome through the back edge) are inherent to loops and are
    not repeated; only an overflowing disjunction is flagged approximate.
    """
    cd = dependence if dependence is not None else control_dependence(graph)
    approximate = False

    def walk(current: int, active: frozenset[int]) -> list[tuple[Dependence, ...]]:
        nonlocal approximate
        direct = cd.get(current, ())
        if not direct:
            return [()]
        terms: list[tuple[Dependence, ...]] = []
        for dependence_ in direct:
            if dependence_.branch in active:
                continue
            for prefix in walk(dependence_.branch, active | {dependence_.branch}):
                terms.append((*prefix, dependence_))
                if len(terms) > limit:
                    approximate = True
                    return terms[:limit]
        return terms or [()]

    terms = walk(node, frozenset({node}))
    unique = tuple(dict.fromkeys(t for t in terms if t))
    return Guards(unique, approximate)


def exceptional_sources(graph: Graph, node: int) -> tuple[int, ...]:
    """Nodes whose exceptional edges can reach ``node`` (``reached_if_raises``)."""
    pred: list[list[tuple[int, str]]] = [[] for _ in range(graph.count)]
    for source, target, kind, _ in graph.edges:
        pred[target].append((source, kind))
    found: set[int] = set()
    seen = {node}
    stack = [node]
    while stack:
        current = stack.pop()
        for source, kind in pred[current]:
            if kind in EXCEPTIONAL:
                found.add(source)
            if source not in seen:
                seen.add(source)
                stack.append(source)
    return tuple(sorted(found))


def summary_flags(graph: Graph) -> dict[str, bool]:
    reach = graph.reachable(ENTRY, normal_only=True)
    return {"normal_exit_reachable": EXIT in reach}


def node_ids(cfg: dict[str, Any], kind: str) -> list[int]:
    return [n["id"] for n in cfg["nodes"] if n["kind"] == kind]


def lines(cfg: dict[str, Any], ids: Iterable[int]) -> list[int | None]:
    nodes = cfg["nodes"]
    return [nodes[i].get("span", [None])[0] for i in ids]
