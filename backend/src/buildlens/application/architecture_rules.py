"""Stage 14 Milestone C: owner-declared rules and their conformance outcomes (C-4, F9).

Three axes stay apart: a ``RuleSet`` is declared intent (no evidence tier); a
``ConformanceResult`` is the evaluation of one intent version against one verified
architecture run; its witnesses are graph edges that keep their own evidence tiers. Results
are computed per request (keyed by the run's graph digest and the rule-set digest), never
cached and never enforced: there is no CI integration, no blocking and no inferred rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from buildlens.application.architecture import Assertion, LoadedGraph
from buildlens.application.architecture_queries import ArchitectureQueries
from buildlens.domain.architecture import (
    MODULE_EDGE_KINDS,
    ArchitectureEdge,
    ArchitectureRun,
)
from buildlens.domain.architecture_rules import (
    MAX_WITNESSES,
    ConformanceOutcome,
    Rule,
    RuleSet,
    glob_expression,
    validate_rules,
)
from buildlens.domain.source import digest

TIER_ORDER = ("unknown", "inferred", "derived", "observed")


class RulesNotFoundError(LookupError):
    pass


class ArchitectureRuleStore(Protocol):
    def add(self, project_id: UUID, rules: tuple[Rule, ...]) -> RuleSet: ...
    def get(self, project_id: UUID, version: int | None) -> RuleSet | None: ...
    def versions(self, project_id: UUID) -> list[RuleSet]: ...


@dataclass(frozen=True, slots=True)
class ConformanceResult:
    rule: Rule
    outcome: ConformanceOutcome
    witnesses: tuple[ArchitectureEdge, ...]
    witness_total: int
    witness_min_tier: str | None
    classes_evaluated: tuple[str, ...]
    coverage_qualifier: tuple[str, ...]
    matched: dict[str, int]


@dataclass(frozen=True, slots=True)
class RuleResults:
    run: ArchitectureRun
    freshness_reasons: tuple[str, ...]
    rule_set: RuleSet
    results: tuple[ConformanceResult, ...]
    result_key: str


class ArchitectureRulesService:
    def __init__(self, store: ArchitectureRuleStore, queries: ArchitectureQueries) -> None:
        self._store = store
        self._queries = queries

    def declare(self, project_id: UUID, payload: Any) -> RuleSet:
        return self._store.add(project_id, validate_rules(payload))

    def rule_set(self, project_id: UUID, version: int | None) -> RuleSet:
        value = self._store.get(project_id, version)
        if value is None:
            raise RulesNotFoundError("No architecture rule set has been declared.")
        return value

    def versions(self, project_id: UUID) -> list[RuleSet]:
        return self._store.versions(project_id)

    def evaluate(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        version: int | None,
    ) -> RuleResults:
        rule_set = self.rule_set(project_id, version)
        run, reasons, loaded, deadline = self._queries._open(project_id, snapshot_id, run_id)
        results = [_evaluate(loaded, rule) for rule in rule_set.rules]
        self._queries._verify(
            run,
            loaded,
            Assertion(
                frozenset(e.source_key for r in results for e in r.witnesses)
                | frozenset(e.target_key or "" for r in results for e in r.witnesses) - {""},
                frozenset(e.key for r in results for e in r.witnesses),
            ),
            deadline,
        )
        return RuleResults(
            run,
            reasons,
            rule_set,
            tuple(results),
            digest({"graph": run.graph_digest, "rules": rule_set.digest}),
        )


def _match(loaded: LoadedGraph, selectors: tuple[str, ...]) -> tuple[set[str], dict[str, int]]:
    modules = {key: node for key, node in loaded.nodes.items() if node.kind == "module"}
    matched: set[str] = set()
    counts: dict[str, int] = {}
    for selector in selectors:
        if selector.startswith("module:"):
            found = {selector} & set(modules)
        elif selector.startswith(("container:", "workspace:")):
            found, stack = set(), [selector]
            while stack:
                current = stack.pop()
                if current in modules:
                    found.add(current)
                stack.extend(loaded.children.get(current, []))
        else:
            expression = glob_expression(selector)
            found = {key for key, node in modules.items() if expression.match(node.locator or "")}
        counts[selector] = len(found)
        matched |= found
    return matched, counts


def _gaps(loaded: LoadedGraph, sources: set[str], classes: frozenset[str]) -> list[str]:
    gaps: set[str] = set()
    for key in sources:
        node = loaded.nodes[key]
        if node.coverage_state not in {"analysed", "not_code"}:
            gaps.add(f"FILE_{node.coverage_state.upper()}")
        if node.attributes.get("literals") in {"not_extracted", "failed", "not_current"}:
            gaps.add("LITERALS_NOT_EXTRACTED")
    for edge in loaded.edges.values():
        if edge.kind not in MODULE_EDGE_KINDS or edge.source_key not in sources:
            continue
        if edge.tier == "unknown" and edge.import_class in classes:
            gaps.add("UNRESOLVED_IMPORTS")
        if (
            edge.import_class == "presumed_unconditional"
            and "unconditional" in classes
            and "presumed_unconditional" not in classes
        ):
            gaps.add("GUARD_CONTEXT_UNKNOWN")
    for item in loaded.stored.diagnostics:
        if item.code == "DYNAMIC_IMPORT_OPAQUE" and set(item.subjects) & sources:
            gaps.add("OPAQUE_DYNAMIC_IMPORT")
    return sorted(gaps)


def _result(
    rule: Rule,
    violations: list[ArchitectureEdge],
    loaded: LoadedGraph,
    sources: set[str],
    matched: dict[str, int],
) -> ConformanceResult:
    classes = frozenset(rule.classes)
    violations.sort(key=lambda edge: edge.key)
    if violations:
        return ConformanceResult(
            rule,
            "not_satisfied",
            tuple(violations[:MAX_WITNESSES]),
            len(violations),
            min((edge.tier for edge in violations), key=TIER_ORDER.index),
            rule.classes,
            (),
            matched,
        )
    gaps = _gaps(loaded, sources, classes)
    return ConformanceResult(
        rule,
        "not_determinable" if gaps else "satisfied_in_observed_graph",
        (),
        0,
        None,
        rule.classes,
        tuple(gaps),
        matched,
    )


def _evaluate(loaded: LoadedGraph, rule: Rule) -> ConformanceResult:
    classes = frozenset(rule.classes)
    edges = [
        edge
        for edge in loaded.edges.values()
        if edge.kind in MODULE_EDGE_KINDS
        and edge.import_class in classes
        and edge.target_key is not None
        and edge.target_key.startswith("module:")
    ]
    if rule.kind == "forbid":
        sources, source_counts = _match(loaded, rule.sources)
        targets, target_counts = _match(loaded, rule.targets)
        matched = {**{f"from:{k}": v for k, v in source_counts.items()},
                   **{f"to:{k}": v for k, v in target_counts.items()}}  # fmt: skip
        if not sources or not targets:
            return ConformanceResult(
                rule, "selector_matches_nothing", (), 0, None, rule.classes, (), matched
            )
        violations = [e for e in edges if e.source_key in sources and e.target_key in targets]
        return _result(rule, violations, loaded, sources, matched)
    layers: list[set[str]] = []
    matched = {}
    for index, selectors in enumerate(rule.layers):
        found, counts = _match(loaded, selectors)
        matched |= {f"layer{index}:{key}": value for key, value in counts.items()}
        layers.append(found)
    if any(not layer for layer in layers):
        return ConformanceResult(
            rule, "selector_matches_nothing", (), 0, None, rule.classes, (), matched
        )
    position: dict[str, int] = {}
    for index, layer in enumerate(layers):
        for module in layer:
            if module in position:
                return ConformanceResult(
                    rule, "not_determinable", (), 0, None, rule.classes,
                    ("SELECTOR_OVERLAP",), matched,
                )  # fmt: skip
            position[module] = index
    violations = [
        edge
        for edge in edges
        if edge.source_key in position
        and edge.target_key in position
        and position[edge.target_key] < position[edge.source_key]
    ]
    return _result(rule, violations, loaded, set(position), matched)
