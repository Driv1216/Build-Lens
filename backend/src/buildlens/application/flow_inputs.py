"""Stage 15 Milestone B: flow model inputs (persisted rows only) and the element emitter.

Keys are portable: they are built from locators, scope keys and ordinals, never database ids,
so two databases holding the same bytes produce the same keys. Evidence items cite the rows
(with ids, for re-verification) by their row digests.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from buildlens.domain import flow_cfg as C
from buildlens.domain.flow import (
    TIERS,
    FlowDiagnostic,
    FlowEdge,
    FlowEvidenceItem,
    FlowNode,
    Tier,
)
from buildlens.domain.flow_evidence import (
    FlowCallsite,
    FlowEvidenceRun,
    FlowJsxBinding,
    FlowScope,
    FrameworkFact,
    ModuleFact,
    callsite_digest,
    jsx_digest,
)
from buildlens.domain.source import digest

# ----------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class ScopeInput:
    artifact_analysis_id: UUID
    locator: str
    language: str
    scope: FlowScope

    @property
    def key(self) -> str:
        return scope_key(self.locator, self.scope.key)


@dataclass(frozen=True, slots=True)
class ImportInput:
    artifact_analysis_id: UUID
    locator: str
    owner_symbol_id: UUID | None
    form: str
    from_module: str | None
    name: str
    alias: str | None
    level: int
    statement_ordinal: int
    item_ordinal: int

    @property
    def row_digest(self) -> str:
        return str(
            digest(
                [self.locator, self.form, self.from_module, self.name, self.alias, self.level]
                + [self.statement_ordinal, self.item_ordinal]
            )
        )

    @property
    def local_name(self) -> str:
        if self.alias:
            return self.alias
        if self.form == "import":  # Python ``import a.b`` binds ``a``
            return self.name.split(".")[0]
        return self.name


@dataclass(frozen=True, slots=True)
class ExportInput:
    artifact_analysis_id: UUID
    locator: str
    kind: str
    name: str | None
    local_name: str | None
    source_module: str | None
    ordinal: int

    @property
    def row_digest(self) -> str:
        return str(
            digest(
                [
                    self.locator,
                    self.kind,
                    self.name,
                    self.local_name,
                    self.source_module,
                    self.ordinal,
                ]
            )
        )


@dataclass(frozen=True, slots=True)
class SymbolInput:
    id: UUID
    artifact_analysis_id: UUID
    locator: str
    kind: str
    name: str
    qualified_name: str
    stable_identity_id: UUID | None
    parent_id: UUID | None
    exported: str | None


@dataclass(frozen=True, slots=True)
class Stage14Element:
    """An architecture node or edge, cited by its stored element digest."""

    table: str  # architecture_nodes | architecture_edges
    key: str
    kind: str
    element_digest: str
    source_key: str | None = None
    target_key: str | None = None
    tier: str | None = None
    conditions: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    catalog_entity_key: str | None = None
    locator: str | None = None

    @property
    def portable_digest(self) -> str:
        return str(
            digest(
                [
                    self.table,
                    self.key,
                    self.kind,
                    self.source_key,
                    self.target_key,
                    self.tier,
                    list(self.conditions),
                    self.attributes,
                    self.locator,
                ]
            )
        )


@dataclass(frozen=True, slots=True)
class LiteralInput:
    artifact_analysis_id: UUID
    locator: str
    ordinal: int
    kind: str
    span: tuple[int | None, int | None, int | None, int | None]


@dataclass(frozen=True, slots=True)
class FlowInput:
    evidence_run: FlowEvidenceRun
    catalog_run_id: UUID
    catalog_digest: str
    architecture_run_id: UUID
    architecture_graph_digest: str
    locators: frozenset[str]
    coverage: dict[str, str]
    scopes: tuple[ScopeInput, ...]
    imports: tuple[ImportInput, ...]
    exports: tuple[ExportInput, ...]
    symbols: tuple[SymbolInput, ...]
    literals: tuple[LiteralInput, ...]
    stage14_nodes: tuple[Stage14Element, ...]
    stage14_edges: tuple[Stage14Element, ...]
    framework_facts: tuple[FrameworkFact, ...]
    module_facts: tuple[ModuleFact, ...]


# ----------------------------------------------------------------------------- keys


def scope_key(locator: str, key: str) -> str:
    return f"scope:{locator}#{key}"


def call_key(locator: str, key: str, ordinal: int) -> str:
    return f"call:{locator}#{key}:{ordinal}"


def jsx_key(locator: str, key: str, ordinal: int) -> str:
    return f"jsx:{locator}#{key}:{ordinal}"


# ----------------------------------------------------------------------------- emitter


class Emitter:
    """Collects nodes, edges, evidence and diagnostics; merges evidence for repeated nodes."""

    def __init__(self, evidence_run_id: UUID, architecture_run_id: UUID) -> None:
        self.evidence_run_id = evidence_run_id
        self.architecture_run_id = architecture_run_id
        self.nodes: dict[str, FlowNode] = {}
        self.edges: dict[str, FlowEdge] = {}
        self.evidence: dict[str, FlowEvidenceItem] = {}
        self.diagnostics: list[FlowDiagnostic] = []

    # -- evidence --------------------------------------------------------------------------
    def item(
        self, kind: str, refs: Iterable[dict[str, Any]], derived: dict[str, Any] | None = None
    ) -> str:
        value = FlowEvidenceItem(kind, tuple(refs), derived or {})
        self.evidence.setdefault(value.digest, value)
        return value.digest

    def scope_ev(self, sc: ScopeInput) -> str:
        return self.item(
            "flow_scope",
            [
                {
                    "table": "flow_scopes",
                    "ids": {
                        "run": str(self.evidence_run_id),
                        "artifact_analysis": str(sc.artifact_analysis_id),
                        "scope": sc.scope.key,
                    },
                    "row_digest": sc.scope.digest,
                    "locator": sc.locator,
                }
            ],
        )

    def callsite_ev(self, sc: ScopeInput, callsite: FlowCallsite) -> str:
        return self.item(
            "flow_callsite",
            [
                {
                    "table": "flow_callsites",
                    "ids": {
                        "run": str(self.evidence_run_id),
                        "artifact_analysis": str(sc.artifact_analysis_id),
                        "scope": sc.scope.key,
                        "ordinal": callsite.ordinal,
                    },
                    "row_digest": callsite_digest(callsite),
                    "locator": sc.locator,
                }
            ],
        )

    def jsx_ev(self, sc: ScopeInput, binding: FlowJsxBinding) -> str:
        return self.item(
            "flow_jsx",
            [
                {
                    "table": "flow_jsx_bindings",
                    "ids": {
                        "run": str(self.evidence_run_id),
                        "artifact_analysis": str(sc.artifact_analysis_id),
                        "scope": sc.scope.key,
                        "ordinal": binding.ordinal,
                    },
                    "row_digest": jsx_digest(binding),
                    "locator": sc.locator,
                }
            ],
        )

    def stage14_ev(self, element: Stage14Element) -> str:
        return self.item(
            "architecture_element",
            [
                {
                    "table": element.table,
                    "ids": {"run": str(self.architecture_run_id), "key": element.key},
                    "row_digest": element.element_digest,
                    "locator": element.locator,
                    # Stage 14 element digests cover catalog identity ids; the portable
                    # projection uses the element's id-free semantic fields instead.
                    "portable": element.portable_digest,
                }
            ],
        )

    def import_ev(self, row: ImportInput) -> str:
        return self.item(
            "structural_import",
            [
                {
                    "table": "import_references",
                    "ids": {
                        "artifact_analysis": str(row.artifact_analysis_id),
                        "statement": row.statement_ordinal,
                        "item": row.item_ordinal,
                    },
                    "row_digest": row.row_digest,
                    "locator": row.locator,
                }
            ],
        )

    def fact_ev(self, fact: FrameworkFact | ModuleFact, kind: str) -> str:
        return self.item(
            kind,
            [
                {
                    "table": "flow_framework_facts"
                    if kind == "framework_fact"
                    else "flow_module_facts",
                    "ids": {"run": str(self.evidence_run_id), "locator": fact.locator},
                    "row_digest": digest(_fact_fields(fact)),
                    "locator": fact.locator,
                }
            ],
        )

    def derivation(self, rule: str, **derived: Any) -> str:
        return self.item("derivation", [], {"rule": rule, **derived})

    # -- elements --------------------------------------------------------------------------
    def node(
        self,
        key: str,
        kind: str,
        label: str,
        locator: str | None,
        attributes: dict[str, Any] | None = None,
        evidence: Iterable[str] = (),
    ) -> str:
        existing = self.nodes.get(key)
        merged = tuple(sorted(set(evidence) | set(existing.evidence if existing else ())))
        if existing is not None:
            self.nodes[key] = FlowNode(
                key, existing.kind, existing.label, existing.locator, existing.attributes, merged
            )
        else:
            self.nodes[key] = FlowNode(key, kind, label, locator, attributes or {}, merged)
        return key

    def edge(
        self,
        relation: str,
        source: str,
        target: str | None,
        tier: str,
        rule: str,
        conditions: Iterable[str] = (),
        attributes: dict[str, Any] | None = None,
        evidence: Iterable[str] = (),
        discriminator: str = "",
    ) -> str:
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}")
        key = f"{relation}|{source}|{target or '-'}" + (
            f"|{discriminator}" if discriminator else ""
        )
        existing = self.edges.get(key)
        evidence_set = set(evidence) | set(existing.evidence if existing else ())
        conditions_set = set(conditions) | set(existing.conditions if existing else ())
        self.edges[key] = FlowEdge(
            key,
            relation,
            source,
            target,
            cast(Tier, tier) if existing is None else existing.tier,
            rule if existing is None else existing.rule,
            tuple(sorted(conditions_set)),
            (attributes or {}) if existing is None else existing.attributes,
            tuple(sorted(evidence_set)),
        )
        return key

    def diagnose(self, code: str, subject: str | None, **attributes: Any) -> None:
        self.diagnostics.append(FlowDiagnostic(code, subject, attributes))


def _fact_fields(fact: FrameworkFact | ModuleFact) -> dict[str, Any]:
    if isinstance(fact, ModuleFact):
        return {
            "kind": fact.kind,
            "locator": fact.locator,
            "fingerprint": fact.input_fingerprint,
            "facts": fact.facts,
        }
    return {
        "ecosystem": fact.ecosystem,
        "package": fact.package,
        "version": fact.version,
        "constraint": fact.constraint,
        "source_kind": fact.source_kind,
        "locator": fact.locator,
        "scope": fact.scope,
    }


def fact_row_digest(fact: FrameworkFact | ModuleFact) -> str:
    return str(digest(_fact_fields(fact)))


# ----------------------------------------------------------------------------- scope views


@dataclass(slots=True)
class ScopeView:
    """Per-scope derived data: the graph, reaching definitions and bindings by name."""

    input: ScopeInput
    graph: C.Graph
    bindings: list[dict[str, Any]]
    by_name: dict[str, list[dict[str, Any]]]
    reaching: dict[int, dict[str, frozenset[int]]]
    parameters: list[dict[str, Any]]

    @classmethod
    def of(cls, sc: ScopeInput) -> ScopeView:
        cfg = sc.scope.cfg
        bindings = list(sc.scope.summary.get("bindings") or [])
        by_name: dict[str, list[dict[str, Any]]] = {}
        for binding in bindings:
            by_name.setdefault(binding["name"], []).append(binding)
        graph = C.Graph.of(cfg) if cfg else C.Graph(0, ())
        reaching = (
            C.reaching_definitions(graph, [(b["node"], b["name"], b["ordinal"]) for b in bindings])
            if cfg
            else {}
        )
        return cls(
            sc, graph, bindings, by_name, reaching, list(sc.scope.summary.get("parameters") or [])
        )

    def reaching_at(self, name: str, node: int) -> list[dict[str, Any]]:
        ordinals = self.reaching.get(node, {}).get(name, frozenset())
        return [b for b in self.by_name.get(name, []) if b["ordinal"] in ordinals]
