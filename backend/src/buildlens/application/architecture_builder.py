"""Stage 14 architecture builder: a pure function of persisted rows (no source reads).

Module edges are a *projection* of the Stage 13 catalog's ``imports``/``exports`` relations.
Each import statement is classified (``import-classes.v2``) and re-keyed with the catalog's
own ``import-resolution.v1``; the resulting relation keys and counts must equal the catalog's
relations exactly, otherwise the build fails (``ARCHITECTURE_CATALOG_MISMATCH``) rather than
publishing a graph that disagrees with its cited evidence.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast
from uuid import UUID

from buildlens.application.architecture_api import (
    ApiModule,
    ApiProjection,
    Binding,
    EndpointInput,
    binding_kind,
    extraction_row_digest,
    literal_ref,
)
from buildlens.application.architecture_views import ManifestInput, TableInput, ViewsProjection
from buildlens.application.semantic_catalog import ImportResolver
from buildlens.domain.architecture import (
    AGGREGATION_VERSION,
    CLASS_SET_NAMES,
    CLASS_SETS,
    CONTAINMENT_VERSION,
    EVIDENCE_RENDERER,
    MAX_EDGES,
    MAX_NODES,
    MODULE_EDGE_KINDS,
    SCC_VERSION,
    STATEMENT_REFS_PER_EDGE,
    ArchitectureBuild,
    ArchitectureComponent,
    ArchitectureDiagnostic,
    ArchitectureEdge,
    ArchitectureEvidence,
    ArchitectureNode,
    ArchitectureOutcome,
    ClassSet,
    ComponentLevel,
    CoverageState,
    EvidenceLink,
    ImportClass,
    Row,
    component_digest,
    diagnostic_digest,
    edge_digest,
    evidence_digest,
    graph_digest_of,
    node_digest,
)
from buildlens.domain.architecture_graph import (
    Graph,
    GraphEdge,
    cyclic_components,
    shortest_cycle,
)
from buildlens.domain.extractors import (
    PYTHON_LITERALS,
    TS_LITERALS,
    extractor_contract_digest,
)
from buildlens.domain.semantic import SemanticEntity, SemanticRelation
from buildlens.domain.source import digest

CODE_LANGUAGES = frozenset({"Python", "TypeScript", "JavaScript"})
MANIFESTS = {"pyproject.toml": "python", "setup.py": "python", "setup.cfg": "python"}
MANIFESTS["package.json"] = "npm"
UNUSABLE_LIMITATIONS = frozenset({"STRUCTURE_FAILED", "EXTRACTOR_CONTRACT_CHANGED"})
DIAGNOSTIC_TARGET_SAMPLE = 20


class ArchitectureBuildError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# --------------------------------------------------------------------------- input model


@dataclass(frozen=True, slots=True)
class ModuleInput:
    """One analysed artifact of the catalog's structural run, with its catalog file entity."""

    locator: str
    language: str | None
    outcome: str
    entity_key: str
    limitations: tuple[str, ...]
    import_contract: str | None
    imports: tuple[Row, ...] = ()
    exports: tuple[Row, ...] = ()
    owner_kinds: dict[str, str] = field(default_factory=dict)
    # Stage 14 Milestone B literal pass (``extracted | partial | failed | not_current |
    # not_extracted | not_applicable``) and its current rows only.
    literal_state: str = "not_applicable"
    literal_extraction: Row | None = None
    literals: tuple[Row, ...] = ()
    top_level_names: frozenset[str] = frozenset()

    @property
    def literals_usable(self) -> bool:
        return self.literal_state in {"extracted", "partial"}

    @property
    def classification_known(self) -> bool:
        facts = (self.literal_extraction or {}).get("artifact_facts") or {}
        return self.literals_usable and bool(facts.get("classification_complete"))


@dataclass(frozen=True, slots=True)
class CatalogRelationInput:
    kind: str
    source_entity_key: str
    target_entity_key: str | None
    target_text: str | None
    resolution_status: str | None
    fact_digest: str
    count: int


@dataclass(frozen=True, slots=True)
class ArchitectureInput:
    inventory: tuple[Row, ...]
    modules: tuple[ModuleInput, ...]
    relations: tuple[CatalogRelationInput, ...]
    catalog_outcome: str
    artifact_identities: dict[str, str] = field(default_factory=dict)
    endpoints: tuple[EndpointInput, ...] = ()
    manifests: tuple[ManifestInput, ...] = ()
    tables: tuple[TableInput, ...] = ()


# --------------------------------------------------------------------------- row digests


def import_row_digest(row: Row, owner_kind: str | None) -> str:
    """Semantic fields of an ``import_references`` row, without database ids."""
    return digest(
        {
            "statement": row.get("statement_ordinal"),
            "item": row.get("item_ordinal"),
            "form": row.get("form"),
            "from_module": row.get("from_module"),
            "name": row.get("name"),
            "alias": row.get("alias"),
            "level": row.get("level"),
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
            "owner_kind": owner_kind,
        }
    )


def export_row_digest(row: Row) -> str:
    return digest(
        {
            "ordinal": row.get("ordinal"),
            "kind": row.get("kind"),
            "name": row.get("name"),
            "local_name": row.get("local_name"),
            "source_module": row.get("source_module"),
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
        }
    )


def artifact_row_digest(row: Row) -> str:
    return digest(
        {
            "locator": row.get("locator"),
            "language": row.get("language"),
            "content_digest": row.get("content_digest"),
        }
    )


# --------------------------------------------------------------------------- helpers


def module_key(locator: str) -> str:
    return f"module:{locator}"


def directory_key(path: str, workspaces: set[str]) -> str:
    if path in workspaces:
        return f"workspace:{path or '.'}"
    return f"container:{path}"


def external_package(specifier: str, python: bool) -> tuple[str, str]:
    """(ecosystem, package name) for an external import target."""
    if python:
        return "python", specifier.split(".")[0]
    if specifier.startswith("node:"):
        return "node", specifier
    parts = specifier.split("/")
    if specifier.startswith("@") and len(parts) > 1:
        return "npm", "/".join(parts[:2])
    return "npm", parts[0]


def alias_prefix(specifier: str) -> str:
    for prefix in ("@/", "~/"):
        if specifier.startswith(prefix):
            return prefix
    return "#" if specifier.startswith("#") else "/" if specifier.startswith("/") else "other"


def _is_python(language: str | None) -> bool:
    return language in {None, "Python"}


_CatalogKey = tuple[str, str, str | None, str | None]


@dataclass(slots=True)
class _Statement:
    """One import row (or re-export row) with its catalog key and import class."""

    kind: str
    source: ModuleInput
    row: Row
    table: str
    catalog_key: _CatalogKey
    status: str
    target_locator: str | None
    module_text: str
    import_class: str
    conditions: tuple[str, ...]
    attributes: Row
    support: tuple[Row, ...] = ()


@dataclass(slots=True)
class _EdgeDraft:
    kind: str
    source: str
    target: str | None
    text: str | None
    status: str
    import_class: str
    tier: str
    statements: list[_Statement] = field(default_factory=list)
    conditions: set[str] = field(default_factory=set)
    forms: set[str] = field(default_factory=set)
    module_texts: set[str] = field(default_factory=set)
    star: bool = False
    side_effect: bool = False
    catalog_facts: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------- shared derivations
