"""Stage 14 Milestone C: integrations, database, frontend-route and subsystem projections.

Pure functions of persisted rows (stored manifest chunks, catalog ``db_table`` facts, literal
rows and the module graph). Nothing is executed and no repository file is read.

Contracts (recorded in ``architecture_contract()``):

* ``manifest-facts.v1``: dependency names from stored ``pyproject.toml`` / ``package.json``
  chunks (``[project]`` dependencies, optional and PEP 735 groups, Poetry tables; npm
  dependency sections). Version and URL text is never stored, only the specifier kind.
* ``package-matching.v1``: Python import names match a declaration only after PEP 503
  normalisation of both; a mismatch stays unknown (import name != distribution name). npm
  package names match exactly through the workspace chain (hoisting). Absence of an import
  is claimed only when the workspace's coverage is complete.
* ``database-view.v1``: tables from SQLAlchemy models (catalog) and ``op.create_table``
  literal rows, merged by name; foreign keys from ``ForeignKey("t.c")`` text (pattern);
  ``uses_model`` from resolved imports of a model class.
* ``frontend-routes.v1``: ``<Route>`` rows joined with their enclosing routes; elements bound
  to modules through resolved imports or the same file.
* ``layer-candidates.v1`` and ``functional-slices.v1``: inferred structure from observed
  dependency direction, names and API candidates; never intended architecture.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import tomllib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

from buildlens.application.architecture_api import ApiModule, literal_ref
from buildlens.domain.architecture import (
    CLASS_SETS,
    ArchitectureEdge,
    ArchitectureNode,
    EvidenceLink,
    Row,
)
from buildlens.domain.source import digest
from buildlens.domain.stdlib_modules import NODE_BUILTINS, PYTHON_STDLIB

Evidence = Callable[[Any, Sequence[Row], Row, str], EvidenceLink]
DiagnosticDraft = tuple[str, str, str, tuple[str, ...], Row, tuple[EvidenceLink, ...]]

NPM_SECTIONS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_FOREIGN_KEY = re.compile(r"""ForeignKey\(\s*["']([A-Za-z_][\w]*)\.[A-Za-z_][\w]*["']""")
_GENERIC_STEMS = frozenset(
    {
        "routes",
        "route",
        "router",
        "api",
        "schemas",
        "schema",
        "page",
        "panel",
        "view",
        "views",
        "client",
        "service",
        "services",
        "queries",
        "store",
        "index",
        "main",
        "app",
        "test",
        "tests",
        "spec",
        "types",
        "utils",
        "util",
        "support",
    }
)
CONVENTIONS = {
    "domain": ("domain", 0),
    "models": ("domain", 0),
    "entities": ("domain", 0),
    "core": ("domain", 0),
    "application": ("application", 1),
    "services": ("application", 1),
    "usecases": ("application", 1),
    "infrastructure": ("infrastructure", 2),
    "adapters": ("infrastructure", 2),
    "persistence": ("infrastructure", 2),
    "api": ("interface", 3),
    "cli": ("interface", 3),
    "routes": ("interface", 3),
    "components": ("interface", 3),
    "pages": ("interface", 3),
    "views": ("interface", 3),
    "ui": ("interface", 3),
}
RUNTIME_CLASSES = CLASS_SETS["S2"]


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def chunk_row_digest(row: Row) -> str:
    """Semantic fields of a ``chunks`` row; ``content_hash`` is recomputed from content."""
    return digest(
        {
            "unit_anchor": row.get("unit_anchor"),
            "part_index": row.get("part_index"),
            "kind": row.get("kind"),
            "content_hash": hashlib.sha256(str(row.get("content", "")).encode()).hexdigest(),
            "start_offset": row.get("start_offset"),
            "end_offset": row.get("end_offset"),
        }
    )


def chunk_ref(locator: str, row: Row) -> Row:
    return {
        "table": "chunks",
        "id": row["id"],
        "contract": None,
        "locator": locator,
        "row_digest": chunk_row_digest(row),
    }


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class ManifestInput:
    locator: str
    ecosystem: str
    chunks: tuple[Row, ...]


@dataclass(frozen=True, slots=True)
class TableInput:
    """One catalog ``db_table`` entity (SQLAlchemy model or Alembic call)."""

    entity_key: str
    locator: str
    recognizer: str
    table: str | None
    model: str | None
    fact_digest: str
    assignments: tuple[str, ...] = ()


@dataclass(slots=True)
class Declaration:
    name: str
    section: str
    kind: str
    refs: list[Row] = field(default_factory=list)


# --------------------------------------------------------------------------- manifests


def _units(chunks: Sequence[Row]) -> tuple[dict[str, tuple[str, list[Row]]], int]:
    """unit anchor -> (text, chunk rows); chunks whose content hash fails are dropped."""
    grouped: dict[str, list[Row]] = defaultdict(list)
    invalid = 0
    for row in chunks:
        content = str(row["content"])
        if hashlib.sha256(content.encode()).hexdigest() != row["content_hash"]:
            invalid += 1
            continue
        grouped[str(row["unit_anchor"])].append(row)
    values = {}
    for anchor, rows in grouped.items():
        rows.sort(key=lambda row: int(row["part_index"]))
        values[anchor] = ("".join(str(row["content"]) for row in rows), rows)
    return values, invalid


def _requirement(text: str) -> tuple[str, str] | None:
    match = _REQUIREMENT.match(text)
    if match is None:
        return None
    rest = text[match.end() :].split(";", 1)[0]
    return match.group(1), "direct_reference" if "@" in rest else "registry"


def parse_pyproject(chunks: Sequence[Row]) -> tuple[list[Declaration], list[str]]:
    declarations: list[Declaration] = []
    problems: list[str] = []
    units, invalid = _units(chunks)
    if invalid:
        problems.append("CHUNK_HASH_MISMATCH")
    for anchor, (text, rows) in sorted(units.items()):
        try:
            table, key = json.loads(anchor.removeprefix("toml:").rsplit(":", 1)[0])
        except (ValueError, TypeError):
            continue
        section: str | None = None
        if str(key).startswith("["):
            continue  # the table header line is its own unit
        if table == "[project]" and key == "dependencies":
            section = "project.dependencies"
        elif table == "[project.optional-dependencies]":
            section = f"optional:{key}"
        elif table == "[dependency-groups]":
            section = f"group:{key}"
        elif re.match(r"^\[tool\.poetry(\.group\.[^.\]]+)?\.(dev-)?dependencies\]$", str(table)):
            section = str(table).strip("[]")
        if section is None:
            continue
        try:
            value = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            problems.append("UNIT_UNPARSEABLE")
            continue
        items = next(iter(value.values()), None) if len(value) == 1 else None
        if section.startswith("tool.poetry"):
            names = [(str(k), "registry") for k in value if k != "python"]
        elif isinstance(items, list):
            names = [
                found
                for item in items
                if isinstance(item, str) and (found := _requirement(item)) is not None
            ]
        else:
            problems.append("UNIT_SHAPE_UNSUPPORTED")
            continue
        for name, kind in names:
            declarations.append(Declaration(name, section, kind, list(rows)))
    return declarations, problems


def parse_package_json(chunks: Sequence[Row]) -> tuple[list[Declaration], str | None, list[str]]:
    declarations: list[Declaration] = []
    problems: list[str] = []
    name: str | None = None
    units, invalid = _units(chunks)
    if invalid:
        problems.append("CHUNK_HASH_MISMATCH")
    for anchor, (text, rows) in sorted(units.items()):
        try:
            key = json.loads(anchor.removeprefix("json:"))[0]
        except (ValueError, TypeError, IndexError, KeyError):
            continue
        if key not in {*NPM_SECTIONS, "name"}:
            continue
        try:
            value = json.loads("{" + text + "}")
        except ValueError:
            problems.append("UNIT_UNPARSEABLE")
            continue
        member = value.get(key)
        if key == "name":
            name = member if isinstance(member, str) else None
            continue
        if not isinstance(member, dict):
            problems.append("UNIT_SHAPE_UNSUPPORTED")
            continue
        for package, specifier in member.items():
            spec = str(specifier)
            kind = (
                "workspace"
                if spec.startswith("workspace:")
                else "local_path"
                if spec.startswith(("file:", "link:"))
                else "direct_reference"
                if "://" in spec or spec.startswith(("git", "github:"))
                else "registry"
            )
            declarations.append(Declaration(str(package), key, kind, list(rows)))
    return declarations, name, problems


# --------------------------------------------------------------------------- projection


class ViewsProjection:
    def __init__(
        self,
        *,
        nodes: dict[str, ArchitectureNode],
        module_edges: Sequence[ArchitectureEdge],
        api_nodes: Mapping[str, ArchitectureNode],
        api_edges: Sequence[ArchitectureEdge],
        modules: Sequence[ApiModule],
        module_inputs: Mapping[str, Any],
        manifests: Sequence[ManifestInput],
        tables: Sequence[TableInput],
        roots: Iterable[str],
        evidence: Evidence,
    ) -> None:
        self.nodes = nodes
        self.module_edges = [e for e in module_edges if e.kind in {"imports", "reexports"}]
        self.api_nodes = api_nodes
        self.api_edges = api_edges
        self.modules = {module.locator: module for module in modules}
        self.inputs = module_inputs
        self.manifests = manifests
        self.tables = tables
        self.roots = sorted(roots)
        self.evidence = evidence
        self.new_nodes: dict[str, ArchitectureNode] = {}
        self.edges: list[ArchitectureEdge] = []
        self.diagnostics: list[DiagnosticDraft] = []
        self.coverage: Row = {}
        self.parents = {key: node.parent_key for key, node in nodes.items()}
        self.children: dict[str | None, list[str]] = defaultdict(list)
        for key, node in sorted(nodes.items()):
            self.children[node.parent_key].append(key)

    # ----------------------------------------------------------------- helpers

    def _workspace(self, key: str) -> str:
        current: str | None = key
        while current is not None and not current.startswith("workspace:"):
            current = self.parents.get(current)
        return current or "workspace:."

    def _workspace_chain(self, workspace: str) -> list[str]:
        chain = []
        current: str | None = workspace
        while current is not None:
            if current.startswith("workspace:"):
                chain.append(current)
            current = self.parents.get(current)
        return chain

    def _modules_under(self, key: str) -> list[str]:
        values, stack = [], [key]
        while stack:
            current = stack.pop()
            if current.startswith("module:"):
                values.append(current)
            stack.extend(child for child in self.children.get(current, []) if child in self.nodes)
        return sorted(values)

    def _complete(self, modules: Iterable[str]) -> list[str]:
        """Blocking gaps for an absence claim over these modules (``absence-gating.v1``)."""
        gaps: set[str] = set()
        for key in modules:
            node = self.nodes[key]
            module = self.inputs.get(key.removeprefix("module:"))
            if node.coverage_state not in {"analysed", "not_code"}:
                gaps.add(f"FILE_{node.coverage_state.upper()}")
            if module is not None and not module.literals_usable:
                gaps.add("LITERALS_NOT_EXTRACTED")
            if module is not None and any(
                row["kind"] == "opaque_dynamic_import" for row in module.literals
            ):
                gaps.add("OPAQUE_DYNAMIC_IMPORT")
        for edge in self.module_edges:
            if edge.source_key in modules and edge.resolution_status == "not_attempted":
                gaps.add("ALIAS_NOT_RESOLVED")
        return sorted(gaps)

    def _edge(
        self,
        key: str,
        kind: str,
        source: str,
        target: str | None,
        tier: str,
        attributes: Row,
        links: Iterable[EvidenceLink],
        conditions: Iterable[str] = (),
        count: int = 1,
    ) -> None:
        self.edges.append(
            ArchitectureEdge(
                0,
                key,
                cast(Any, kind),
                source,
                target,
                None,
                cast(Any, tier),
                None,
                None,
                count,
                attributes,
                tuple(sorted(set(conditions))),
                tuple(sorted(set(links), key=lambda link: (link.source, link.digest))),
            )
        )

    def _node(
        self,
        key: str,
        kind: str,
        parent: str | None,
        name: str,
        attributes: Row,
        links: Iterable[EvidenceLink],
        locator: str | None = None,
        language: str | None = None,
        entity: str | None = None,
    ) -> None:
        self.new_nodes[key] = ArchitectureNode(
            key,
            cast(Any, kind),
            parent,
            name,
            locator,
            language,
            entity,
            None,
            "derived",
            attributes,
            evidence=tuple(sorted(set(links), key=lambda link: (link.source, link.digest))),
        )

    def _diagnose(
        self,
        category: str,
        code: str,
        tier: str,
        subjects: Sequence[str],
        attributes: Row,
        links: Iterable[EvidenceLink],
    ) -> None:
        self.diagnostics.append(
            (
                category,
                code,
                tier,
                tuple(subjects),
                attributes,
                tuple(sorted(set(links), key=lambda link: (link.source, link.digest))),
            )
        )

    def run(self) -> None:
        self._integrations()
        self._database()
        self._frontend()
        self._layers()
        self._slices()

    # ----------------------------------------------------------------- C1 integrations

    def _integrations(self) -> None:
        declared: dict[str, dict[str, list[tuple[Declaration, str]]]] = defaultdict(
            lambda: defaultdict(list)
        )  # workspace -> ecosystem -> [(declaration, manifest locator)]
        local_packages: set[str] = set()
        parsed = 0
        for manifest in self.manifests:
            workspace = self._workspace(f"container:{posixpath.dirname(manifest.locator)}")
            directory = posixpath.dirname(manifest.locator)
            for key in (f"workspace:{directory or '.'}", f"container:{directory}"):
                if key in self.nodes:
                    workspace = key if key.startswith("workspace:") else self._workspace(key)
                    break
            if manifest.ecosystem == "python":
                found_items, problems = parse_pyproject(manifest.chunks)
                name = None
            else:
                found_items, name, problems = parse_package_json(manifest.chunks)
                if name:
                    local_packages.add(name)
            refs = [chunk_ref(manifest.locator, row) for row in manifest.chunks]
            if not manifest.chunks or problems:
                link = (
                    self.evidence(
                        "inventory_artifact",
                        refs[:64],
                        {"problems": problems},
                        f"{manifest.locator} could not be fully read from stored chunks",
                    )
                    if refs
                    else None
                )
                self._diagnose(
                    "coverage",
                    "MANIFEST_UNPARSEABLE",
                    "unknown",
                    [workspace],
                    {"manifest": manifest.locator, "problems": problems or ["NOT_PROCESSED"]},
                    [link] if link else self.nodes[workspace].evidence,
                )
            if manifest.chunks and not problems:
                parsed += 1
            for item in found_items:
                declared[workspace][manifest.ecosystem].append((item, manifest.locator))
        imported: dict[tuple[str, str], dict[str, list[ArchitectureEdge]]] = defaultdict(
            lambda: defaultdict(list)
        )  # (workspace, ecosystem) -> package node -> edges
        for edge in self.module_edges:
            if edge.target_key and edge.target_key.startswith("external:"):
                _, ecosystem, _ = edge.target_key.split(":", 2)
                ecosystem = "npm" if ecosystem == "node" else ecosystem
                imported[(self._workspace(edge.source_key), ecosystem)][edge.target_key].append(
                    edge
                )
        statuses: Counter[str] = Counter()
        origins: dict[str, str] = {}
        matched: set[tuple[str, str, str]] = set()  # (workspace, ecosystem, declaration name)
        for (workspace, ecosystem), packages in sorted(imported.items()):
            chain = (
                self._workspace_chain(workspace)
                if ecosystem == "npm"
                else [
                    next(
                        (w for w in self._workspace_chain(workspace) if declared[w].get("python")),
                        workspace,
                    )
                ]
            )
            visible = [
                (w, item, loc) for w in chain for item, loc in declared[w].get(ecosystem, [])
            ]
            for key, edges in sorted(packages.items()):
                name = self.nodes[key].name
                if ecosystem == "python":
                    if name in PYTHON_STDLIB:
                        origin = "stdlib"
                    else:
                        found = [v for v in visible if pep503(v[1].name) == pep503(name)]
                        origin = "declared" if found else "not_matched_to_declaration"
                        matched.update((w, ecosystem, item.name) for w, item, _ in found)
                else:
                    package = name
                    if key.startswith("external:node:") or package in NODE_BUILTINS:
                        origin = "builtin"
                    elif package in local_packages:
                        origin = "workspace_package"
                    else:
                        found = [v for v in visible if v[1].name == package]
                        origin = "declared" if found else "imported_not_declared"
                        matched.update((w, ecosystem, item.name) for w, item, _ in found)
                        if not found:
                            links = {link for edge in edges for link in edge.evidence}
                            self._diagnose(
                                "integration",
                                "IMPORTED_NOT_DECLARED",
                                "derived",
                                [key],
                                {"workspace": workspace, "importers": len(edges)},
                                links,
                            )
                statuses[f"{ecosystem}:{origin}"] += 1
                previous = origins.get(key)
                origins[key] = origin if previous in {None, origin} else "mixed"
        for key, origin in origins.items():
            node = self.nodes[key]
            self.nodes[key] = replace(node, attributes={**node.attributes, "origin": origin})
        for workspace, ecosystems in sorted(declared.items()):
            for ecosystem, entries_all in sorted(ecosystems.items()):
                by_name: dict[str, list[tuple[Declaration, str]]] = defaultdict(list)
                for item, locator in entries_all:
                    by_name[item.name].append((item, locator))
                missing: list[str] = []
                for name, entries in sorted(by_name.items()):
                    key = (
                        next(
                            (
                                k
                                for k in self.nodes
                                if k.startswith("external:python:")
                                and pep503(self.nodes[k].name) == pep503(name)
                            ),
                            f"external:python:{pep503(name)}",
                        )
                        if ecosystem == "python"
                        else f"external:npm:{name}"
                    )
                    if key not in self.nodes and key not in self.new_nodes:
                        self._node(
                            key,
                            "external_package",
                            None,
                            name,
                            {
                                "ecosystem": ecosystem,
                                # Python import names may differ from distribution names, so
                                # an unmatched declaration is never "not imported".
                                "origin": "declared_not_matched"
                                if ecosystem == "python"
                                else "declared_only",
                            },
                            [
                                self.evidence(
                                    "derivation",
                                    (),
                                    {"algorithm": "manifest-facts.v1", "name": name},
                                    f"{name} is declared as a dependency ({ecosystem})",
                                )
                            ],
                        )
                    refs = [chunk_ref(loc, row) for item, loc in entries for row in item.refs]
                    link = self.evidence(
                        "inventory_artifact",
                        _unique(refs)[:64],
                        {
                            "algorithm": "manifest-facts.v1",
                            "sections": sorted({i.section for i, _ in entries}),
                        },
                        f"{workspace.removeprefix('workspace:')} declares {name} "
                        f"({', '.join(sorted({i.section for i, _ in entries}))})",
                    )
                    self._edge(
                        f"declares_dependency|{workspace}|{key}",
                        "declares_dependency",
                        workspace,
                        key,
                        "observed",
                        {
                            "sections": sorted({item.section for item, _ in entries}),
                            "kinds": sorted({item.kind for item, _ in entries}),
                            "manifests": sorted({loc for _, loc in entries}),
                        },
                        [link],
                    )
                    if (workspace, ecosystem, name) not in matched:
                        missing.append(name)
                if not missing:
                    continue
                statuses[f"{ecosystem}:declared_not_matched"] += len(missing)
                if ecosystem == "python":
                    self._diagnose(
                        "integration",
                        "DECLARED_NOT_MATCHED_BY_IMPORT_NAME",
                        "unknown",
                        [workspace],
                        {"names": missing[:50], "total": len(missing)},
                        self.nodes[workspace].evidence,
                    )
                    continue
                scope = [k for w in [workspace] for k in self._modules_under(w)]
                gaps = self._complete(scope)
                self._diagnose(
                    "integration",
                    "ABSENCE_NOT_DETERMINABLE" if gaps else "DECLARED_NOT_OBSERVED_IMPORTED",
                    "unknown" if gaps else "derived",
                    [workspace],
                    {
                        "names": missing[:50],
                        "total": len(missing),
                        "blocking_gaps": gaps,
                        "claim": "DECLARED_NOT_OBSERVED_IMPORTED",
                    },
                    self.nodes[workspace].evidence,
                )
        self.coverage["integrations"] = {
            "manifests": len(self.manifests),
            "manifests_parsed": parsed,
            "packages": dict(sorted(statuses.items())),
            "stdlib_list": "cpython-3.11+3.14.v1",
        }

    # ----------------------------------------------------------------- C2 database

    def _database(self) -> None:
        tables: dict[str, Row] = defaultdict(lambda: {"models": [], "migrations": []})
        links: dict[str, set[EvidenceLink]] = defaultdict(set)
        models: dict[tuple[str, str], str] = {}  # (module locator, class name) -> table key
        unknown_names = 0
        for item in self.tables:
            if item.recognizer != "sqlalchemy-declarative.v1":
                continue
            catalog = EvidenceLink("catalog", item.fact_digest)
            if item.table is None:
                unknown_names += 1
                key = f"db_table:?{item.locator}#{item.model}"
            else:
                key = f"db_table:{item.table}"
            tables[key]["models"].append({"locator": item.locator, "model": item.model})
            links[key].add(catalog)
            if item.model:
                models[(item.locator, item.model.rsplit(".", 1)[-1])] = key
            self._edge(
                f"maps_table|module:{item.locator}|{key}|{item.model}",
                "maps_table",
                f"module:{item.locator}",
                key,
                "inferred",
                {"model": item.model, "recognizer": item.recognizer},
                [catalog],
            )
            for value in item.assignments:
                for referenced in _FOREIGN_KEY.findall(value):
                    target = f"db_table:{referenced}"
                    tables[target]  # noqa: B018 - make sure the referenced table node exists
                    links[target].add(catalog)
                    self._edge(
                        f"references_table|{key}|{target}|{item.model}",
                        "references_table",
                        key,
                        target,
                        "inferred",
                        {"model": item.model, "pattern": "ForeignKey"},
                        [catalog],
                        ("FOREIGN_KEY_FROM_TEXT_PATTERN",),
                    )
        for module in self.modules.values():
            for row in module.literals if module.usable else ():
                if row["kind"] != "migration_table":
                    continue
                key = f"db_table:{row['subject']}"
                link = self.evidence(
                    "literal_rows",
                    [literal_ref(module.locator, row)],
                    {"algorithm": "database-view.v1", "operation": "create_table"},
                    f"{module.locator}:{row['start_line']} creates table {row['subject']}",
                )
                tables[key]["migrations"].append(
                    {"locator": module.locator, "line": row["start_line"]}
                )
                links[key].add(link)
                self._edge(
                    f"migrates_table|module:{module.locator}|{key}|{row['start_line']}",
                    "migrates_table",
                    f"module:{module.locator}",
                    key,
                    "observed",
                    {
                        "operation": "create_table",
                        "line": row["start_line"],
                        "migration_location": "/versions/" in f"/{module.locator}",
                    },
                    [link],
                )
        uses = 0
        for module in self.modules.values():
            for local, binding in sorted(module.bindings.items()):
                table = models.get((binding.target, str(binding.name)))
                if table is None:
                    continue
                uses += 1
                link = self.evidence(
                    "import_statements",
                    [binding.ref],
                    {"algorithm": "database-view.v1", "model": binding.name},
                    f"{module.locator} imports model {binding.name} from {binding.target}",
                )
                self._edge(
                    f"uses_model|module:{module.locator}|{table}|{local}",
                    "uses_model",
                    f"module:{module.locator}",
                    table,
                    "derived",
                    {"model": binding.name, "local": local},
                    [link],
                )
        python_modules = [
            k
            for k in self.nodes
            if k.startswith("module:")
            and (self.inputs.get(k.removeprefix("module:")) is not None)
            and self.inputs[k.removeprefix("module:")].language in {None, "Python"}
        ]
        gaps = self._complete(python_modules)
        for key, entry in sorted(tables.items()):
            name = key.removeprefix("db_table:")
            sources = [s for s in ("models", "migrations") if entry[s]]
            tier = "inferred" if len(sources) == 2 else "derived" if sources else "inferred"
            self._node(
                key,
                "db_table",
                None,
                name,
                {
                    "models": entry["models"],
                    "migrations": entry["migrations"],
                    "merged_by_name": len(sources) == 2,
                    "name_known": not name.startswith("?"),
                    "tier": tier,
                },
                links[key]
                or {
                    self.evidence(
                        "derivation", (), {"table": name}, f"{name} is referenced by a foreign key"
                    )
                },
            )
            if not sources or name.startswith("?"):
                continue
            missing = (
                "NO_OBSERVED_MIGRATION"
                if not entry["migrations"]
                else ("NO_OBSERVED_MODEL" if not entry["models"] else None)
            )
            if missing:
                self._diagnose(
                    "database",
                    "ABSENCE_NOT_DETERMINABLE" if gaps else missing,
                    "unknown" if gaps else "derived",
                    [key],
                    {"claim": missing, "blocking_gaps": gaps},
                    links[key],
                )
        self.coverage["database"] = {
            "tables": len(tables),
            "models": sum(1 for t in self.tables if t.recognizer == "sqlalchemy-declarative.v1"),
            "model_tables_without_literal_name": unknown_names,
            "migration_tables": sum(len(t["migrations"]) for t in tables.values()),
            "uses_model": uses,
            "absence_gaps": gaps,
            "not_covered": ["raw SQL", "sa.Table(...) metadata", "live database introspection"],
        }

    # ----------------------------------------------------------------- C3 frontend routes

    def _frontend(self) -> None:
        total = bound = 0
        for module in self.modules.values():
            rows = [
                row
                for row in (module.literals if module.usable else ())
                if row["kind"] == "frontend_route"
            ]
            by_ordinal = {int(row["ordinal"]): row for row in rows}
            for row in rows:
                total += 1
                attributes = row["attributes"]
                segments: list[str] = []
                known = True
                current: Row | None = row
                chain: list[Row] = []
                while current is not None and len(chain) < 16:
                    chain.append(current)
                    path = current["attributes"].get("path") or {}
                    if path.get("state") == "literal":
                        segments.append(str(path["value"]))
                    elif path.get("state") != "absent":
                        known = False
                    parent = current["attributes"].get("parent")
                    current = by_ordinal.get(int(parent)) if parent is not None else None
                full: str | None = None
                if known:
                    full = ""
                    for segment in reversed(segments):
                        full = (
                            segment
                            if segment.startswith("/")
                            else (full.rstrip("/") + "/" + segment if segment else full)
                        )
                    full = full or "/"
                key = f"route:{module.locator}:{row['ordinal']}"
                link = self.evidence(
                    "literal_rows",
                    [literal_ref(module.locator, item) for item in chain],
                    {"algorithm": "frontend-routes.v1", "full_path": full},
                    f"{module.locator}:{row['start_line']} routes {full or 'an unknown path'} "
                    f"to {row['subject'] or 'an unknown element'}",
                )
                self._node(
                    key,
                    "frontend_route",
                    f"module:{module.locator}",
                    full or "?",
                    {
                        "path": attributes.get("path"),
                        "full_path": full,
                        "index": attributes.get("index"),
                        "element": row["subject"],
                        "line": row["start_line"],
                        "parent": f"route:{module.locator}:{attributes['parent']}"
                        if attributes.get("parent") is not None
                        else None,
                    },
                    [link],
                    locator=module.locator,
                    language=module.language,
                )
                element = row["subject"]
                target: str | None = None
                refs: list[Row] = []
                if element:
                    head = str(element).split(".")[0]
                    binding = module.bindings.get(head)
                    if binding is not None and not binding.target.startswith("external:"):
                        target, refs = f"module:{binding.target}", [binding.ref]
                    elif head in module.top_level_names:
                        target = f"module:{module.locator}"
                if target is not None and target in self.nodes:
                    bound += 1
                    links = [link]
                    if refs:
                        links.append(
                            self.evidence(
                                "import_statements",
                                refs,
                                {"algorithm": "frontend-routes.v1"},
                                f"{module.locator} imports {element} from "
                                f"{target.removeprefix('module:')}",
                            )
                        )
                    self._edge(
                        f"routes_to|{key}|{target}",
                        "routes_to",
                        key,
                        target,
                        "derived",
                        {"element": element},
                        links,
                    )
        self.coverage["frontend_routes"] = {
            "routes": total,
            "bound_to_modules": bound,
            "not_covered": ["object route configs (createBrowserRouter)", "lazy() elements"],
        }

    # ----------------------------------------------------------------- C4 layer candidates

    def _package(self, key: str) -> bool:
        node = self.nodes.get(key)
        return node is not None and bool(node.attributes.get("python_package"))

    def _layer_scopes(self) -> list[tuple[str, bool]]:
        """(scope, python): from each Python source root follow the single-package chain;
        from each npm workspace follow the single-container chain without direct files."""
        starts: list[tuple[str, bool]] = [
            (f"container:{root}" if root else "workspace:.", True) for root in self.roots
        ]
        for key, node in self.nodes.items():
            if node.kind == "workspace" and "npm" in node.attributes.get("ecosystems", []):
                starts.append((key, False))
        scopes: list[tuple[str, bool]] = []
        for start, python in sorted(set(starts)):
            key = start if start in self.nodes else start.replace("container:", "workspace:", 1)
            if key not in self.nodes:
                continue
            for _ in range(8):
                children = self.children.get(key, [])
                containers = [
                    c
                    for c in children
                    if c.startswith("container:")
                    and self._modules_under(c)
                    and (not python or self._package(c))
                ]
                files = [
                    c
                    for c in children
                    if c.startswith("module:") and not c.endswith("/__init__.py")
                ]
                if len(containers) == 1 and (python or not files):
                    key = containers[0]
                    continue
                break
            if (key, python) not in scopes:
                scopes.append((key, python))
        return scopes

    def _layers(self) -> None:
        produced = 0
        for scope, python in self._layer_scopes():
            members = [
                c
                for c in self.children.get(scope, [])
                if c.startswith("container:")
                and self._modules_under(c)
                and (not python or self._package(c))
            ]
            if len(members) < 2:
                continue
            owner = {m: c for c in members for m in self._modules_under(c)}
            deps: dict[str, Counter[str]] = defaultdict(Counter)
            witnesses: dict[tuple[str, str], list[str]] = defaultdict(list)
            for edge in self.module_edges:
                if edge.import_class not in RUNTIME_CLASSES or edge.target_key is None:
                    continue
                a, b = owner.get(edge.source_key), owner.get(edge.target_key)
                if a and b and a != b:
                    deps[a][b] += edge.count
                    witnesses[(a, b)].append(edge.key)
            ranks, cyclic = _longest_path_ranks(members, {a: set(v) for a, v in deps.items()})
            convention = {
                m: CONVENTIONS.get(posixpath.basename(m.split(":", 1)[1])) for m in members
            }
            inversions: list[dict[str, Any]] = []
            for (a, b), keys in sorted(witnesses.items()):
                ca, cb = convention[a], convention[b]
                if ca and cb and ca[1] < cb[1]:
                    inversions.append(  # conventional rank of the source is lower
                        {
                            "from": a,
                            "to": b,
                            "edges": sorted(keys)[:10],
                            "count": deps[a][b],
                            "from_convention": ca[0],
                            "to_convention": cb[0],
                        }
                    )
            key = f"layer_candidate:{scope}"
            link = self.evidence(
                "derivation",
                (),
                {
                    "algorithm": "layer-candidates.v1",
                    "scope": scope,
                    "ranks": ranks,
                    "cyclic": cyclic,
                    "classes": "S2",
                },
                f"Observed dependency direction among {len(members)} containers of "
                f"{scope.split(':', 1)[1]} (candidate layering, not intended architecture)",
            )
            order: dict[int, list[str]] = defaultdict(list)
            for member, rank in ranks.items():
                order[rank].append(member)
            self._node(
                key,
                "layer_candidate",
                None,
                f"layers of {scope.split(':', 1)[1]}",
                {
                    "scope": scope,
                    "order": [sorted(order[r]) for r in sorted(order, reverse=True)],
                    "ranks": ranks,
                    "cyclic_groups": cyclic,
                    "conventions": {m: c[0] for m, c in convention.items() if c},
                    "inversions": inversions,
                    "class_set": "S2",
                    "banner": "observed dependency direction, never intended architecture",
                },
                [link],
            )
            produced += 1
            for member in members:
                self._edge(
                    f"member_of|{member}|{key}",
                    "member_of",
                    member,
                    key,
                    "inferred",
                    {"rank": ranks[member], "signals": ["observed_dependency_direction"]},
                    [link],
                )
            for item in inversions:
                links = [link, *(x for k in item["edges"] for x in self._edge_links(k))]
                self._diagnose(
                    "direction",
                    "LAYER_CANDIDATE_INVERSION",
                    "inferred",
                    [item["from"], item["to"]],
                    {
                        "scope": scope,
                        "count": item["count"],
                        "edges": item["edges"],
                        "convention": [item["from_convention"], item["to_convention"]],
                    },
                    links,
                )
        self.coverage["layers"] = {"scopes": produced, "algorithm": "layer-candidates.v1"}

    def _edge_links(self, key: str) -> tuple[EvidenceLink, ...]:
        edge = next((e for e in self.module_edges if e.key == key), None)
        return edge.evidence if edge else ()

    # ----------------------------------------------------------------- C4 functional slices

    def _slices(self) -> None:
        stems: dict[str, str] = {}
        seeds: dict[str, set[str]] = defaultdict(set)  # seed module -> kinds
        for node in self.api_nodes.values():
            if node.kind == "router" and not node.attributes.get("is_app") and node.locator:
                seeds[f"module:{node.locator}"].add("backend_router")
            if (
                node.kind == "client_call"
                and node.locator
                and any(
                    b.get("outcome") == "conditional_candidate"
                    for b in node.attributes.get("branches", [])
                )
            ):
                seeds[f"module:{node.locator}"].add("api_client")
        for edge in self.edges:
            if edge.kind == "routes_to" and edge.target_key:
                seeds[edge.target_key].add("frontend_route")
        if not seeds:
            self.coverage["slices"] = {"slices": 0, "reason": "NO_SEEDS"}
            return
        parent = {seed: seed for seed in seeds}

        def find(item: str) -> str:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(a: str, b: str, why: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
            reasons[min(ra, rb)].add(why)

        reasons: dict[str, set[str]] = defaultdict(set)
        for seed in seeds:
            stems[seed] = stem(seed.removeprefix("module:"))
        by_stem: dict[str, list[str]] = defaultdict(list)
        for seed, value in stems.items():
            if value:
                by_stem[value].append(seed)
        for group in by_stem.values():
            for other in group[1:]:
                union(group[0], other, "NAME_STEM")
        for edge in self.api_edges:
            if edge.kind != "calls_endpoint" or not edge.target_key:
                continue
            client = self.api_nodes.get(edge.source_key)
            endpoint = self.api_nodes.get(edge.target_key)
            router = endpoint.attributes.get("router") if endpoint else None
            router_node = self.api_nodes.get(str(router)) if router else None
            if client and router_node and client.locator and router_node.locator:
                a, b = f"module:{client.locator}", f"module:{router_node.locator}"
                if a in parent and b in parent:
                    union(a, b, "API_CANDIDATE")
        groups: dict[str, list[str]] = defaultdict(list)
        for seed in sorted(seeds):
            groups[find(seed)].append(seed)
        adjacency: dict[str, set[str]] = defaultdict(set)
        for edge in self.module_edges:
            if (
                edge.import_class in RUNTIME_CLASSES
                and edge.target_key
                and edge.target_key.startswith("module:")
            ):
                adjacency[edge.source_key].add(edge.target_key)
        reached: dict[str, set[str]] = defaultdict(set)
        for root, members in groups.items():
            stack, seen = list(members), set(members)
            while stack:
                current = stack.pop()
                for target in adjacency.get(current, ()):
                    if target not in seen:
                        seen.add(target)
                        stack.append(target)
            for module in seen:
                reached[module].add(root)
        modules = [k for k in self.nodes if k.startswith("module:")]
        assigned = shared = 0
        names = {root: (stems.get(root) or root.removeprefix("module:")) for root in groups}
        for root, members in sorted(groups.items()):
            key = f"functional_slice:{names[root]}:{digest(sorted(members))[:8]}"
            exclusive = sorted(m for m in modules if reached.get(m) == {root})
            common = sorted(
                m for m in modules if root in reached.get(m, set()) and len(reached[m]) > 1
            )
            link = self.evidence(
                "derivation",
                (),
                {
                    "algorithm": "functional-slices.v1",
                    "seeds": members,
                    "grouping": sorted(reasons.get(root, set())),
                    "members": digest(exclusive),
                    "classes": "S2",
                },
                f"Candidate functional slice '{names[root]}': {len(members)} seed module(s), "
                f"{len(exclusive)} exclusively reachable, {len(common)} shared "
                "(imports, names and API links; no call graph)",
            )
            self._node(
                key,
                "functional_slice",
                None,
                names[root],
                {
                    "seeds": [{"module": m, "kinds": sorted(seeds[m])} for m in members],
                    "grouping": sorted(reasons.get(root, set())),
                    "exclusive": len(exclusive),
                    "shared": len(common),
                    "shared_modules": common[:200],
                    "banner": "candidate functional slice from imports, names and API links; "
                    "no call graph; not a complete subsystem model",
                },
                [link],
            )
            for module in exclusive:
                signals = ["exclusive_reachability"]
                if stem(module.removeprefix("module:")) == stems.get(root):
                    signals.append("name_stem")
                if module in members:
                    signals.append("seed")
                self._edge(
                    f"member_of|{module}|{key}",
                    "member_of",
                    module,
                    key,
                    "inferred",
                    {"signals": signals},
                    [link],
                )
        assigned = sum(1 for m in modules if len(reached.get(m, set())) == 1)
        shared = sum(1 for m in modules if len(reached.get(m, set())) > 1)
        self.coverage["slices"] = {
            "slices": len(groups),
            "modules": len(modules),
            "assigned_exclusively": assigned,
            "shared": shared,
            "unreached": len(modules) - assigned - shared,
            "seeds": len(seeds),
            "not_covered": ["CLI sub-apps (no literal kind)", "call graph"],
        }


def stem(locator: str) -> str:
    """Name-stem affinity token: file name tokens minus generic words (pattern only)."""
    base = posixpath.splitext(posixpath.basename(locator))[0]
    if base == "__init__":
        base = posixpath.basename(posixpath.dirname(locator))
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", base).lower().split("_")
    kept = [word for word in words if word and word not in _GENERIC_STEMS]
    return "_".join(kept)


def _longest_path_ranks(
    members: Sequence[str], deps: Mapping[str, set[str]]
) -> tuple[dict[str, int], list[list[str]]]:
    """Longest-path rank on the condensation (sinks rank 0); SCCs share one rank."""
    from buildlens.domain.architecture_graph import Graph, GraphEdge, strongly_connected_components

    edges = [GraphEdge(f"{a}|{b}", a, b, None) for a, targets in deps.items() for b in targets]
    graph = Graph(edges, nodes=tuple(members))
    components = strongly_connected_components(graph)
    component_of = {node: index for index, group in enumerate(components) for node in group}
    successors: dict[int, set[int]] = defaultdict(set)
    for a, targets in deps.items():
        for b in targets:
            if component_of[a] != component_of[b]:
                successors[component_of[a]].add(component_of[b])
    rank: dict[int, int] = {}

    def visit(index: int) -> int:
        stack = [(index, iter(sorted(successors.get(index, ()))))]
        while stack:
            current, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                rank[current] = 1 + max((rank[c] for c in successors.get(current, ())), default=-1)
            elif child not in rank:
                stack.append((child, iter(sorted(successors.get(child, ())))))
        return rank[index]

    for index in range(len(components)):
        if index not in rank:
            visit(index)
    cyclic = [sorted(group) for group in components if len(group) > 1]
    return {node: rank[component_of[node]] for node in members}, sorted(cyclic)


def _unique(rows: Iterable[Row]) -> list[Row]:
    seen: dict[str, Row] = {}
    for row in rows:
        seen.setdefault(digest(row), row)
    return list(seen.values())
