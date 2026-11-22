"""Finite, provider-independent impact propagation and comparison contracts."""

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Literal

CONTRACT = "impact.v5"
MAX_ORIGINS = 32
MAX_ARTIFACTS = 128
MAX_FRAMES = 4
MAX_STATES = 2_000
MAX_RELATIONSHIPS = 6_000
MAX_VERIFIED_ROWS = 8_000
MAX_REPORT_BYTES = 1_048_576
MAX_TERMS = 16
MAX_ATOMS = 16
MAX_WITNESSES = 3
Condition = frozenset[str]
Side = Literal["old", "new"]


@dataclass(frozen=True, order=True, slots=True)
class CellKey:
    origin: str
    side: Side
    subject: str
    mechanism: str
    property: str
    context: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AbstractValue:
    """Exact disjunction ordered by inclusion. None represents widened/top."""

    terms: frozenset[Condition] | None = frozenset()
    gaps: frozenset[str] = frozenset()

    def join(self, other: "AbstractValue") -> "AbstractValue":
        gaps = self.gaps | other.gaps
        if self.terms is None or other.terms is None:
            return AbstractValue(None, gaps)
        terms = self.terms | other.terms
        if len(terms) > MAX_TERMS:
            return AbstractValue(None, gaps | {"CONDITION_ALTERNATIVES_LIMIT"})
        return AbstractValue(terms, gaps)


@dataclass(frozen=True, slots=True)
class Supports:
    """Display certificates. They are intentionally absent from AbstractValue."""

    records: tuple[tuple[Condition, tuple[str, ...]], ...] = ()
    refuted: bool = False

    def record(self, term: Condition, path: tuple[str, ...]) -> "Supports":
        if any(known == term for known, _ in self.records):
            return self
        return replace(self, records=(*self.records, (term, path))[:MAX_WITNESSES])


@dataclass(frozen=True, slots=True)
class Transfer:
    id: str
    target: str
    mechanism: str
    conditions: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    push: str | None = None
    pop: str | None = None


@dataclass(frozen=True, slots=True)
class Frontier:
    transfers: tuple[Transfer, ...] = ()
    gaps: tuple[str, ...] = ()


@dataclass(slots=True)
class Walk:
    cells: dict[CellKey, AbstractValue] = field(default_factory=dict)
    supporting: dict[CellKey, Supports] = field(default_factory=dict)
    frontier: set[CellKey] = field(default_factory=set)
    gaps: set[str] = field(default_factory=set)
    expanded: int = 0


def refuted(term: Condition) -> bool:
    # Only the producer of an atom can certify opposite outcomes (signed atoms).
    return any(atom.startswith("!") and atom[1:] in term for atom in term)


def walk(
    roots: Iterable[CellKey],
    read: Callable[[CellKey], Frontier],
    expired: Callable[[], bool],
    *,
    max_states: int = MAX_STATES,
    max_relationships: int = MAX_RELATIONSHIPS,
) -> Walk:
    result = Walk()
    queue: deque[CellKey] = deque()
    queued: set[CellKey] = set()
    # Witness representatives for every exact analytical term, independent of display retention.
    paths: dict[CellKey, dict[Condition, tuple[str, ...]]] = {}

    def add(key: CellKey, value: AbstractValue, proofs: dict[Condition, tuple[str, ...]]) -> None:
        old = result.cells.get(key, AbstractValue())
        if key not in result.cells and len(result.cells) >= max_states:
            result.frontier.add(key)
            result.gaps.add("STATE_LIMIT")
            return
        new = old.join(value)
        certificate = result.supporting.get(key, Supports())
        for term, path in proofs.items():
            certificate = certificate.record(term, path)
        result.supporting[key] = certificate
        if new == old:
            return
        result.cells[key] = new
        if new.terms is None:
            paths.pop(key, None)
        else:
            known = paths.setdefault(key, {})
            for term, path in proofs.items():
                known.setdefault(term, path)
        if key not in queued:
            queue.append(key)
            queued.add(key)

    for root in sorted(set(roots)):
        add(root, AbstractValue(frozenset({frozenset()})), {frozenset(): ()})

    while queue:
        if expired():
            result.gaps.add("DEADLINE")
            result.frontier.update(queue)
            break
        key = queue.popleft()
        queued.remove(key)
        current = result.cells[key]
        page = read(key)
        result.gaps.update((*current.gaps, *page.gaps))
        for step in page.transfers:
            if result.expanded >= max_relationships or expired():
                result.gaps.add("DEADLINE" if expired() else "RELATIONSHIP_LIMIT")
                result.frontier.update((key, *queue))
                return result
            result.expanded += 1
            context = key.context
            if step.pop:
                if not context or context[-1] != step.pop:
                    continue
                context = context[:-1]
            target = replace(key, subject=step.target, mechanism=step.mechanism, context=context)
            if step.push:
                if len(context) >= MAX_FRAMES:
                    result.frontier.add(target)
                    result.gaps.add("CONTEXT_LIMIT")
                    continue
                target = replace(target, context=(*context, step.push))
            gaps = current.gaps | frozenset((*page.gaps, *step.gaps))
            if current.terms is None:
                add(target, AbstractValue(None, gaps), {})
                continue
            contributions: dict[Condition, tuple[str, ...]] = {}
            widened = False
            for term in sorted(current.terms, key=lambda item: tuple(sorted(item))):
                combined = term | frozenset(step.conditions)
                if refuted(combined):
                    certificate = result.supporting.get(target, Supports())
                    result.supporting[target] = replace(certificate, refuted=True)
                    continue
                if len(combined) > MAX_ATOMS:
                    widened = True
                    gaps |= {"CONDITION_ATOMS_LIMIT"}
                    continue
                contributions.setdefault(combined, (*paths[key][term], step.id))
            if contributions or widened:
                terms = None if widened else frozenset(contributions)
                add(target, AbstractValue(terms, gaps), contributions)
    return result


@dataclass(frozen=True, slots=True)
class Threshold:
    operator: str
    limit: int

    def accepts(self, value: int) -> bool:
        match self.operator:
            case "<":
                return value < self.limit
            case "<=":
                return value <= self.limit
            case ">":
                return value > self.limit
            case ">=":
                return value >= self.limit
            case "==":
                return value == self.limit
            case "!=":
                return value != self.limit
            case _:
                raise ValueError("Unsupported integer comparison")


def integer_difference(
    before: Threshold, after: Threshold, domain: tuple[int | None, int | None] = (None, None)
) -> list[dict[str, object]]:
    """Compare predicates on a caller-proven common exact integer domain."""
    lower, upper = domain
    points = sorted({before.limit, before.limit + 1, after.limit, after.limit + 1})
    regions: list[dict[str, object]] = []
    for start, end in zip([None, *points], [*(point - 1 for point in points), None], strict=True):
        lo = (
            max(start, lower)
            if start is not None and lower is not None
            else (start if start is not None else lower)
        )
        hi = (
            min(end, upper)
            if end is not None and upper is not None
            else (end if end is not None else upper)
        )
        if lo is not None and hi is not None and lo > hi:
            continue
        sample = lo if lo is not None else hi if hi is not None else 0
        old, new = before.accepts(sample), after.accepts(sample)
        if old == new:
            continue
        if (
            regions
            and lo is not None
            and regions[-1]["max"] == lo - 1
            and regions[-1]["new"] == new
        ):
            regions[-1]["max"] = hi
        else:
            regions.append({"min": lo, "max": hi, "sample": sample, "old": old, "new": new})
    return regions