# The builder and read-time verification recompute aggregates and components with these same
# functions, so a persisted derived record can be checked against its persisted inputs.


def parents_of(nodes: Iterable[ArchitectureNode]) -> dict[str, str | None]:
    return {node.key: node.parent_key for node in nodes}


def ancestors(key: str, parents: Mapping[str, str | None]) -> list[str]:
    chain = [key]
    while (parent := parents[chain[-1]]) is not None:
        chain.append(parent)
    return chain


# ``direction-diagnostics.v1``: inferred from path conventions only (never from content).
DIRECTION_CODES: dict[str, tuple[str, ...]] = {
    "PRODUCTION_IMPORTS_TEST": ("TEST_PATH_CONVENTION",),
    "RUNTIME_IMPORTS_MIGRATION": ("MIGRATION_PATH_CONVENTION",),
    "CROSS_WORKSPACE_RELATIVE_IMPORT": ("WORKSPACE_FROM_MANIFESTS",),
}
_TEST_SEGMENTS = {"tests", "test", "__tests__"}
_TEST_NAME = re.compile(r"^(test_.*\.py|.*_test\.py|conftest\.py|.*\.(test|spec)\.[cm]?[jt]sx?)$")


def is_test_path(locator: str) -> bool:
    parts = locator.split("/")
    return bool(_TEST_SEGMENTS & set(parts[:-1]) or _TEST_NAME.match(parts[-1]))


def is_migration_path(locator: str) -> bool:
    parts = locator.split("/")[:-1]
    return "migrations" in parts or "/".join(parts).endswith("alembic/versions")


def _workspace(key: str, nodes: Mapping[str, ArchitectureNode]) -> str | None:
    current: str | None = key
    while current is not None and not current.startswith("workspace:"):
        node = nodes.get(current)
        current = node.parent_key if node is not None else None
    return current


def direction_codes(edge: ArchitectureEdge, nodes: Mapping[str, ArchitectureNode]) -> list[str]:
    """Direction diagnostics of one resolved module edge (``direction-diagnostics.v1``)."""
    if (
        edge.kind not in MODULE_EDGE_KINDS
        or edge.target_key is None
        or not edge.target_key.startswith("module:")
        or edge.source_key == edge.target_key
    ):
        return []
    source = edge.source_key.removeprefix("module:")
    target = edge.target_key.removeprefix("module:")
    codes = []
    if is_test_path(target) and not is_test_path(source):
        codes.append("PRODUCTION_IMPORTS_TEST")
    if is_migration_path(target) and not (is_migration_path(source) or is_test_path(source)):
        codes.append("RUNTIME_IMPORTS_MIGRATION")
    relative = any(str(text).startswith(".") for text in edge.attributes.get("module_text", []))
    if (
        relative
        and not source.endswith(".py")
        and _workspace(edge.source_key, nodes) != _workspace(edge.target_key, nodes)
    ):
        codes.append("CROSS_WORKSPACE_RELATIVE_IMPORT")
    return codes


def _resolved_module_edges(edges: Iterable[ArchitectureEdge]) -> list[ArchitectureEdge]:
    return [
        edge
        for edge in edges
        if edge.kind in MODULE_EDGE_KINDS
        and edge.target_key is not None
        and edge.target_key.startswith("module:")
    ]


@dataclass(frozen=True, slots=True)
class AggregateShape:
    source: str
    target: str
    import_class: str
    scope: str
    keys: tuple[str, ...]
    count: int

    @property
    def key(self) -> str:
        return f"depends_on|{self.source}|{self.target}|{self.import_class}"

    def attributes(self) -> Row:
        return {
            "scope": self.scope,
            "constituents": digest(list(self.keys)),
            "constituent_edges": len(self.keys),
        }


def aggregate_shapes(
    edges: Iterable[ArchitectureEdge], parents: Mapping[str, str | None]
) -> list[AggregateShape]:
    """``depends_on`` between the children of the lowest common ancestor of each module edge."""
    groups: dict[tuple[str, str, str], list[ArchitectureEdge]] = defaultdict(list)
    scopes: dict[tuple[str, str, str], str] = {}
    for edge in _resolved_module_edges(edges):
        assert edge.target_key is not None
        if edge.source_key == edge.target_key:
            continue
        source_chain = ancestors(edge.source_key, parents)
        target_chain = ancestors(edge.target_key, parents)
        shared = set(target_chain)
        common = next(node for node in source_chain if node in shared)
        group = (
            source_chain[source_chain.index(common) - 1],
            target_chain[target_chain.index(common) - 1],
            str(edge.import_class),
        )
        groups[group].append(edge)
        scopes[group] = common
    return [
        AggregateShape(
            source,
            target,
            import_class,
            scopes[(source, target, import_class)],
            tuple(sorted(edge.key for edge in members)),
            sum(edge.count for edge in members),
        )
        for (source, target, import_class), members in sorted(groups.items())
    ]


@dataclass(frozen=True, slots=True)
class ComponentShape:
    level: str
    class_set: str
    scope: str | None
    members: tuple[str, ...]
    witness: tuple[str, ...]
    underlying: bool | None = None

    @property
    def key(self) -> str:
        return f"component:{self.level}:{digest([self.scope, list(self.members)])[:24]}"

    def attributes(self) -> Row:
        return {} if self.underlying is None else {"module_cycle_underlying": self.underlying}


def component_shapes(
    edges: Sequence[ArchitectureEdge], parents: Mapping[str, str | None]
) -> list[ComponentShape]:
    """SCCs of the module relation and of each scope's aggregate relation, per class set.

    A member set is reported once, at the smallest class set in which it is cyclic.
    """
    module_edges = [
        GraphEdge(edge.key, edge.source_key, str(edge.target_key), edge.import_class)
        for edge in _resolved_module_edges(edges)
    ]
    values: list[ComponentShape] = []
    seen: set[tuple[str, str | None, tuple[str, ...]]] = set()
    module_components: list[tuple[str, tuple[str, ...]]] = []
    for name in CLASS_SET_NAMES:
        graph = Graph(module_edges, CLASS_SETS[name])
        for members in cyclic_components(graph):
            if ("module", None, members) in seen:
                continue
            seen.add(("module", None, members))
            module_components.append((name, members))
            witness = shortest_cycle(graph, members[0], set(members)) or ()
            values.append(ComponentShape("module", name, None, members, witness))
    by_scope: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in edges:
        if edge.kind == "depends_on" and edge.target_key is not None:
            by_scope[str(edge.attributes["scope"])].append(
                GraphEdge(edge.key, edge.source_key, edge.target_key, edge.import_class)
            )
    for scope, scoped in sorted(by_scope.items()):
        for name in CLASS_SET_NAMES:
            graph = Graph(scoped, CLASS_SETS[name])
            for members in cyclic_components(graph):
                # Sibling modules already form module-level components.
                if ("container", scope, members) in seen or all(
                    member.startswith("module:") for member in members
                ):
                    continue
                seen.add(("container", scope, members))
                witness = shortest_cycle(graph, members[0], set(members)) or ()
                underlying = _underlying(scope, set(members), name, module_components, parents)
                values.append(
                    ComponentShape("container", name, scope, members, witness, underlying)
                )
    values.sort(key=lambda item: (item.level, item.scope or "", item.class_set, item.members))
    return values


