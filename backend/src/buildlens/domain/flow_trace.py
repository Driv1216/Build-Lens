"""Stage 15 Milestone C: the ``FlowTrace`` contract and the sound predicate fragment.

A trace is a bounded, reproducible walk over a verified flow model run. Every step carries four
separate verdicts (plan §3.4):

- **L1 resolution:** the edge's tier, rule and conditions, verbatim.
- **L2 reachability:** whether the step's CFG node is statically reachable in its scope.
- **L3 path condition:** the control-dependence guards accumulated along the path.
- **L4 feasibility:** that condition decided in the sound fragment (``flow-feasibility.v1``):
  ``refuted_in_fragment``, ``consistent_in_fragment`` or ``undetermined``. Never "executes".

A structural path is one witness in an over-approximate model, not an execution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from buildlens.domain.source import digest

FLOW_TRACE_VERSION = "flow-trace.v1"
FEASIBILITY_VERSION = "flow-feasibility.v1"
ENTRY_POINTS_VERSION = "flow-entry-points.v1"

# Placeholder bounds (D7), recorded in every trace's query.
MAX_DEPTH = 24
MAX_HOPS = 2_000
TRACE_FANOUT = 8
DNF_TERMS = 16
DEADLINE_SECONDS = 5.0
VERIFICATION_BUDGET = 25_000

Direction = Literal["forward", "backward"]
Status = Literal[
    "complete", "truncated_depth", "truncated_limit", "deadline", "incomplete_evidence"
]
Feasibility = Literal["refuted_in_fragment", "consistent_in_fragment", "undetermined"]

WITNESS_NOTE = (
    "one shortest structural witness in the over-approximate model; other paths may exist"
)
ASSUMPTIONS_PY = ("ADMISSIBLE_LOCALS_ONLY", "ASSERTIONS_ENABLED_ASSUMED")
ASSUMPTIONS_JS = ("ADMISSIBLE_LOCALS_ONLY", "HOST_OBJECT_FALSY_EXCLUDED")


@dataclass(frozen=True, slots=True)
class TraceQuery:
    entry: str
    direction: Direction = "forward"
    max_depth: int = MAX_DEPTH
    max_hops: int = MAX_HOPS
    fanout: int = TRACE_FANOUT

    def normalised(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "direction": self.direction,
            "bounds": {
                "max_depth": self.max_depth,
                "max_hops": self.max_hops,
                "fanout": self.fanout,
                "dnf_terms": DNF_TERMS,
                "deadline_seconds": DEADLINE_SECONDS,
            },
            # Traces always keep refuted segments; hiding them is a presentation filter.
            "options": {},
        }


def flow_trace_contract() -> dict[str, Any]:
    return {
        "trace": FLOW_TRACE_VERSION,
        "feasibility": FEASIBILITY_VERSION,
        "entry_points": ENTRY_POINTS_VERSION,
        "bounds": {
            "max_depth": MAX_DEPTH,
            "max_hops": MAX_HOPS,
            "fanout": TRACE_FANOUT,
            "dnf_terms": DNF_TERMS,
            "deadline_seconds": DEADLINE_SECONDS,
            "verification_budget": VERIFICATION_BUDGET,
        },
    }


def trace_digest(graph_digest: str, query: dict[str, Any], result: dict[str, Any]) -> str:
    """INV-7: sha256 over the canonical {contract, graph digest, normalised query, result}."""
    return str(
        digest(
            {
                "contract": flow_trace_contract(),
                "graph_digest": graph_digest,
                "query": query,
                "result": result,
            }
        )
    )


# ----------------------------------------------------------------------------- feasibility


@dataclass(frozen=True, slots=True)
class Atom:
    """One guard outcome over a variable identity (scope, name, reaching definitions).

    ``op`` is a fragment operator, or ``opaque`` (a fresh unknown per evaluation: equal text
    never implies equal value). ``value`` is the polarity the path requires.
    """

    op: str
    var: tuple[str, str, tuple[int, ...]] | None
    value: bool
    lit: tuple[Any, ...] | None = None
    text: str | None = None  # typeof name
    reason: str | None = None  # why an atom is opaque
    # (scope, CFG branch node) when the branch is outside every loop: one activation of the
    # scope takes one outcome there, so opposite outcomes on one path contradict.
    branch: tuple[str, int] | None = None


@dataclass(slots=True)
class _Domain:
    truthy: set[bool] = field(default_factory=set)
    nullish: set[bool] = field(default_factory=set)
    equal: list[tuple[Any, ...]] = field(default_factory=list)
    unequal: list[tuple[Any, ...]] = field(default_factory=list)
    typeof: list[tuple[str, bool]] = field(default_factory=list)


def literal_key(lit: dict[str, Any] | None) -> tuple[Any, ...] | None:
    """A literal's identity: (type, value) for small values, (type, sha256, len) for text."""
    if not lit:
        return None
    t = lit.get("t")
    if t in {"none", "undefined"}:
        return (t,)
    if t in {"bool", "int"} and "v" in lit:
        return (t, lit["v"])
    if t in {"str", "bytes"} and "sha256" in lit:
        return (t, lit["sha256"], lit.get("len"))
    return None