def _underlying(
    scope: str,
    members: set[str],
    class_set: str,
    module_components: list[tuple[str, tuple[str, ...]]],
    parents: Mapping[str, str | None],
) -> bool:
    """Whether a module-level cycle (at this class set or smaller) spans two members."""
    limit = CLASS_SET_NAMES.index(class_set)
    for name, component in module_components:
        if CLASS_SET_NAMES.index(name) > limit:
            continue
        children = set()
        for module in component:
            chain = ancestors(module, parents)
            if scope in chain[1:]:
                children.add(chain[chain.index(scope) - 1])
        if len(children & members) >= 2:
            return True
    return False


# --------------------------------------------------------------------------- builder


class _Builder:
    def __init__(self, source: ArchitectureInput) -> None:
        self.source = source
        self.inventory = {
            str(row["locator"]): row for row in source.inventory if row.get("locator")
        }
        self.resolver = ImportResolver(self.inventory)
        self.modules = {item.locator: item for item in source.modules}
        self.evidence: dict[str, ArchitectureEvidence] = {}
        self.api: ApiProjection | None = None
        self.views: ViewsProjection | None = None
        self.relations: dict[tuple[str, str, str | None, str | None], CatalogRelationInput] = {}
        for relation in source.relations:
            key = (
                relation.kind,
                relation.source_entity_key,
                relation.target_entity_key,
                None if relation.target_entity_key else relation.target_text,
            )
            self.relations[key] = relation

    # ----------------------------------------------------------------- evidence

    def _evidence(self, kind: Any, refs: Sequence[Row], derived: Row, text: str) -> EvidenceLink:
        value = evidence_digest(EVIDENCE_RENDERER, text, refs, derived)
        if value not in self.evidence:
            self.evidence[value] = ArchitectureEvidence(
                value, kind, tuple(refs), derived, EVIDENCE_RENDERER, text
            )
        return EvidenceLink("architecture", value)

    def _derivation(self, derived: Row, text: str) -> EvidenceLink:
        return self._evidence("derivation", (), derived, text)

    # ----------------------------------------------------------------- statements

    def _classify(
        self, module: ModuleInput, rows: list[Row]
    ) -> tuple[str, tuple[str, ...], Row, tuple[Row, ...]]:
        """``import-classes.v2`` for one statement, refined by literal guard rows (C-2).

        Returns (class, conditions, attributes, supporting evidence refs). Guard context is
        trusted only when the artifact's current literal pass recorded every classification
        row; otherwise Python module-scope imports stay ``presumed_unconditional``.
        """
        first = rows[0]
        form = str(first["form"])
        owner = first.get("owner_symbol_id")
        owner_kind = module.owner_kinds.get(str(owner)) if owner else None
        known = module.classification_known
        line = (first.get("start_line"), first.get("end_line"))
        pass_ref = (
            (
                {
                    "table": "artifact_extractions",
                    "id": module.literal_extraction["id"],
                    "contract": module.literal_extraction.get("contract_digest"),
                    "locator": module.locator,
                    "row_digest": extraction_row_digest(module.literal_extraction),
                },
            )
            if known and module.literal_extraction
            else ()
        )
        if _is_python(module.language):
            if owner and owner_kind not in {None, "class"}:
                return "deferred", (), {}, ()
            if not known:
                return "presumed_unconditional", ("PYTHON_GUARD_CONTEXT_UNKNOWN",), {}, ()
            start = int(first.get("start_line") or 0)
            guard = next(
                (
                    row
                    for row in module.literals
                    if row["kind"] == "import_guard"
                    and int(row["start_line"]) <= start <= int(row["end_line"])
                ),
                None,
            )
            if guard is None:
                return "unconditional", (), {}, pass_ref
            ref = (literal_ref(module.locator, guard),)
            if guard["attributes"].get("guard") == "type_checking":
                return "type_level", ("PYTHON_TYPE_CHECKING_GUARD",), {}, ref
            return "conditional", ("PYTHON_IMPORT_GUARDED",), {}, ref
        if form == "dynamic_import":
            return "deferred", (), {}, ()
        if form == "require":
            if owner:
                return "deferred", (), {}, ()
            if not known:
                return "presumed_unconditional", ("REQUIRE_GUARD_CONTEXT_UNKNOWN",), {}, ()
            guard = next(
                (
                    row
                    for row in module.literals
                    if row["kind"] == "import_guard"
                    and (row["start_line"], row["end_line"]) == line
                ),
                None,
            )
            if guard is None:
                return "unconditional", (), {}, pass_ref
            return "conditional", ("REQUIRE_GUARDED",), {}, (literal_ref(module.locator, guard),)
        if any(str(row["name"]) == "" for row in rows):
            return "unconditional", (), {"side_effect": True}, ()
        if module.language == "JavaScript":
            return "unconditional", (), {}, ()
        if all(row["form"] == "es_import_type" for row in rows):
            if not known:
                # ``import type`` and ``import { type A }`` are indistinguishable without the
                # literal pass; the conservative class keeps such cycles visible.
                return (
                    "elision_dependent",
                    ("TS_ELISION_NOT_DECIDED", "TYPE_IMPORT_FORM_UNDISTINGUISHED"),
                    {},
                    (),
                )
            statement = next(
                (
                    row
                    for row in module.literals
                    if row["kind"] == "type_import" and (row["start_line"], row["end_line"]) == line
                ),
                None,
            )
            if statement is not None:
                return "type_level", (), {}, (literal_ref(module.locator, statement),)
            return "elision_dependent", ("TS_ELISION_NOT_DECIDED",), {}, pass_ref
        return "elision_dependent", ("TS_ELISION_NOT_DECIDED",), {}, ()

    def _target(self, resolved_status: str, locator: str | None) -> tuple[str | None, bool]:
        """Catalog target entity key for a resolved locator, and whether it is analysed."""
        if resolved_status != "resolved" or locator is None:
            return None, False
        module = self.modules.get(locator)
        return (module.entity_key, True) if module is not None else (None, False)

    def _statements(self, module: ModuleInput) -> list[_Statement]:
        if set(module.limitations) & UNUSABLE_LIMITATIONS or module.outcome == "failed":
            return []
        python = _is_python(module.language)
        values: list[_Statement] = []
        by_statement: dict[Any, list[Row]] = defaultdict(list)
        for row in module.imports:
            by_statement[row.get("statement_ordinal")].append(row)
        for _, rows in sorted(by_statement.items(), key=lambda item: int(item[0] or 0)):
            rows.sort(key=lambda row: int(row.get("item_ordinal") or 0))
            import_class, conditions, attributes, support = self._classify(module, rows)
            for row in rows:
                resolved = (
                    self.resolver.python(module.locator, row)
                    if python
                    else self.resolver.specifier(module.locator, row.get("from_module"))
                )
                text = row.get("from_module") if row["form"] != "import" else row["name"]
                if python and int(row.get("level") or 0):
                    text = "." * int(row["level"]) + str(text or "")
                target, analysed = self._target(resolved.status, resolved.locator)
                key: _CatalogKey
                if analysed:
                    key = ("imports", module.entity_key, target, None)
                elif resolved.status == "resolved" and resolved.locator in self.inventory:
                    key = ("imports", module.entity_key, None, resolved.locator)
                else:
                    key = ("imports", module.entity_key, None, str(text or row["name"]))
                star = python and row["form"] == "from" and row["name"] == "*"
                values.append(
                    _Statement(
                        "imports",
                        module,
                        row,
                        "import_references",
                        key,
                        resolved.status,
                        resolved.locator if resolved.status == "resolved" else None,
                        str(text or row["name"]),
                        import_class,
                        conditions,
                        {**attributes, "star": star},
                        support,
                    )
                )
        for row in module.exports:
            if row["kind"] not in {"reexport", "reexport_all"}:
                continue
            resolved = self.resolver.specifier(module.locator, row.get("source_module"))
            target, analysed = self._target(resolved.status, resolved.locator)
            key = cast(
                _CatalogKey,
                ("exports", module.entity_key, target, None)
                if analysed
                else ("exports", module.entity_key, None, str(row.get("source_module"))),
            )
            import_class, conditions = (
                ("unconditional", ())
                if module.language == "JavaScript"
                else ("elision_dependent", ("TS_ELISION_NOT_DECIDED",))
            )
            values.append(
                _Statement(
                    "reexports",
                    module,
                    row,
                    "export_references",
                    key,
                    resolved.status,
                    resolved.locator if resolved.status == "resolved" else None,
                    str(row.get("source_module")),
                    import_class,
                    conditions,
                    {},
                )
            )
        return values

    def _check_catalog(self, statements: list[_Statement]) -> None:
        """The projection must reproduce the catalog's import/re-export relations exactly."""
        observed: Counter[tuple[str, str, str | None, str | None]] = Counter(
            item.catalog_key for item in statements
        )
        expected: Counter[tuple[str, str, str | None, str | None]] = Counter()
        for key, relation in self.relations.items():
            if relation.kind == "exports" and relation.resolution_status in {
                "same_file",
                "unresolved",
            }:
                continue
            expected[key] = relation.count
        if observed != expected:
            missing = sorted(str(key) for key in (expected - observed))[:5]
            extra = sorted(str(key) for key in (observed - expected))[:5]
            raise ArchitectureBuildError(
                "ARCHITECTURE_CATALOG_MISMATCH", f"missing={missing} unexpected={extra}"
            )
        for item in statements:
            relation = self.relations[item.catalog_key]
            if relation.resolution_status != item.status:
                raise ArchitectureBuildError(
                    "ARCHITECTURE_CATALOG_MISMATCH", f"status differs for {item.catalog_key}"
                )

    # ----------------------------------------------------------------- nodes

    def _coverage_state(self, locator: str) -> CoverageState:
        module = self.modules.get(locator)
        row = self.inventory.get(locator, {})
        if module is None:
            return "not_analysed" if row.get("language") in CODE_LANGUAGES else "not_code"
        if module.outcome == "failed" or "STRUCTURE_FAILED" in module.limitations:
            return "structure_failed"
        if "EXTRACTOR_CONTRACT_CHANGED" in module.limitations:
            return "not_current"
        if "PARTIAL_PARSE_ARTIFACT" in module.limitations:
            return "partial"
        return "analysed"

    def run(self) -> ArchitectureBuild:
        statements = [item for module in self.source.modules for item in self._statements(module)]
        self._check_catalog(statements)

        module_locators = {
            locator
            for locator, row in self.inventory.items()
            if row.get("language") in CODE_LANGUAGES
        }
        module_locators |= set(self.modules)
        module_locators |= {item.target_locator for item in statements if item.target_locator}
        manifests: dict[str, list[str]] = defaultdict(list)
        for locator in self.inventory:
            directory, name = posixpath.split(locator)
            if name in MANIFESTS:
                manifests[directory].append(name)
        workspaces = {"", *manifests}
        directories: set[str] = {""}
        for locator in [
            *module_locators,
            *(posixpath.join(d, n) for d in manifests for n in manifests[d]),
        ]:
            directory = posixpath.dirname(locator)
            while directory not in directories:
                directories.add(directory)
                directory = posixpath.dirname(directory)
        roots = set(self.resolver.roots)

        nodes: dict[str, ArchitectureNode] = {}
        for path in sorted(directories):
            key = directory_key(path, workspaces)
            parent = None if path == "" else directory_key(posixpath.dirname(path), workspaces)
            files = sorted(manifests.get(path, []))
            ecosystems = sorted({MANIFESTS[name] for name in files})
            attributes: Row = {
                "path": path,
                "manifests": files,
                "ecosystems": ecosystems,
                "python_package": posixpath.join(path, "__init__.py") in self.inventory,
                "source_root": path in roots,
            }
            refs = [
                {
                    "table": "artifacts",
                    "id": self.inventory[posixpath.join(path, name)].get("id"),
                    "contract": None,
                    "locator": posixpath.join(path, name),
                    "row_digest": artifact_row_digest(self.inventory[posixpath.join(path, name)]),
                }
                for name in files
            ]
            derived = {"algorithm": CONTAINMENT_VERSION, "path": path, "manifests": files}
            label = path or "(repository root)"
            text = (
                f"{label} is a workspace ({', '.join(files) or 'repository root'})"
                if key.startswith("workspace:")
                else f"{label} is a directory containing analysed files"
            )
            link = (
                self._evidence("inventory_artifact", refs, derived, text)
                if refs
                else self._derivation(derived, text)
            )
            nodes[key] = ArchitectureNode(
                key,
                "workspace" if key.startswith("workspace:") else "container",
                parent,
                posixpath.basename(path) or "(repository root)",
                path or None,
                None,
                None,
                None,
                "derived",
                attributes,
                evidence=(link,),
            )
        for locator in sorted(module_locators):
            row = self.inventory.get(locator, {"locator": locator})
            module = self.modules.get(locator)
            state = self._coverage_state(locator)
            ref = {
                "table": "artifacts",
                "id": row.get("id"),
                "contract": None,
                "locator": locator,
                "row_digest": artifact_row_digest(row),
            }
            language = row.get("language") or "no language"
            text = f"{locator} is an inventoried file ({language}); coverage {state}"
            link = self._evidence("inventory_artifact", [ref], {"coverage": state}, text)
            identity = self.source.artifact_identities.get(locator)
            nodes[module_key(locator)] = ArchitectureNode(
                module_key(locator),
                "module",
                directory_key(posixpath.dirname(locator), workspaces),
                posixpath.basename(locator),
                locator,
                module.language if module else row.get("language"),
                module.entity_key if module else None,
                UUID(identity) if identity else None,
                state,
                {
                    "limitations": sorted(module.limitations) if module else [],
                    "literals": module.literal_state if module else "not_applicable",
                },
                evidence=(link,),
            )

        drafts = self._edges(statements, nodes)
        edges = self._finish_edges(drafts)
        aggregates = self._aggregate(edges, nodes)
        api_modules = [self._api_module(module, statements) for module in self.source.modules]
        api = ApiProjection(api_modules, self.source.endpoints, self._evidence)
        api.run()
        for key, node in api.nodes.items():
            if key in nodes:
                raise ArchitectureBuildError("ARCHITECTURE_KEY_COLLISION", key)
            nodes[key] = node
        self.api = api
        views = ViewsProjection(
            nodes=nodes,
            module_edges=edges,
            api_nodes=api.nodes,
            api_edges=api.edges,
            modules=api_modules,
            module_inputs=self.modules,
            manifests=self.source.manifests,
            tables=self.source.tables,
            roots=self.resolver.roots,
            evidence=self._evidence,
        )
        try:
            views.run()
        except ValueError as error:  # a view element produced twice: fail closed, never merge
            raise ArchitectureBuildError("ARCHITECTURE_KEY_COLLISION", str(error)) from error
        for key, node in views.new_nodes.items():
            if key in nodes:
                raise ArchitectureBuildError("ARCHITECTURE_KEY_COLLISION", key)
            nodes[key] = node
        self.views = views
        all_edges = sorted(
            [*edges, *aggregates, *api.edges, *views.edges], key=lambda edge: edge.key
        )
        if len({edge.key for edge in all_edges}) != len(all_edges):
            raise ArchitectureBuildError("ARCHITECTURE_KEY_COLLISION", "duplicate edge key")
        if len(nodes) > MAX_NODES or len(all_edges) > MAX_EDGES:
            raise ArchitectureBuildError(
                "ARCHITECTURE_LIMIT_EXCEEDED", f"nodes={len(nodes)} edges={len(all_edges)}"
            )
        all_edges = [replace(edge, ordinal=position) for position, edge in enumerate(all_edges)]
        nodes = self._metrics(nodes, all_edges)
        components = self._components(all_edges, nodes)
        diagnostics = self._diagnostics(
            all_edges, nodes, components, [*api.diagnostics, *views.diagnostics]
        )
        ordered_nodes = [
            replace(node, ordinal=position)
            for position, node in enumerate(sorted(nodes.values(), key=lambda node: node.key))
        ]
        coverage = self._coverage(statements, ordered_nodes)
        return self._result(ordered_nodes, all_edges, components, diagnostics, coverage)

    # ----------------------------------------------------------------- API inputs

    def _api_module(self, module: ModuleInput, statements: list[_Statement]) -> ApiModule:
        """Literal rows and one-hop import bindings of one module (Milestone B)."""
        bindings: dict[str, Binding] = {}
        python = _is_python(module.language)
        for item in statements:
            if item.source is not module or item.kind != "imports":
                continue
            row = item.row
            owner = row.get("owner_symbol_id")
            ref = {
                "table": "import_references",
                "id": row["id"],
                "contract": module.import_contract,
                "locator": module.locator,
                "row_digest": import_row_digest(
                    row, module.owner_kinds.get(str(owner)) if owner else None
                ),
            }
            name, alias = str(row.get("name") or ""), row.get("alias")
            target = item.target_locator if item.status == "resolved" else None
            if python:
                if target is None:
                    continue
                if row["form"] == "from" and name != "*":
                    bound = None if binding_kind(target, name) else name
                    bindings.setdefault(
                        str(alias or name), Binding(target, bound, item.module_text, ref)
                    )
                elif row["form"] == "import" and alias:
                    bindings.setdefault(str(alias), Binding(target, None, item.module_text, ref))
            elif row["form"] in {"es_import", "es_import_type"} and name:
                destination = target or f"external:{item.module_text}"
                bindings.setdefault(
                    str(alias or name), Binding(destination, name, item.module_text, ref)
                )
        return ApiModule(
            module.locator,
            module.language,
            module.literals_usable,
            module.literals,
            module.import_contract,
            bindings,
            module.top_level_names,
        )

    # ----------------------------------------------------------------- edges

    def _edges(
        self, statements: list[_Statement], nodes: dict[str, ArchitectureNode]
    ) -> dict[str, _EdgeDraft]:
        drafts: dict[str, _EdgeDraft] = {}
        for item in statements:
            source = module_key(item.source.locator)
            target: str | None = None
            text: str | None = None
            tier = "derived"
            if item.status == "resolved" and item.target_locator:
                target = module_key(item.target_locator)
            elif item.status == "external":
                ecosystem, name = external_package(
                    item.module_text, _is_python(item.source.language)
                )
                target = f"external:{ecosystem}:{name}"
                if target not in nodes:
                    derived = {
                        "algorithm": "external-package.v1",
                        "ecosystem": ecosystem,
                        "name": name,
                    }
                    link = self._derivation(
                        derived,
                        f"{name} is imported as a package outside the repository ({ecosystem})",
                    )
                    nodes[target] = ArchitectureNode(
                        target, "external_package", None, name, None, None, None, None, "derived",
                        {"ecosystem": ecosystem}, evidence=(link,),
                    )  # fmt: skip
            else:
                text = item.module_text
                tier = "unknown"
            key = "|".join(
                [item.kind, source, target or f"text:{text}", item.status, item.import_class]
            )
            draft = drafts.get(key)
            if draft is None:
                draft = drafts[key] = _EdgeDraft(
                    item.kind, source, target, text, item.status, item.import_class, tier
                )
            draft.statements.append(item)
            draft.conditions.update(item.conditions)
            draft.forms.add(str(item.row.get("form") or item.row.get("kind")))
            draft.module_texts.add(item.module_text)
            draft.star = draft.star or bool(item.attributes.get("star"))
            draft.side_effect = draft.side_effect or bool(item.attributes.get("side_effect"))
            draft.catalog_facts.add(self.relations[item.catalog_key].fact_digest)
        return drafts

    def _finish_edges(self, drafts: dict[str, _EdgeDraft]) -> list[ArchitectureEdge]:
        edges: list[ArchitectureEdge] = []
        for key, draft in sorted(drafts.items()):
            rows = sorted(
                draft.statements,
                key=lambda item: (
                    int(item.row.get("statement_ordinal") or item.row.get("ordinal") or 0),
                    int(item.row.get("item_ordinal") or 0),
                ),
            )
            refs = []
            for item in rows[:STATEMENT_REFS_PER_EDGE]:
                owner = item.row.get("owner_symbol_id")
                row_digest = (
                    import_row_digest(
                        item.row, item.source.owner_kinds.get(str(owner)) if owner else None
                    )
                    if item.table == "import_references"
                    else export_row_digest(item.row)
                )
                refs.append(
                    {
                        "table": item.table,
                        "id": item.row["id"],
                        "contract": item.source.import_contract,
                        "locator": item.source.locator,
                        "row_digest": row_digest,
                    }
                )
            supporting: dict[str, Row] = {}
            for item in rows:
                for ref in item.support:
                    supporting.setdefault(f"{ref['table']}:{ref['id']}", ref)
            refs.extend(list(supporting.values())[:STATEMENT_REFS_PER_EDGE])
            statements = len(
                {
                    item.row.get("statement_ordinal", item.row.get("ordinal"))
                    for item in draft.statements
                }
            )
            target = draft.target or f"unresolved {draft.text!r} ({draft.status})"
            verb = "imports" if draft.kind == "imports" else "re-exports from"
            text = (
                f"{draft.source.removeprefix('module:')} {verb} {target}: "
                f"{len(draft.statements)} name(s) in {statements} statement(s), "
                f"class {draft.import_class}"
            )
            derived = {
                "kind": draft.kind,
                "class": draft.import_class,
                "status": draft.status,
                "statements": statements,
                "items": len(draft.statements),
                "refs_omitted": max(0, len(rows) - STATEMENT_REFS_PER_EDGE),
            }
            links = [
                self._evidence("import_statements", refs, derived, text),
                *(EvidenceLink("catalog", value) for value in sorted(draft.catalog_facts)),
            ]
            attributes: Row = {
                "forms": sorted(draft.forms),
                "statements": statements,
                "module_text": sorted(draft.module_texts),
                "star": draft.star,
                "side_effect": draft.side_effect,
            }
            edges.append(
                ArchitectureEdge(
                    0,
                    key,
                    "imports" if draft.kind == "imports" else "reexports",
                    draft.source,
                    draft.target,
                    draft.text,
                    "derived" if draft.tier == "derived" else "unknown",
                    draft.status,  # type: ignore[arg-type]
                    draft.import_class,  # type: ignore[arg-type]
                    len(draft.statements),
                    attributes,
                    tuple(sorted(draft.conditions)),
                    tuple(links),
                )
            )
        return edges

    def _aggregate(
        self, edges: list[ArchitectureEdge], nodes: dict[str, ArchitectureNode]
    ) -> list[ArchitectureEdge]:
        values: list[ArchitectureEdge] = []
        for shape in aggregate_shapes(edges, parents_of(nodes.values())):
            derived = {
                "algorithm": AGGREGATION_VERSION,
                "scope": shape.scope,
                "constituents": digest(list(shape.keys)),
                "constituent_count": len(shape.keys),
                "count": shape.count,
            }
            link = self._derivation(
                derived,
                f"{shape.source} depends on {shape.target}: {len(shape.keys)} module edge(s), "
                f"{shape.count} name(s), class {shape.import_class}",
            )
            values.append(
                ArchitectureEdge(
                    0,
                    shape.key,
                    "depends_on",
                    shape.source,
                    shape.target,
                    None,
                    "derived",
                    "resolved",
                    cast(ImportClass, shape.import_class),
                    shape.count,
                    shape.attributes(),
                    (),
                    (link,),
                )
            )
        return values

    # ----------------------------------------------------------------- metrics

    def _metrics(
        self, nodes: dict[str, ArchitectureNode], edges: list[ArchitectureEdge]
    ) -> dict[str, ArchitectureNode]:
        parents = parents_of(nodes.values())
        fan_out: dict[str, set[str]] = defaultdict(set)
        fan_in: dict[str, set[str]] = defaultdict(set)
        external: dict[str, set[str]] = defaultdict(set)
        unresolved: Counter[str] = Counter()
        internal: list[tuple[str, str]] = []
        for edge in edges:
            if edge.kind not in MODULE_EDGE_KINDS:
                continue
            if edge.target_key is None:
                unresolved[edge.source_key] += edge.count
            elif edge.target_key.startswith("external:"):
                external[edge.source_key].add(edge.target_key)
            elif edge.source_key != edge.target_key:
                fan_out[edge.source_key].add(edge.target_key)
                fan_in[edge.target_key].add(edge.source_key)
                internal.append((edge.source_key, edge.target_key))
        efferent: dict[str, set[str]] = defaultdict(set)
        afferent: dict[str, set[str]] = defaultdict(set)
        for source, target in set(internal):
            source_chain = ancestors(source, parents)[1:]
            target_chain = ancestors(target, parents)[1:]
            for container in source_chain:
                if container not in target_chain:
                    efferent[container].add(target)
            for container in target_chain:
                if container not in source_chain:
                    afferent[container].add(source)
        values: dict[str, ArchitectureNode] = {}
        for key, node in nodes.items():
            if node.kind == "module":
                metrics: Row = {
                    "fan_out": len(fan_out[key]),
                    "fan_in": len(fan_in[key]),
                    "external_packages": len(external[key]),
                    "unresolved_names": unresolved[key],
                }
            elif node.kind in {"workspace", "container"}:
                ce, ca = len(efferent[key]), len(afferent[key])
                metrics = {
                    "efferent_modules": ce,
                    "afferent_modules": ca,
                    "instability": round(ce / (ca + ce), 4) if ca + ce else None,
                }
            elif node.kind == "external_package":
                metrics = {"importing_modules": len({k for k, v in external.items() if key in v})}
            else:
                metrics = {}
            values[key] = replace(node, metrics=metrics)
        return values

    # ----------------------------------------------------------------- components

    def _components(
        self, edges: list[ArchitectureEdge], nodes: dict[str, ArchitectureNode]
    ) -> list[ArchitectureComponent]:
        values: list[ArchitectureComponent] = []
        for position, shape in enumerate(component_shapes(edges, parents_of(nodes.values()))):
            derived = {
                "algorithm": SCC_VERSION,
                "level": shape.level,
                "class_set": shape.class_set,
                "scope": shape.scope,
                "members": digest(list(shape.members)),
                "size": len(shape.members),
            }
            noun = "import cycle" if shape.level == "module" else "aggregate dependency cycle"
            unit = "modules" if shape.level == "module" else "containers"
            link = self._derivation(
                derived,
                f"{noun} of {len(shape.members)} {unit} in class set {shape.class_set}",
            )
            values.append(
                ArchitectureComponent(
                    shape.key,
                    position,
                    cast(ComponentLevel, shape.level),
                    cast(ClassSet, shape.class_set),
                    shape.scope,
                    shape.members,
                    shape.witness,
                    shape.attributes(),
                    (link,),
                )
            )
        return values

    # ----------------------------------------------------------------- diagnostics

    def _diagnostics(
        self,
        edges: list[ArchitectureEdge],
        nodes: dict[str, ArchitectureNode],
        components: list[ArchitectureComponent],
        extra: Sequence[tuple[str, str, str, tuple[str, ...], Row, tuple[EvidenceLink, ...]]] = (),
    ) -> list[ArchitectureDiagnostic]:
        drafts: list[tuple[str, str, str, tuple[str, ...], Row, tuple[EvidenceLink, ...]]] = list(
            extra
        )
        by_key = {edge.key: edge for edge in edges}
        for component in components:
            if component.level == "module":
                conditions = sorted(
                    {c for key in component.witness for c in by_key[key].conditions}
                )
                drafts.append(
                    (
                        "graph", "IMPORT_CYCLE", "derived", component.members,
                        {
                            "class_set": component.class_set,
                            "size": len(component.members),
                            "component": component.key,
                            "limitations": conditions,
                        },
                        component.evidence,
                    )
                )  # fmt: skip
            else:
                drafts.append(
                    (
                        "graph", "AGGREGATE_DEPENDENCY_CYCLE", "derived", component.members,
                        {
                            "class_set": component.class_set,
                            "size": len(component.members),
                            "component": component.key,
                            "scope": component.scope_key,
                            "module_cycle_underlying": component.attributes[
                                "module_cycle_underlying"
                            ],
                        },
                        component.evidence,
                    )
                )  # fmt: skip
        per_module: dict[tuple[str, str], list[ArchitectureEdge]] = defaultdict(list)
        for edge in edges:
            if edge.kind not in MODULE_EDGE_KINDS:
                continue
            if edge.source_key == edge.target_key:
                per_module[("SELF_IMPORT", edge.source_key)].append(edge)
            status_codes = {
                "not_found": "IMPORT_NOT_FOUND",
                "ambiguous": "IMPORT_AMBIGUOUS",
                "not_attempted": "ALIAS_NOT_RESOLVED",
            }
            code = status_codes.get(str(edge.resolution_status))
            if code:
                per_module[(code, edge.source_key)].append(edge)
            if edge.attributes.get("star"):
                per_module[("STAR_IMPORT", edge.source_key)].append(edge)
            for code in direction_codes(edge, nodes):
                per_module[(code, edge.source_key)].append(edge)
        categories = {
            "SELF_IMPORT": ("graph", "derived"),
            "IMPORT_NOT_FOUND": ("resolution", "unknown"),
            "IMPORT_AMBIGUOUS": ("resolution", "unknown"),
            "ALIAS_NOT_RESOLVED": ("resolution", "unknown"),
            "STAR_IMPORT": ("resolution", "observed"),
            **{code: ("direction", "inferred") for code in DIRECTION_CODES},
        }
        for (code, module), members in sorted(per_module.items()):
            category, tier = categories[code]
            targets = sorted({edge.target_text or edge.target_key or "" for edge in members})
            links = tuple(
                sorted(
                    {link for edge in members for link in edge.evidence},
                    key=lambda value: (value.source, value.digest),
                )
            )
            drafts.append(
                (
                    category, code, tier, (module,),
                    {
                        "names": sum(edge.count for edge in members),
                        "targets": targets[:DIAGNOSTIC_TARGET_SAMPLE],
                        "targets_total": len(targets),
                        **({"conditions": list(DIRECTION_CODES[code])}
                           if code in DIRECTION_CODES else {}),
                    },
                    links,
                )
            )  # fmt: skip
        coverage_codes = {
            "not_analysed": "FILE_NOT_ANALYSED",
            "structure_failed": "FILE_NOT_ANALYSED",
            "not_current": "DECLARATION_CONTRACT_NOT_CURRENT",
            "partial": "PARTIAL_PARSE",
        }
        for key, node in sorted(nodes.items()):
            code = coverage_codes.get(node.coverage_state) if node.kind == "module" else None
            if code:
                drafts.append(
                    (
                        "coverage", code, "unknown" if code != "PARTIAL_PARSE" else "observed",
                        (key,), {"coverage_state": node.coverage_state}, node.evidence,
                    )
                )  # fmt: skip
        for source in self.source.modules:
            key = module_key(source.locator)
            found = nodes.get(key)
            if found is None or source.outcome == "failed":
                continue
            if source.literal_state in {"not_extracted", "failed", "not_current"}:
                drafts.append(
                    (
                        "coverage", "LITERALS_NOT_EXTRACTED", "unknown", (key,),
                        {"state": source.literal_state}, found.evidence,
                    )
                )  # fmt: skip
            elif source.literal_state == "partial":
                drafts.append(
                    (
                        "coverage", "LITERALS_PARTIAL", "observed", (key,),
                        {"state": source.literal_state}, found.evidence,
                    )
                )  # fmt: skip
            opaque = [row for row in source.literals if row["kind"] == "opaque_dynamic_import"]
            if opaque:
                link = self._evidence(
                    "literal_rows",
                    [literal_ref(source.locator, row) for row in opaque[:STATEMENT_REFS_PER_EDGE]],
                    {"count": len(opaque)},
                    f"{source.locator} loads {len(opaque)} module(s) with a non-literal specifier",
                )
                drafts.append(
                    (
                        "resolution", "DYNAMIC_IMPORT_OPAQUE", "unknown", (key,),
                        {"count": len(opaque), "lines": [row["start_line"] for row in opaque][:20]},
                        (link,),
                    )
                )  # fmt: skip
        drafts.sort(key=lambda item: (item[0], item[1], item[3], digest(item[4])))
        return [
            ArchitectureDiagnostic(position, code, category, tier, subjects, attributes, links)  # type: ignore[arg-type]
            for position, (category, code, tier, subjects, attributes, links) in enumerate(drafts)
        ]

    # ----------------------------------------------------------------- coverage and result

    def _coverage(
        self, statements: list[_Statement], nodes: Sequence[ArchitectureNode]
    ) -> dict[str, object]:
        modules: Counter[str] = Counter()
        by_workspace: dict[str, Counter[str]] = defaultdict(Counter)
        parents = {item.key: item for item in nodes}
        for node in nodes:
            if node.kind != "module":
                continue
            modules[node.coverage_state] += 1
            workspace = node.parent_key
            while workspace and not workspace.startswith("workspace:"):
                workspace = parents[workspace].parent_key
            by_workspace[workspace or "workspace:."][f"{node.language}:{node.coverage_state}"] += 1
        alias_prefixes: Counter[str] = Counter()
        alias_files: set[str] = set()
        for item in statements:
            if item.status == "not_attempted":
                alias_prefixes[alias_prefix(item.module_text)] += 1
                alias_files.add(item.source.locator)
        return {
            "modules": dict(sorted(modules.items())),
            "by_workspace": {
                key: dict(sorted(value.items())) for key, value in sorted(by_workspace.items())
            },
            "statements": {
                "by_resolution": dict(sorted(Counter(item.status for item in statements).items())),
                "by_class": dict(sorted(Counter(item.import_class for item in statements).items())),
                "total": len(statements),
            },
            "alias": {
                "prefixes": dict(sorted(alias_prefixes.items())),
                "statements": sum(alias_prefixes.values()),
                "files": sorted(alias_files),
            },
            **self._literal_coverage(),
            "catalog_outcome": self.source.catalog_outcome,
        }

    def _literal_coverage(self) -> Row:
        states: Counter[str] = Counter()
        guard: Counter[str] = Counter()
        opaque = 0
        observable = True
        for module in self.source.modules:
            if module.outcome == "failed":
                continue
            states[module.literal_state] += 1
            if module.literal_state == "not_applicable":
                continue
            observable = observable and module.literals_usable
            opaque += sum(row["kind"] == "opaque_dynamic_import" for row in module.literals)
            if _is_python(module.language):
                guard["known" if module.classification_known else "unknown"] += 1
        api = self.api.coverage(states) if self.api is not None else {}
        return {
            "python_guard_context": "known" if not guard["unknown"] else "unknown",
            "python_guard_files": dict(sorted(guard.items())),
            "literals": dict(sorted(states.items())),
            "opaque_dynamic_imports": (
                "not_observable" if not observable else "present" if opaque else "none_observed"
            ),
            "opaque_dynamic_import_count": opaque,
            "api": api,
            **(self.views.coverage if self.views is not None else {}),
        }

    def _result(
        self,
        nodes: list[ArchitectureNode],
        edges: list[ArchitectureEdge],
        components: list[ArchitectureComponent],
        diagnostics: list[ArchitectureDiagnostic],
        coverage: dict[str, object],
    ) -> ArchitectureBuild:
        evidence = tuple(sorted(self.evidence.values(), key=lambda item: item.digest))
        graph = graph_digest_of(
            [node_digest(node) for node in nodes],
            [edge_digest(edge) for edge in edges],
            [component_digest(item) for item in components],
            [diagnostic_digest(item) for item in diagnostics],
            coverage,
        )
        instance = digest({"graph": graph, "evidence": [item.digest for item in evidence]})
        portable = portable_digest(nodes, edges, components, diagnostics, evidence)
        states = {node.coverage_state for node in nodes if node.kind == "module"}
        outcome: ArchitectureOutcome = (
            "partial"
            if states & {"partial", "not_current", "structure_failed", "not_analysed"}
            or self.source.catalog_outcome != "complete"
            else "complete"
        )
        return ArchitectureBuild(
            tuple(nodes),
            tuple(edges),
            tuple(components),
            tuple(diagnostics),
            evidence,
            graph,
            instance,
            portable,
            coverage,
            outcome,
        )


def portable_digest(
    nodes: Iterable[ArchitectureNode],
    edges: Iterable[ArchitectureEdge],
    components: Iterable[ArchitectureComponent],
    diagnostics: Iterable[ArchitectureDiagnostic],
    evidence: Iterable[ArchitectureEvidence],
) -> str:
    """Database-independent projection: portable keys, texts and row digests, no row ids,
    no catalog fact digests and no runtime-bound contract digests."""

    def portable_evidence(item: ArchitectureEvidence) -> list[Any]:
        refs = [
            [ref.get("table"), ref.get("locator"), ref.get("row_digest")]
            for ref in item.source_refs
        ]
        return [item.kind, item.rendered_text, item.derived, refs]

    by_digest = {item.digest: portable_evidence(item) for item in evidence}

    def links(values: Iterable[EvidenceLink]) -> list[Any]:
        return sorted(
            [digest(by_digest[link.digest])] if link.source == "architecture" else ["catalog"]
            for link in values
        )

    return digest(
        {
            "nodes": [
                [n.key, n.kind, n.parent_key, n.name, n.locator, n.language, n.coverage_state,
                 n.attributes, n.metrics, links(n.evidence)]
                for n in nodes
            ],
            "edges": [
                [e.key, e.kind, e.source_key, e.target_key, e.target_text, e.tier,
                 e.resolution_status, e.import_class, e.count, e.attributes, list(e.conditions),
                 links(e.evidence)]
                for e in edges
            ],
            "components": [
                [c.key, c.level, c.class_set, c.scope_key, list(c.members), list(c.witness),
                 c.attributes]
                for c in components
            ],
            "diagnostics": [
                [d.code, d.category, d.tier, list(d.subjects), d.attributes] for d in diagnostics
            ],
        }
    )  # fmt: skip