def _number(t: Any) -> bool:
    return t in {"int", "float"}


def literals_equal(a: tuple[Any, ...], b: tuple[Any, ...]) -> bool | None:
    """True / False when decided; ``None`` when the comparison is not decided (cross-type
    numbers, ``bool`` versus ``int`` in Python)."""
    if a == b:
        return True
    if a[0] == b[0]:
        return False
    if {a[0], b[0]} <= {"bool", "int", "float"}:
        return None
    return False


def literal_truthiness(key: tuple[Any, ...]) -> bool | None:
    t = key[0]
    if t in {"none", "undefined"}:
        return False
    if t in {"bool", "int"}:
        return bool(key[1])
    if t in {"str", "bytes"}:
        return key[2] is not None and key[2] > 0 if len(key) > 2 else None
    return None


def decide(term: tuple[Atom, ...]) -> Feasibility:
    """Exact per-variable domain intersection over one conjunction.

    ``refuted_in_fragment`` only when admissible atoms alone contradict; ``consistent_in_fragment``
    only when every atom is in the fragment, every comparison was decided and none contradicts.
    """
    domains: dict[tuple[str, str, tuple[int, ...]], _Domain] = {}
    outcomes: dict[tuple[str, int], bool] = {}
    complete = True
    for atom in term:
        if atom.branch is not None:
            previous = outcomes.setdefault(atom.branch, atom.value)
            if previous != atom.value and atom.op in {"opaque", "const"}:
                return "refuted_in_fragment"
        if atom.op == "const":
            truth = literal_truthiness(atom.lit) if atom.lit else None
            if truth is None:
                complete = False
            elif truth != atom.value:
                return "refuted_in_fragment"
            continue
        if atom.op == "opaque" or atom.var is None:
            complete = False
            continue
        d = domains.setdefault(atom.var, _Domain())
        if atom.op == "truthy":
            d.truthy.add(atom.value)
        elif atom.op == "nullish":
            d.nullish.add(atom.value)
            if atom.value:
                d.truthy.add(False)
        elif atom.op == "eq" and atom.lit is not None:
            (d.equal if atom.value else d.unequal).append(atom.lit)
        elif atom.op == "typeof" and atom.text:
            d.typeof.append((atom.text, atom.value))
        else:
            complete = False
    for d in domains.values():
        if len(d.truthy) > 1 or len(d.nullish) > 1:
            return "refuted_in_fragment"
        for index, a in enumerate(d.equal):
            for b in d.equal[index + 1 :]:
                same = literals_equal(a, b)
                if same is False:
                    return "refuted_in_fragment"
                if same is None:
                    complete = False
            for b in d.unequal:
                if literals_equal(a, b) is True:
                    return "refuted_in_fragment"
            truth = literal_truthiness(a)
            if truth is not None and d.truthy and truth not in d.truthy:
                return "refuted_in_fragment"
            if d.nullish:
                is_nullish = a[0] in {"none", "undefined"}
                if (True in d.nullish) != is_nullish:
                    return "refuted_in_fragment"
        positives = {name for name, value in d.typeof if value}
        negatives = {name for name, value in d.typeof if not value}
        if len(positives) > 1 or positives & negatives:
            return "refuted_in_fragment"
    return "consistent_in_fragment" if complete else "undetermined"


def decide_dnf(terms: list[tuple[Atom, ...]], approximate: bool) -> Feasibility:
    """A disjunction is refuted only when every term is; consistent when some term is."""
    if not terms:
        return "undetermined" if approximate else "consistent_in_fragment"
    verdicts = [decide(t) for t in terms]
    if all(v == "refuted_in_fragment" for v in verdicts) and not approximate:
        return "refuted_in_fragment"
    if any(v == "consistent_in_fragment" for v in verdicts):
        return "consistent_in_fragment"
    return "undetermined"


def atom_json(atom: Atom) -> dict[str, Any]:
    value: dict[str, Any] = {"op": atom.op, "value": atom.value}
    if atom.var is not None:
        value["var"] = {"scope": atom.var[0], "name": atom.var[1], "definitions": list(atom.var[2])}
    if atom.lit is not None:
        value["lit"] = list(atom.lit)
    if atom.text is not None:
        value["typeof"] = atom.text
    if atom.reason is not None:
        value["reason"] = atom.reason
    if atom.branch is not None:
        value["branch"] = {"scope": atom.branch[0], "cfg_node": atom.branch[1]}
    return value


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