def build_architecture(source: ArchitectureInput) -> ArchitectureBuild:
    return _Builder(source).run()


def architecture_input(
    *,
    inventory: Sequence[Row],
    analyses: Sequence[Row],
    imports: Mapping[str, Sequence[Row]],
    exports: Mapping[str, Sequence[Row]],
    extractions: Mapping[str, Sequence[Row]],
    owner_kinds: Mapping[str, str],
    producer_digest: str,
    entities: Sequence[SemanticEntity],
    relations: Sequence[SemanticRelation],
    relation_counts: Mapping[str, int],
    catalog_outcome: str,
    literals: Mapping[str, Sequence[Row]] | None = None,
    top_level_names: Mapping[str, Iterable[str]] | None = None,
    endpoints: Sequence[EndpointInput] = (),
    manifests: Sequence[ManifestInput] = (),
    tables: Sequence[TableInput] = (),
) -> ArchitectureInput:
    """Normalise one catalog run and its structural rows into the builder's input.

    Shared by the PostgreSQL store and the unit tests so both feed the builder identically.
    Skipped analyses have no catalog entity and are excluded, exactly as in the catalog.
    """
    files = {entity.locator: entity for entity in entities if entity.kind == "file"}
    modules: list[ModuleInput] = []
    for analysis in sorted(analyses, key=lambda row: str(row["locator"])):
        if analysis["outcome"] == "skipped":
            continue
        locator = str(analysis["locator"])
        entity = files.get(locator)
        if entity is None:
            raise ArchitectureBuildError(
                "ARCHITECTURE_CATALOG_MISMATCH", f"no entity for {locator}"
            )
        key = str(analysis["id"])
        language = analysis.get("language")
        declaration = analysis.get("declaration_extraction_id")
        contract: str | None = producer_digest
        if not _is_python(language):
            row = next(
                (item for item in extractions.get(key, ()) if item["id"] == declaration), None
            )
            contract = str(row["contract_digest"]) if row else None
        module_exports = (
            ()
            if _is_python(language)
            else tuple(row for row in exports.get(key, ()) if row["extraction_id"] == declaration)
        )
        module_imports = tuple(imports.get(key, ()))
        owners = {
            str(row["owner_symbol_id"]): owner_kinds[str(row["owner_symbol_id"])]
            for row in module_imports
            if row.get("owner_symbol_id") and str(row["owner_symbol_id"]) in owner_kinds
        }
        literal_name = (
            PYTHON_LITERALS
            if _is_python(language)
            else TS_LITERALS
            if language in {"TypeScript", "JavaScript"}
            else None
        )
        literal_row = (
            next(
                (item for item in extractions.get(key, ()) if item["extractor"] == literal_name),
                None,
            )
            if literal_name
            else None
        )
        state = literal_state(literal_name, literal_row)
        module_literals = (
            tuple((literals or {}).get(str(literal_row["id"]), ()))
            if literal_row is not None and state in {"extracted", "partial"}
            else ()
        )
        modules.append(
            ModuleInput(
                locator,
                language,
                str(analysis["outcome"]),
                entity.entity_key,
                tuple(entity.limitations),
                contract,
                module_imports,
                module_exports,
                owners,
                state,
                literal_row if state in {"extracted", "partial"} else None,
                module_literals,
                frozenset((top_level_names or {}).get(key, ())),
            )
        )
    sources = {entity.entity_key for entity in files.values()}
    selected = [
        CatalogRelationInput(
            relation.kind,
            relation.source_entity_key,
            relation.target_entity_key,
            relation.target_text,
            relation.resolution_status,
            relation.fact_digest,
            int(relation_counts.get(relation.fact_digest, 1)),
        )
        for relation in relations
        if relation.kind in {"imports", "exports"} and relation.source_entity_key in sources
    ]
    identities = {
        str(row["locator"]): str(row["stable_identity_id"])
        for row in inventory
        if row.get("stable_identity_id")
    }
    return ArchitectureInput(
        tuple(inventory),
        tuple(modules),
        tuple(selected),
        catalog_outcome,
        identities,
        tuple(endpoints),
        tuple(manifests),
        tuple(tables),
    )


def literal_state(extractor: str | None, row: Row | None) -> str:
    """Per-artifact literal coverage: only a current, extracted pass is usable (F10)."""
    if extractor is None:
        return "not_applicable"
    if row is None:
        return "not_extracted"
    if row["outcome"] in {"failed", "skipped"}:
        return "failed"
    if row["contract_digest"] != extractor_contract_digest(extractor):
        return "not_current"
    return "partial" if row["outcome"] == "partial" else "extracted"
