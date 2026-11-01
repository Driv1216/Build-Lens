"""Stage 15 Milestone B: TS/TSX/JS call resolution, module resolution and framework semantics.

Module resolution: Stage 13 relative specifiers (``import-resolution.v1``), then
``ts-path-aliases.v1`` (tsconfig ``baseUrl``/``paths`` with an ``extends`` chain ≤ 3, read as
data) and ``ts-workspace-packages.v1`` (in-repository ``package.json`` names, dist → source
only through literal tsconfig ``outDir``/``rootDir``). React, TanStack Query, react-hook-form
and request-descriptor semantics follow the source-verified contracts recorded for Milestone B.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from buildlens.application.flow_inputs import (
    Emitter,
    FlowInput,
    ImportInput,
    ScopeView,
    call_key,
    jsx_key,
    scope_key,
)
from buildlens.application.semantic_catalog import ImportResolver
from buildlens.domain.flow import FANOUT, HTTP_CAVEAT, RULES
from buildlens.domain.flow_evidence import FlowCallsite, ModuleFact

_EXTENSIONS = (".ts", ".tsx", ".d.ts", ".js", ".jsx")
_GLOBALS = {
    "console",
    "window",
    "document",
    "fetch",
    "setTimeout",
    "setInterval",
    "clearTimeout",
    "clearInterval",
    "Promise",
    "JSON",
    "Object",
    "Array",
    "Math",
    "Date",
    "Number",
    "String",
    "Boolean",
    "Error",
    "Map",
    "Set",
    "URL",
    "URLSearchParams",
    "encodeURIComponent",
    "decodeURIComponent",
    "parseInt",
    "parseFloat",
    "isNaN",
    "structuredClone",
    "queueMicrotask",
    "requestAnimationFrame",
    "AbortController",
    "localStorage",
    "sessionStorage",
    "globalThis",
    "navigator",
    "location",
    "history",
    "alert",
    "confirm",
    "Symbol",
    "Reflect",
    "RegExp",
    "TextEncoder",
    "TextDecoder",
    "Intl",
    "BigInt",
    "crypto",
    "performance",
    "require",
}
_CLIENT_PACKAGES = {"openapi-fetch", "@hey-api/client-axios", "@hey-api/client-fetch"}
_VERBS_UPPER = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_VERBS_LOWER = {v.lower() for v in _VERBS_UPPER}


@dataclass(frozen=True, slots=True)
class TsTarget:
    kind: str  # scope | class | instance | module | external | global | special | prop | param | unknown  # noqa: E501
    locator: str | None = None
    scope: str | None = None
    name: str | None = None
    tier: str = "derived"
    conditions: tuple[str, ...] = ()
    data: tuple[tuple[str, Any], ...] = ()

    def get(self, key: str) -> Any:
        return dict(self.data).get(key)

    def weaker(self, tier: str, *conditions: str) -> TsTarget:
        order = ("observed", "derived", "inferred", "unknown")
        new = tier if order.index(tier) > order.index(self.tier) else self.tier
        return TsTarget(
            self.kind,
            self.locator,
            self.scope,
            self.name,
            new,
            tuple(sorted(set(self.conditions) | set(conditions))),
            self.data,
        )


def _unknown(code: str) -> TsTarget:
    return TsTarget("unknown", name=code, tier="unknown", conditions=(code,))


def _special(name: str, tier: str = "derived", *conditions: str, **data: Any) -> TsTarget:
    return TsTarget(
        "special",
        name=name,
        tier=tier,
        conditions=tuple(conditions),
        data=tuple(sorted(data.items())),
    )


@dataclass(slots=True)
class _Modules:
    """Module resolution facts: tsconfig ``paths`` per directory and workspace packages."""

    resolver: ImportResolver
    locators: frozenset[str]
    tsconfigs: dict[str, ModuleFact] = field(default_factory=dict)
    packages: dict[str, ModuleFact] = field(default_factory=dict)  # package name -> fact

    def resolve(
        self, locator: str, specifier: str | None
    ) -> tuple[str | None, str, tuple[str, ...]]:
        """(target locator, status, conditions) for an import specifier."""
        base = self.resolver.specifier(locator, specifier)
        if base.status == "resolved":
            return base.locator, "resolved", ()
        if base.status == "not_found" or not specifier:
            return None, base.status, ()
        aliased = self._alias(locator, specifier)
        if aliased is not None:
            return aliased
        workspace = self._workspace(specifier)
        if workspace is not None:
            return workspace
        return None, base.status, ("ALIAS_NOT_RESOLVED",) if base.status == "not_attempted" else ()

    def _probe(self, path: str) -> str | None:
        path = posixpath.normpath(path)
        candidates = [path, *(path + ext for ext in _EXTENSIONS)]
        stem, extension = posixpath.splitext(path)
        if extension in {".js", ".jsx", ".mjs", ".cjs"}:
            candidates += [stem + ".ts", stem + ".tsx"]
        candidates += [posixpath.join(path, "index" + ext) for ext in _EXTENSIONS]
        return next((c for c in candidates if c in self.locators), None)

    def _config(
        self, fact: ModuleFact, depth: int = 0
    ) -> tuple[str | None, dict[str, list[str]] | None, str]:
        """(baseUrl directory, paths, the directory paths are relative to) after ``extends``."""
        directory = posixpath.dirname(fact.locator)
        base_url, paths, paths_dir = None, None, directory
        extends = fact.facts.get("extends")
        if depth < 3 and isinstance(extends, str):
            parent = self._extends(directory, extends)
            if parent is not None:
                base_url, paths, paths_dir = self._config(parent, depth + 1)
        if fact.facts.get("baseUrl") is not None:
            base_url = posixpath.normpath(posixpath.join(directory, fact.facts["baseUrl"]))
        if fact.facts.get("paths") is not None:
            paths, paths_dir = fact.facts["paths"], directory
        return base_url, paths, paths_dir

    def _extends(self, directory: str, extends: str) -> ModuleFact | None:
        if extends.startswith("."):
            target = posixpath.normpath(posixpath.join(directory, extends))
            for candidate in (target, target + ".json"):
                if candidate in self.tsconfigs:
                    return self.tsconfigs[candidate]
            return None
        for name, package in self.packages.items():  # a workspace package's tsconfig file
            if extends.startswith(name + "/"):
                target = posixpath.join(
                    posixpath.dirname(package.locator), extends[len(name) + 1 :]
                )
                for candidate in (target, target + ".json"):
                    if candidate in self.tsconfigs:
                        return self.tsconfigs[candidate]
        return None

    def _nearest(self, locator: str) -> ModuleFact | None:
        directory = posixpath.dirname(locator)
        while True:
            for name in ("tsconfig.json", "tsconfig.app.json", "jsconfig.json"):
                key = posixpath.join(directory, name) if directory else name
                if key in self.tsconfigs:
                    return self.tsconfigs[key]
            if not directory:
                return None
            directory = posixpath.dirname(directory)

    def _alias(
        self, locator: str, specifier: str
    ) -> tuple[str | None, str, tuple[str, ...]] | None:
        fact = self._nearest(locator)
        if fact is None:
            return None
        base_url, paths, paths_dir = self._config(fact)
        root = base_url or paths_dir
        for pattern, targets in sorted((paths or {}).items(), key=lambda item: -len(item[0])):
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                if not specifier.startswith(prefix):
                    continue
                rest = specifier[len(prefix) :]
            elif pattern == specifier:
                rest = ""
            else:
                continue
            for target in targets:
                found = self._probe(posixpath.join(root, target.replace("*", rest)))
                if found:
                    return found, "resolved", ("TSCONFIG_PATHS_TRUSTED",)
            return None, "not_found", ("TSCONFIG_PATHS_TRUSTED",)
        if base_url and not specifier.startswith((".", "/")):
            found = self._probe(posixpath.join(base_url, specifier))
            if found:
                return found, "resolved", ("TSCONFIG_BASEURL_TRUSTED",)
        return None

    def _workspace(self, specifier: str) -> tuple[str | None, str, tuple[str, ...]] | None:
        for name, fact in sorted(self.packages.items(), key=lambda item: -len(item[0])):
            if specifier != name and not specifier.startswith(name + "/"):
                continue
            directory = posixpath.dirname(fact.locator)
            subpath = specifier[len(name) :].lstrip("/")
            targets = _export_targets(fact.facts, subpath)
            config = self.tsconfigs.get(posixpath.join(directory, "tsconfig.json"))
            for target in targets:
                path = posixpath.normpath(posixpath.join(directory, target))
                found = (
                    self._probe(path) if path in self.locators or not path.endswith(".js") else None
                )
                if found:
                    return found, "resolved", ("WORKSPACE_PACKAGE_SOURCE",)
                if config is not None:
                    out = config.facts.get("outDir")
                    src = config.facts.get("rootDir")
                    if out and src:
                        out_dir = posixpath.normpath(posixpath.join(directory, out))
                        src_dir = posixpath.normpath(posixpath.join(directory, src))
                        if path.startswith(out_dir + "/"):
                            mapped = src_dir + path[len(out_dir) :]
                            stem = posixpath.splitext(mapped)[0]
                            if stem.endswith(".d"):
                                stem = stem[:-2]
                            found = self._probe(stem)
                            if found:
                                return found, "resolved", ("WORKSPACE_DIST_TO_SOURCE_MAPPED",)
            if subpath:
                found = self._probe(posixpath.join(directory, subpath))
                if found:
                    return found, "resolved", ("WORKSPACE_PACKAGE_SOURCE",)
            return None, "not_found", ("WORKSPACE_PACKAGE_NOT_RESOLVED",)
        return None


def _export_targets(facts: dict[str, Any], subpath: str) -> list[str]:
    exports = facts.get("exports")
    key = "." if not subpath else "./" + subpath
    values: list[str] = []
    entry: Any = None
    if isinstance(exports, str) and key == ".":
        entry = exports
    elif isinstance(exports, dict):
        entry = (
            exports.get(key)
            if any(k.startswith(".") for k in exports)
            else (exports if key == "." else None)
        )
    for value in _conditional(entry):
        values.append(value)
    if key == ".":
        values += [facts[k] for k in ("types", "module", "main") if isinstance(facts.get(k), str)]
    return values


def _conditional(entry: Any) -> list[str]:
    """``exports`` conditions in priority import > default > types > require (D13)."""
    if isinstance(entry, str):
        return [entry]
    if isinstance(entry, dict):
        values: list[str] = []
        for condition in ("import", "default", "types", "require", "node", "browser"):
            values += _conditional(entry.get(condition))
        return values
    return []


class TypeScriptFlow:
    def __init__(self, inp: FlowInput, em: Emitter, facts: Any) -> None:
        self.inp, self.em, self.facts = inp, em, facts
        self.views: dict[tuple[str, str], ScopeView] = {}
        for sc in inp.scopes:
            if sc.language in {"TypeScript", "JavaScript"}:
                self.views[(sc.locator, sc.scope.key)] = ScopeView.of(sc)
        self.imports: dict[str, dict[str, ImportInput]] = {}
        for row in inp.imports:
            if row.locator.endswith((".ts", ".tsx", ".js", ".jsx")) and row.form in {
                "es_import",
                "es_import_type",
                "require",
            }:
                self.imports.setdefault(row.locator, {})[row.local_name] = row
        self.exports: dict[str, list[Any]] = {}
        for export in inp.exports:
            self.exports.setdefault(export.locator, []).append(export)
        self.symbols = {s.id: s for s in inp.symbols}
        self.by_owner: dict[UUID, ScopeView] = {
            v.input.scope.owner_symbol_id: v
            for v in self.views.values()
            if v.input.scope.owner_symbol_id is not None
        }
        self.modules = _Modules(ImportResolver(inp.locators), inp.locators)
        for fact in inp.module_facts:
            if fact.kind == "tsconfig":
                self.modules.tsconfigs[fact.locator] = fact
            elif isinstance(fact.facts.get("name"), str):
                self.modules.packages[fact.facts["name"]] = fact
        self.prop_bindings: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
        # Effects per component (effect scope, deps or None, evidence) and state-setter calls
        # (callsite, component, state name, evidence), joined after all callsites.
        self.effects: dict[str, list[tuple[str, list[str] | None, str]]] = {}
        self.setters: list[tuple[str, str, str, str]] = []
        self.class_symbols: dict[tuple[str, str], list[Any]] = {}
        for symbol in inp.symbols:
            if symbol.kind == "class":
                self.class_symbols.setdefault((symbol.locator, symbol.name), []).append(symbol)

    # ----------------------------------------------------------------- lookup

    def parent(self, view: ScopeView) -> ScopeView | None:
        key = view.input.scope.parent_key
        return self.views.get((view.input.locator, key)) if key else None

    def lookup(
        self, view: ScopeView, name: str, node: int | None, depth: int = 0
    ) -> list[TsTarget]:
        if depth > 6:
            return [_unknown("RESOLUTION_DEPTH")]
        if name in view.by_name:
            bindings = view.reaching_at(name, node) if node is not None else view.by_name[name]
            if not bindings:
                bindings = view.by_name[name]  # hoisted function declarations and closures
            return self._merge(
                [t for b in bindings for t in self.interpret(view, b, depth + 1)], len(bindings)
            )
        cursor = self.parent(view)
        while cursor is not None:
            if name in cursor.by_name:
                found = [
                    t for b in cursor.by_name[name] for t in self.interpret(cursor, b, depth + 1)
                ]
                merged = self._merge(found, len(cursor.by_name[name]))
                if (
                    len(cursor.by_name[name]) > 1
                    and cursor.input.scope.kind != "module"
                    and len(merged) > 1
                ):
                    return [t.weaker("unknown", "CLOSURE_REBINDING") for t in merged]
                return merged
            cursor = self.parent(cursor)
        row = self.imports.get(view.input.locator, {}).get(name)
        if row is not None:
            return [t for t in self.import_target(row, depth + 1)]
        if name in _GLOBALS:
            return [TsTarget("global", name=name, tier="observed")]
        return [_unknown("NAME_UNRESOLVED")]

    @staticmethod
    def _merge(targets: list[TsTarget], definitions: int) -> list[TsTarget]:
        unique: dict[tuple[Any, ...], TsTarget] = {}
        for t in targets:
            unique.setdefault((t.kind, t.locator, t.scope, t.name, t.data), t)
        values = list(unique.values())
        if len(values) > 1:
            values = [t.weaker("inferred", "MULTIPLE_REACHING_DEFINITIONS") for t in values]
        return values[: FANOUT + 1]

    def import_target(self, row: ImportInput, depth: int) -> list[TsTarget]:
        specifier = row.from_module
        target, status, conditions = self.modules.resolve(row.locator, specifier)
        evidence = (("import", row.row_digest),)
        if target is None:
            if status == "external":
                qual = f"{specifier}.{row.name}" if row.name not in {"*", ""} else str(specifier)
                return [
                    TsTarget(
                        "external",
                        name=qual,
                        tier="derived",
                        conditions=(f"EXTERNAL_PACKAGE:{_package(specifier or '')}",),
                        data=evidence,
                    )
                ]
            return [_unknown(conditions[0] if conditions else f"IMPORT_{status.upper()}")]
        tier = (
            "inferred"
            if any(c in {"WORKSPACE_DIST_TO_SOURCE_MAPPED"} for c in conditions)
            else "derived"
        )
        if row.name == "*":
            return [TsTarget("module", target, tier=tier, conditions=conditions, data=evidence)]
        return [t.weaker(tier, *conditions) for t in self.exported(target, row.name, depth + 1)]

    def exported(self, locator: str, name: str, depth: int) -> list[TsTarget]:
        if depth > 6:
            return [_unknown("RESOLUTION_DEPTH")]
        for symbol in self.inp.symbols:
            if symbol.locator != locator or symbol.parent_id is not None:
                continue
            if (name == "default" and symbol.exported == "default") or (
                symbol.exported == "named" and symbol.name == name
            ):
                view = self.by_owner.get(symbol.id)
                if symbol.kind == "class":
                    module_view = self.views.get((locator, "m"))
                    for binding in module_view.by_name.get(symbol.name, []) if module_view else []:
                        if binding["kind"] == "class" and module_view is not None:
                            return self.interpret(module_view, binding, depth + 1)
                    return [
                        TsTarget(
                            "class", locator, name=symbol.name, data=(("symbol", str(symbol.id)),)
                        )
                    ]
                if view is not None:
                    return [TsTarget("scope", locator, view.input.scope.key)]
        module = self.views.get((locator, "m"))
        for export in self.exports.get(locator, []):
            if export.kind in {"named", "default", "commonjs"} and (
                export.name == name or (name == "default" and export.kind == "default")
            ):
                local = export.local_name or export.name
                if export.source_module:
                    target, _, conditions = self.modules.resolve(locator, export.source_module)
                    if target:
                        return [
                            t.weaker("derived", *conditions)
                            for t in self.exported(target, local or name, depth + 1)
                        ]
                if module is not None and local and local in module.by_name:
                    return self.lookup(module, local, None, depth + 1)
                row = self.imports.get(locator, {}).get(local or "")
                if row is not None:
                    return self.import_target(row, depth + 1)
            if export.kind == "reexport" and export.name == name and export.source_module:
                target, _, conditions = self.modules.resolve(locator, export.source_module)
                if target:
                    return [
                        t.weaker("derived", *conditions)
                        for t in self.exported(target, export.local_name or name, depth + 1)
                    ]
        for export in self.exports.get(locator, []):
            if export.kind == "reexport_all" and export.source_module:
                target, _, conditions = self.modules.resolve(locator, export.source_module)
                if target:
                    found = [
                        t for t in self.exported(target, name, depth + 1) if t.kind != "unknown"
                    ]
                    if found:
                        return [t.weaker("derived", *conditions) for t in found]
        if module is not None and name in module.by_name:
            return self.lookup(module, name, None, depth + 1)
        return [_unknown("NAME_NOT_EXPORTED")]

    def interpret(self, view: ScopeView, binding: dict[str, Any], depth: int) -> list[TsTarget]:
        kind, value = binding["kind"], binding.get("value") or {}
        locator = view.input.locator
        if kind == "def":
            key = value.get("key")
            return [TsTarget("scope", locator, key)] if key else [_unknown("SCOPE_NOT_PLANNED")]
        if kind == "class":
            symbols = self.class_symbols.get((locator, binding["name"]), [])
            data: list[tuple[str, Any]] = [
                ("defining", view.input.scope.key),
                ("fields", int(value.get("instance_fields") or 0)),
            ]
            if len(symbols) == 1:
                data.append(("symbol", str(symbols[0].id)))
            if value.get("extends"):
                data.append(("extends", _freeze(value["extends"])))
            return [TsTarget("class", locator, name=binding["name"], data=tuple(data))]
        if kind == "param":
            if value.get("k") == "property" and (value.get("from") or {}).get("k") == "param":
                return [
                    TsTarget(
                        "prop",
                        locator,
                        view.input.scope.key,
                        name=value.get("key"),
                        tier="inferred",
                    )
                ]
            if value.get("k") == "param" and value.get("index") == 0:
                return [TsTarget("prop", locator, view.input.scope.key, name=None, tier="inferred")]
            return [
                TsTarget(
                    "param",
                    locator,
                    view.input.scope.key,
                    name=binding["name"],
                    tier="unknown",
                    conditions=("CALLABLE_PARAMETER",),
                )
            ]
        if kind in {"declare", "assign"}:
            return self.value(view, value, binding["node"], depth)
        return [_unknown("VALUE_KIND_" + kind.upper())]

    def value(
        self, view: ScopeView, value: dict[str, Any], node: int | None, depth: int
    ) -> list[TsTarget]:
        k = value.get("k")
        if depth > 6:
            return [_unknown("RESOLUTION_DEPTH")]
        if k == "scope":
            key = value.get("key")
            return (
                [TsTarget("scope", view.input.locator, key)]
                if key
                else [_unknown("SCOPE_NOT_PLANNED")]
            )
        if k == "name":
            return self.lookup(view, value["id"], node, depth + 1)
        if k == "attr":
            return self.attribute(view, value, node, depth + 1)
        if k == "fallback":
            options = [
                t for o in value.get("options") or [] for t in self.value(view, o, node, depth + 1)
            ]
            return [
                t.weaker("inferred", "FALLBACK_ALTERNATIVES")
                for t in options
                if t.kind != "unknown"
            ] or [_unknown("FALLBACK_UNKNOWN")]
        if k in {"call", "new"}:
            return self.call_value(view, value, node, depth)
        if k in {"property", "unpack"}:
            source = value.get("from") or {}
            if source.get("k") == "call":
                made = self.call_value(view, source, node, depth)
                if k == "property":
                    return [t for m in made for t in self._property(m, value.get("key"))]
                return [t for m in made for t in self._unpacked(m, int(value.get("index", 0)))]
        return [_unknown("VALUE_" + str(k).upper())]

    def call_value(
        self, view: ScopeView, shape: dict[str, Any], node: int | None, depth: int
    ) -> list[TsTarget]:
        callees = self.callee(view, shape.get("callee") or {}, node, depth + 1)
        results: list[TsTarget] = []
        for t in callees:
            qual = t.name or ""
            if shape.get("k") == "new" and t.kind == "class":
                results.append(
                    TsTarget(
                        "instance",
                        t.locator,
                        name=t.name,
                        tier=t.tier,
                        conditions=t.conditions,
                        data=t.data,
                    )
                )
            elif (
                t.kind == "external" and _package(qual) == "react" and qual.endswith(".useCallback")
            ):
                args = shape.get("args") or []
                results += self.value(view, args[0], node, depth + 1) if args else []
            elif t.kind == "external" and _package(qual) == "react" and qual.endswith(".useState"):
                results.append(_special("use_state", t.tier))
            elif (
                t.kind == "external"
                and qual.endswith(".useNavigate")
                and _package(qual) in {"react-router", "react-router-dom", "@tanstack/react-router"}
            ):
                results.append(_special("navigate", t.tier))
            elif (
                t.kind == "external"
                and _package(qual) == "@tanstack/react-query"
                and qual.endswith(".useMutation")
            ):
                options = (shape.get("args") or [{}])[0]
                results.append(
                    _special(
                        "mutation",
                        "inferred",
                        options=_freeze(options),
                        scope=view.input.scope.key,
                        locator=view.input.locator,
                    )
                )
            elif (
                t.kind == "external"
                and _package(qual) == "@tanstack/react-query"
                and qual.endswith(".useQueryClient")
            ):
                results.append(_special("query_client", "inferred"))
            elif (
                t.kind == "external"
                and _package(qual) == "react-hook-form"
                and qual.endswith(".useForm")
            ):
                results.append(_special("use_form", t.tier))
            elif (
                t.kind == "external"
                and _package(qual) in _CLIENT_PACKAGES
                and qual.rsplit(".", 1)[-1] in {"createClient", "default", "createConfig"}
            ):
                options = (shape.get("args") or [{}])[0]
                base = (
                    _object(options).get("baseUrl", {}).get("path")
                    if options.get("k") == "object"
                    else None
                )
                results.append(
                    _special(
                        "api_client",
                        "inferred",
                        "DESCRIPTOR_CLIENT_PACKAGE",
                        package=_package(qual),
                        base=base,
                    )
                )
            elif t.kind == "scope":
                results += self._returned(t, depth + 1)
            else:
                results.append(_unknown("CALL_RESULT_UNKNOWN"))
        return results or [_unknown("CALL_RESULT_UNKNOWN")]

    def _returned(self, function: TsTarget, depth: int) -> list[TsTarget]:
        """What a repository function returns, when it is a recognised factory."""
        view = self.views.get((function.locator or "", function.scope or ""))
        if view is None or depth > 5:
            return [_unknown("CALL_RESULT_UNKNOWN")]
        values: list[TsTarget] = []
        for shape in view.input.scope.summary.get("returns") or []:
            if shape.get("k") == "object" and self._client_object(view, shape):
                values.append(
                    _special(
                        "api_client",
                        "inferred",
                        "DESCRIPTOR_CLIENT_FACTORY_SHAPE",
                        package="repository",
                    )
                )
            elif shape.get("k") in {"call", "new", "name", "attr"}:
                values += [
                    t for t in self.value(view, shape, None, depth + 1) if t.kind == "special"
                ]
        return values or [_unknown("CALL_RESULT_UNKNOWN")]

    def _client_object(self, view: ScopeView, shape: dict[str, Any]) -> bool:
        """A vendored request client: an object with ≥3 HTTP-verb keys and ``request``, built in a
        module that imports ``axios`` or calls ``fetch`` (``DESCRIPTOR_CLIENT_FACTORY_SHAPE``)."""
        keys = {str(k.get("key")) for k in shape.get("keys") or []}
        if len(keys & _VERBS_LOWER) < 3 or "request" not in keys:
            return False
        imports = self.imports.get(view.input.locator, {})
        return any(row.from_module == "axios" for row in imports.values()) or any(
            c.callee.get("k") == "name" and c.callee.get("id") == "fetch"
            for (loc, _), v in self.views.items()
            if loc == view.input.locator
            for c in v.input.scope.callsites
        )

    def _property(self, target: TsTarget, key: Any) -> list[TsTarget]:
        if target.kind == "special" and target.name == "use_form" and key == "handleSubmit":
            return [_special("handle_submit", target.tier)]
        if (
            target.kind == "special"
            and target.name == "mutation"
            and key in {"mutate", "mutateAsync"}
        ):
            return [
                _special(
                    "mutation_call",
                    target.tier,
                    *target.conditions,
                    method=key,
                    **dict(target.data),
                )
            ]
        return [_unknown("PROPERTY_UNKNOWN")]

    def _unpacked(self, target: TsTarget, index: int) -> list[TsTarget]:
        if target.kind == "special" and target.name == "use_state" and index == 1:
            return [_special("state_setter", target.tier)]
        return [_unknown("UNPACKED_VALUE")]

    # ----------------------------------------------------------------- callees

    def callee(
        self, view: ScopeView, shape: dict[str, Any], node: int | None, depth: int = 0
    ) -> list[TsTarget]:
        k = shape.get("k")
        if k == "name":
            return self.lookup(view, shape["id"], node, depth)
        if k == "attr":
            return self.attribute(view, shape, node, depth)
        if k == "fallback":
            return self.value(view, shape, node, depth)
        if k in {"call", "new"}:
            return [_unknown("CALL_RESULT_RECEIVER")]
        if k == "super":
            base = self._base_class(view, depth)
            if base is None:
                return [_unknown("SUPER_CLASS_UNKNOWN")]
            constructor = self._constructor(base)
            if constructor is None:
                return [_unknown("SUPER_CONSTRUCTOR_IMPLICIT")]
            return [constructor.weaker("derived", "SUPER_CONSTRUCTOR")]
        return [_unknown("DYNAMIC_CALLEE")]

    def attribute(
        self, view: ScopeView, shape: dict[str, Any], node: int | None, depth: int
    ) -> list[TsTarget]:
        root = shape.get("root") or {}
        path = list(shape.get("path") or [])
        if root.get("k") == "this":
            current = self._this(view)
        elif root.get("k") in {"name", "fallback"}:
            current = self.callee(view, root, node, depth + 1)
        elif root.get("k") in {"call", "new"}:
            current = self.call_value(view, root, node, depth + 1)
        else:
            return [_unknown("DYNAMIC_RECEIVER")]
        for attribute in path:
            nxt: list[TsTarget] = []
            for t in current:
                nxt += self._member(t, attribute, depth)
            current = self._merge(nxt, 1)
        return current

    def _this(self, view: ScopeView) -> list[TsTarget]:
        cursor: ScopeView | None = view
        while cursor is not None and cursor.input.scope.kind != "method":
            cursor = self.parent(cursor)
        if cursor is None or cursor.input.scope.owner_symbol_id is None:
            return [_unknown("THIS_UNKNOWN")]
        owner = self.symbols.get(cursor.input.scope.owner_symbol_id)
        parent = self.symbols.get(owner.parent_id) if owner and owner.parent_id else None
        if parent is None or parent.kind != "class":
            return [_unknown("THIS_UNKNOWN")]
        return [
            TsTarget(
                "instance", parent.locator, name=parent.name, data=(("symbol", str(parent.id)),)
            )
        ]

    def _member(self, t: TsTarget, attribute: str, depth: int) -> list[TsTarget]:
        if t.kind == "module" and t.locator:
            return [
                x.weaker(t.tier, *t.conditions)
                for x in self.exported(t.locator, attribute, depth + 1)
            ]
        if t.kind in {"class", "instance"} and t.locator:
            class_id = t.get("symbol")
            for symbol in self.inp.symbols:
                if (
                    symbol.kind == "method"
                    and str(symbol.parent_id) == class_id
                    and symbol.name == attribute
                ):
                    view = self.by_owner.get(symbol.id)
                    if view is not None:
                        return [
                            TsTarget(
                                "scope",
                                view.input.locator,
                                view.input.scope.key,
                                tier=t.tier,
                                conditions=t.conditions,
                            )
                        ]
            return [_unknown("ATTRIBUTE_NOT_FOUND")]
        if t.kind == "external":
            return [
                TsTarget(
                    "external", name=f"{t.name}.{attribute}", tier=t.tier, conditions=t.conditions
                )
            ]
        if t.kind == "global":
            return [TsTarget("global", name=f"{t.name}.{attribute}", tier=t.tier)]
        if t.kind == "special":
            if t.name == "api_client":
                return [
                    _special("descriptor", t.tier, *t.conditions, verb=attribute, **dict(t.data))
                ]
            if t.name == "query_client" and attribute in {
                "invalidateQueries",
                "refetchQueries",
                "resetQueries",
            }:
                return [_special("invalidate", t.tier, *t.conditions, method=attribute)]
            if t.name == "mutation" and attribute in {"mutate", "mutateAsync"}:
                return [
                    _special(
                        "mutation_call", t.tier, *t.conditions, method=attribute, **dict(t.data)
                    )
                ]
        if t.kind == "prop":
            return (
                [TsTarget("prop", t.locator, t.scope, name=attribute, tier="inferred")]
                if t.name is None
                else [_unknown("PROP_MEMBER")]
            )
        if t.kind == "unknown":
            return [t]
        return [_unknown("RECEIVER_TYPE_UNKNOWN")]

    # ----------------------------------------------------------------- emission

    def run(self, endpoints: list[Any]) -> None:
        for (locator, key), view in sorted(self.views.items()):
            sc = view.input
            self.em.node(
                scope_key(locator, key),
                "function"
                if sc.scope.kind in {"function", "method"}
                or (sc.scope.name and sc.scope.kind != "module")
                else "scope",
                sc.scope.name or sc.scope.kind,
                locator,
                {
                    "kind": sc.scope.kind,
                    "callable_kind": sc.scope.callable_kind,
                    "scope": key,
                    "truncated": sc.scope.truncated,
                    "identity": self._identity(sc.scope.owner_symbol_id),
                    "language": sc.language,
                },
                [self.em.scope_ev(sc)],
            )
            if sc.scope.truncated:
                self.em.diagnose(
                    "FLOW_SCOPE_TRUNCATED", scope_key(locator, key), limit=sc.scope.truncated
                )
            if sc.scope.kind == "module":
                self.em.entry(
                    "module",
                    locator,
                    locator,
                    scope_key(locator, key),
                    locator,
                    [self.em.scope_ev(sc)],
                )
        for view in self.views.values():
            self.jsx(view)
        for (_, _), view in sorted(self.views.items()):
            for callsite in view.input.scope.callsites:
                self.callsite(view, callsite, endpoints)
        self.reruns()

    def reruns(self) -> None:
        """``setS(v)`` → ``may_rerun_effect`` for effects of the same component whose deps list
        ``s`` (or that have no deps array). Never "runs": the update may bail out or batch."""
        for key, component, state, ev in self.setters:
            for effect, deps, effect_ev in self.effects.get(component, []):
                if deps is not None and state not in deps and "?" not in deps:
                    continue
                self.em.edge(
                    "may_rerun_effect",
                    key,
                    effect,
                    "inferred",
                    RULES["react"],
                    (
                        "DEPENDENCY_CHANGE_NOT_DETERMINED",
                        "RENDER_MAY_BAIL_OUT",
                        *(("EFFECT_HAS_NO_DEPENDENCY_ARRAY",) if deps is None else ()),
                        *(("DEPENDENCIES_NOT_LITERAL",) if deps and "?" in deps else ()),
                    ),
                    {"state": state, "deps": deps},
                    [ev, effect_ev],
                )

    def jsx(self, view: ScopeView) -> None:
        sc, em = view.input, self.em
        owner = scope_key(sc.locator, sc.scope.key)
        for binding in sc.scope.jsx:
            ev = em.jsx_ev(sc, binding)
            key = jsx_key(sc.locator, sc.scope.key, binding.ordinal)
            value = binding.value
            if (
                binding.intrinsic
                and binding.attribute.startswith("on")
                and binding.attribute[2:3].isupper()
            ):
                scheduler = em.node(
                    f"scheduler:dom_event:{binding.attribute}",
                    "scheduler",
                    f"DOM {binding.attribute}",
                    None,
                    {"event": binding.attribute},
                )
                handlers: list[tuple[TsTarget, tuple[str, ...]]] = []
                if value.get("k") == "call":
                    for t in self.callee(view, value.get("callee") or {}, binding.cfg_node):
                        if t.kind == "special" and t.name == "handle_submit":
                            args = value.get("args") or []
                            for index, arg in enumerate(args[:2]):
                                for h in self.value(view, arg, binding.cfg_node, 1):
                                    handlers.append(
                                        (
                                            h,
                                            (
                                                "HANDLER_IS_CALL_RESULT",
                                                "VALIDATION_MAY_BLOCK",
                                                "ON_VALID" if index == 0 else "ON_INVALID",
                                            ),
                                        )
                                    )
                        else:
                            em.edge(
                                "unresolved",
                                owner,
                                None,
                                "unknown",
                                RULES["react"],
                                ("HANDLER_IS_CALL_RESULT",),
                                {"jsx": key},
                                [ev],
                                key,
                            )
                else:
                    handlers = [(h, ()) for h in self.value(view, value, binding.cfg_node, 1)]
                for h, conditions in handlers:
                    if h.kind == "scope" and h.locator and h.scope:
                        target = scope_key(h.locator, h.scope)
                        rule = RULES["callbacks"] if conditions else RULES["react"]
                        em.edge(
                            "registers",
                            owner,
                            target,
                            "inferred" if conditions else h.tier,
                            rule,
                            (*h.conditions, *conditions),
                            {
                                "scheduler": f"dom_event:{binding.attribute}",
                                "element": binding.element,
                                "jsx": key,
                                "cfg_node": binding.cfg_node,
                            },
                            [ev],
                            key,
                        )
                        em.edge(
                            "scheduled_by",
                            scheduler,
                            target,
                            "derived",
                            RULES["react"],
                            ("ELEMENT_MUST_BE_MOUNTED",),
                            {"jsx": key},
                            [ev],
                            key,
                        )
                        em.entry(
                            "dom_event",
                            key,
                            f"<{binding.element} {binding.attribute}>",
                            target,
                            sc.locator,
                            [ev],
                            {"event": binding.attribute, "element": binding.element},
                            "inferred" if conditions else h.tier,
                            ("ELEMENT_MUST_BE_MOUNTED", *conditions),
                        )
                    elif h.kind == "prop":
                        em.edge(
                            "unresolved",
                            owner,
                            None,
                            "unknown",
                            RULES["react"],
                            ("HANDLER_FROM_PROPS",),
                            {"jsx": key, "prop": h.name},
                            [ev],
                            key,
                        )
                    elif h.kind == "unknown":
                        em.edge(
                            "unresolved",
                            owner,
                            None,
                            "unknown",
                            RULES["react"],
                            (h.name or "DYNAMIC_HANDLER",),
                            {"jsx": key},
                            [ev],
                            key,
                        )
            elif not binding.intrinsic and binding.attribute != "...":
                components = (
                    [
                        c
                        for c in self.lookup(view, binding.element.split(".")[0], binding.cfg_node)
                        if c.kind == "scope"
                    ]
                    if "." not in binding.element
                    else []
                )
                for c in components:
                    for h in self.value(view, value, binding.cfg_node, 1):
                        if h.kind == "scope" and h.locator and h.scope:
                            child = scope_key(c.locator or "", c.scope or "")
                            self.prop_bindings.setdefault(
                                (c.locator or "", c.scope or "", binding.attribute), []
                            ).append((scope_key(h.locator, h.scope), ev))
                            em.edge(
                                "binds_prop_callback",
                                child,
                                scope_key(h.locator, h.scope),
                                "inferred",
                                RULES["react"],
                                ("PROP_CALLBACK_BINDING", *h.conditions),
                                {"prop": binding.attribute, "jsx": key},
                                [ev],
                                key,
                            )

    def callsite(self, view: ScopeView, callsite: FlowCallsite, endpoints: list[Any]) -> None:
        sc, em = view.input, self.em
        key = call_key(sc.locator, sc.scope.key, callsite.ordinal)
        ev = em.callsite_ev(sc, callsite)
        em.node(
            key,
            "callsite",
            _label(callsite.callee),
            sc.locator,
            {
                "ordinal": callsite.ordinal,
                "cfg_node": callsite.cfg_node,
                "copies": list(callsite.copies),
                "awaited": callsite.awaited,
                "line": callsite.span.start_line,
                "language": sc.language,
            },
            [ev],
        )
        em.edge(
            "contains_callsite",
            scope_key(sc.locator, sc.scope.key),
            key,
            "observed",
            RULES["ts_resolution"],
            (),
            {"ordinal": callsite.ordinal},
            [ev],
        )
        targets = self.callee(view, callsite.callee, callsite.cfg_node)
        rule = RULES["ts_resolution"]
        tail = (
            (callsite.callee.get("path") or [None])[-1]
            if callsite.callee.get("k") == "attr"
            else None
        )
        if tail in {"then", "catch", "finally"}:
            # Promise reactions run as microtasks after settlement; the receiver's promise type
            # is not verified (``THENABLE_ASSUMED``).
            positions = (0, 1) if tail == "then" else (0,)
            self.schedule(view, callsite, key, ev, "microtask", positions, ("THENABLE_ASSUMED",))
        for t in targets[: FANOUT + 1]:
            if t.kind == "scope" and t.locator and t.scope:
                target = scope_key(t.locator, t.scope)
                callee = self.views.get((t.locator, t.scope))
                em.edge("resolves_to", key, target, t.tier, rule, t.conditions, {}, [ev])
                em.edge(
                    _invocation(
                        callee.input.scope.callable_kind if callee else "sync_function",
                        callsite.awaited,
                    ),
                    key,
                    target,
                    t.tier,
                    RULES["js_async"],
                    t.conditions,
                    {"awaited": callsite.awaited},
                    [ev],
                )
            elif t.kind == "class":
                self.construct(callsite, key, ev, t)
            elif t.kind == "instance" and callsite.callee.get("new"):
                em.edge(
                    "constructs",
                    key,
                    None,
                    "unknown",
                    RULES["ts_construction"],
                    ("CONSTRUCTOR_NOT_MODELLED",),
                    {"class": t.name},
                    [ev],
                )
            elif t.kind == "external":
                self.external(view, callsite, key, ev, t)
            elif t.kind == "special":
                self.special(view, callsite, key, ev, t, endpoints)
            elif t.kind == "prop":
                found = self.prop_bindings.get((t.locator or "", t.scope or "", t.name or ""), [])
                if not found:
                    em.edge(
                        "unresolved",
                        key,
                        None,
                        "unknown",
                        rule,
                        ("PROP_CALLBACK_UNBOUND",),
                        {"prop": t.name},
                        [ev],
                    )
                for handler, handler_ev in found[:FANOUT]:
                    em.edge(
                        "invokes_callback",
                        key,
                        handler,
                        "inferred",
                        RULES["react"],
                        ("PROP_CALLBACK_BINDING",),
                        {"prop": t.name},
                        [ev, handler_ev],
                    )
            elif t.kind == "global":
                em.edge(
                    "unresolved", key, None, "unknown", rule, ("GLOBAL",), {"global": t.name}, [ev]
                )
                scheduler = _SCHEDULING_GLOBALS.get(t.name or "")
                if scheduler is not None:
                    self.schedule(view, callsite, key, ev, scheduler, (0,), ())
            else:
                code = t.name if t.kind == "unknown" and t.name else "CALLABLE_" + t.kind.upper()
                em.edge(
                    "unresolved",
                    key,
                    None,
                    "unknown",
                    rule,
                    tuple(sorted({code, *t.conditions})),
                    {},
                    [ev],
                )

    def external(
        self, view: ScopeView, callsite: FlowCallsite, key: str, ev: str, t: TsTarget
    ) -> None:
        em, qual = self.em, t.name or ""
        package = _package(qual)
        hook = qual.rsplit(".", 1)[-1]
        args = [a for a in callsite.arguments if a.get("pos") is not None]
        component = scope_key(view.input.locator, view.input.scope.key)
        if package == "react" and hook in {"useEffect", "useLayoutEffect"} and args:
            scheduler = "react_commit:layout" if hook == "useLayoutEffect" else "react_commit"
            node = em.node(f"scheduler:{scheduler}", "scheduler", scheduler, None, {})
            deps = args[1]["shape"] if len(args) > 1 else None
            for h in self.value(view, args[0]["shape"], callsite.cfg_node, 1):
                if h.kind == "scope" and h.locator and h.scope:
                    target = scope_key(h.locator, h.scope)
                    em.edge(
                        "registers",
                        key,
                        target,
                        h.tier,
                        RULES["react"],
                        (
                            "EFFECT_TIMING_RELATIVE_TO_PAINT_NOT_DETERMINED",
                            "STRICT_MODE_DEV_DOUBLE_INVOKE_POSSIBLE",
                        ),
                        {"scheduler": scheduler, "deps": _deps(deps)},
                        [ev],
                    )
                    em.edge(
                        "scheduled_by",
                        node,
                        target,
                        "derived",
                        RULES["react"],
                        ("RUNS_AFTER_COMMIT_WHEN_DEPS_CHANGE",),
                        {"component": component},
                        [ev],
                    )
                    self.effects.setdefault(component, []).append((target, _deps(deps), ev))
                    em.entry(
                        "effect",
                        key,
                        hook,
                        target,
                        view.input.locator,
                        [ev],
                        {"scheduler": scheduler, "deps": _deps(deps)},
                        "inferred",
                        ("EFFECT_TIMING_RELATIVE_TO_PAINT_NOT_DETERMINED",),
                    )
                    self.cleanup(h, target, scheduler, ev)
            return
        if package == "react" and hook == "useMemo" and args:
            for h in self.value(view, args[0]["shape"], callsite.cfg_node, 1):
                if h.kind == "scope" and h.locator and h.scope:
                    em.edge(
                        "invokes",
                        key,
                        scope_key(h.locator, h.scope),
                        "inferred",
                        RULES["react"],
                        ("RECOMPUTE_WHEN_DEPS_CHANGE_OR_CACHE_DISCARDED",),
                        {"phase": "render"},
                        [ev],
                    )
            return
        if package == "@tanstack/react-query" and hook in {"useQuery", "useSuspenseQuery"} and args:
            options = args[0]["shape"]
            values = _object(options)
            enabled = values.get("enabled")
            if enabled and enabled.get("t") == "bool" and enabled.get("v") is False:
                em.edge(
                    "unresolved",
                    key,
                    None,
                    "unknown",
                    RULES["tanstack"],
                    ("ENABLED_FALSE",),
                    {},
                    [ev],
                )
                return
            node = em.node("scheduler:tanstack_query", "scheduler", "TanStack query", None, {})
            for h in self.value(view, values.get("queryFn") or {}, callsite.cfg_node, 1):
                if h.kind == "scope" and h.locator and h.scope:
                    target = scope_key(h.locator, h.scope)
                    conditions = ("QUERY_MAY_BE_CACHED", "ORDER_NOT_DETERMINED") + (
                        ("ENABLED_CONDITION",) if enabled else ()
                    )
                    em.edge(
                        "registers",
                        key,
                        target,
                        "inferred",
                        RULES["tanstack"],
                        conditions,
                        {"scheduler": "tanstack_query"},
                        [ev],
                    )
                    em.edge(
                        "scheduled_by",
                        node,
                        target,
                        "inferred",
                        RULES["tanstack"],
                        conditions,
                        {},
                        [ev],
                    )
            return
        if qual.split(".")[0] in {"localStorage", "sessionStorage"}:
            node = em.node(
                f"effect:{key}", "storage_effect", hook, view.input.locator, {"api": qual}
            )
            em.edge("crosses_boundary", key, node, "observed", RULES["react"], (), {}, [ev])
            return
        boundary = em.node(
            f"external:{package}", "integration_boundary", package, None, {"package": package}
        )
        em.edge(
            "unresolved",
            key,
            None,
            "unknown",
            RULES["ts_resolution"],
            (f"EXTERNAL_PACKAGE:{package}",),
            {"qualified": qual},
            [ev],
        )
        em.edge(
            "crosses_boundary",
            key,
            boundary,
            "derived",
            RULES["ts_resolution"],
            t.conditions,
            {"qualified": qual},
            [ev],
        )

    def special(
        self,
        view: ScopeView,
        callsite: FlowCallsite,
        key: str,
        ev: str,
        t: TsTarget,
        endpoints: list[Any],
    ) -> None:
        em = self.em
        component = scope_key(view.input.locator, view.input.scope.key)
        if t.name == "state_setter":
            found = self._state_name(view, callsite.callee, callsite.cfg_node)
            if found is not None:
                self.setters.append((key, found[0], found[1], ev))
            node = em.node(f"effect:{key}", "state_update", "setState", view.input.locator, {})
            em.edge(
                "schedules_render",
                key,
                node,
                "inferred",
                RULES["react"],
                ("RENDER_MAY_BAIL_OUT", "UPDATES_MAY_BE_BATCHED", "COMPONENT_MAY_BE_UNMOUNTED"),
                {
                    "component": found[0] if found else component,
                    "state": found[1] if found else None,
                },
                [ev],
            )
        elif t.name == "navigate":
            target = (
                next((a.get("shape") for a in callsite.arguments if a.get("pos") == 0), None) or {}
            )
            path = target.get("path") if target.get("k") == "lit" else _template_path(target)
            node = em.node(
                f"effect:{key}",
                "navigation",
                path or "navigate()",
                view.input.locator,
                {"path": path},
            )
            em.edge(
                "navigates",
                key,
                node,
                "inferred" if path is None else "derived",
                RULES["react"],
                () if path else ("NAVIGATION_TARGET_UNKNOWN",),
                {},
                [ev],
            )
        elif t.name == "mutation_call":
            self.mutation(view, callsite, key, ev, t)
        elif t.name == "invalidate":
            node = em.node("scheduler:tanstack_query", "scheduler", "TanStack query", None, {})
            em.edge(
                "may_refetch",
                key,
                node,
                "inferred",
                RULES["tanstack"],
                ("QUERY_KEY_MATCH_NOT_DETERMINED", "QUERY_MAY_BE_CACHED"),
                {"method": t.get("method")},
                [ev],
            )
        elif t.name == "descriptor":
            self.descriptor(view, callsite, key, ev, t, endpoints)
        elif t.name == "handle_submit":
            args = [a for a in callsite.arguments if a.get("pos") is not None]
            for index, arg in enumerate(args[:2]):
                for h in self.value(view, arg["shape"], callsite.cfg_node, 1):
                    if h.kind == "scope" and h.locator and h.scope:
                        em.edge(
                            "registers",
                            key,
                            scope_key(h.locator, h.scope),
                            "inferred",
                            RULES["callbacks"],
                            ("VALIDATION_MAY_BLOCK", "ON_VALID" if index == 0 else "ON_INVALID"),
                            {
                                "scheduler": "library_callback",
                                "contract": "react-hook-form.handleSubmit",
                            },
                            [ev],
                        )
        else:
            em.edge(
                "unresolved",
                key,
                None,
                "unknown",
                RULES["ts_resolution"],
                ("SPECIAL_VALUE_" + str(t.name).upper(),),
                {},
                [ev],
            )

    def schedule(
        self,
        view: ScopeView,
        callsite: FlowCallsite,
        key: str,
        ev: str,
        scheduler: str,
        positions: tuple[int, ...],
        conditions: tuple[str, ...],
    ) -> None:
        """A callback handed to a scheduler: ``registers`` (nothing runs now) and
        ``scheduled_by`` (it runs later, in no order relative to other lanes)."""
        node = self.em.node(f"scheduler:{scheduler}", "scheduler", scheduler, None, {})
        for argument in callsite.arguments:
            if argument.get("pos") not in positions:
                continue
            for h in self.value(view, argument.get("shape") or {}, callsite.cfg_node, 1):
                if h.kind != "scope" or not h.locator or not h.scope:
                    continue
                target = scope_key(h.locator, h.scope)
                self.em.edge(
                    "registers",
                    key,
                    target,
                    h.tier if not conditions else "inferred",
                    RULES["js_async"],
                    (*h.conditions, *conditions, "ORDER_NOT_DETERMINED"),
                    {"scheduler": scheduler, "argument": argument.get("pos")},
                    [ev],
                )
                self.em.edge(
                    "scheduled_by",
                    node,
                    target,
                    "inferred",
                    RULES["js_async"],
                    ("ORDER_NOT_DETERMINED",),
                    {},
                    [ev],
                )

    def _identity(self, owner: Any) -> str | None:
        symbol = self.symbols.get(owner) if owner is not None else None
        return str(symbol.stable_identity_id) if symbol and symbol.stable_identity_id else None

    # ----------------------------------------------------------------- React state and effects

    def cleanup(self, effect: TsTarget, target: str, scheduler: str, ev: str) -> None:
        """A function returned by an effect is its cleanup: before a re-run and on unmount."""
        view = self.views.get((effect.locator or "", effect.scope or ""))
        if view is None:
            return
        for shape in view.input.scope.summary.get("returns") or []:
            for h in self.value(view, shape, None, 1):
                if h.kind == "scope" and h.locator and h.scope:
                    self.em.edge(
                        "registers",
                        target,
                        scope_key(h.locator, h.scope),
                        h.tier,
                        RULES["react"],
                        ("CLEANUP_BEFORE_RERUN_OR_UNMOUNT",),
                        {"scheduler": f"{scheduler}:cleanup"},
                        [ev],
                    )

    def _state_name(
        self, view: ScopeView, callee: dict[str, Any], node: int | None
    ) -> tuple[str, str] | None:
        """(component scope key, state name) for a ``useState`` setter called as ``callee``."""
        if callee.get("k") != "name":
            return None
        name = callee["id"]
        cursor: ScopeView | None = view
        while cursor is not None and name not in cursor.by_name:
            cursor = self.parent(cursor)
        if cursor is None:
            return None
        for binding in cursor.by_name[name]:
            value = binding.get("value") or {}
            if value.get("k") != "unpack" or value.get("index") != 1:
                continue
            for sibling in cursor.bindings:
                other = sibling.get("value") or {}
                if (
                    other.get("k") == "unpack"
                    and other.get("index") == 0
                    and other.get("from") == value.get("from")
                    and sibling["node"] == binding["node"]
                ):
                    return (
                        scope_key(cursor.input.locator, cursor.input.scope.key),
                        sibling["name"],
                    )
        return None

    # ----------------------------------------------------------------- construction

    def construct(self, callsite: FlowCallsite, key: str, ev: str, cls: TsTarget) -> None:
        """``new C(...)`` (``ts-construction.v1``): field initialisers, then the constructor; a
        class without one runs its base's (implicit ``super(...args)``). A constructor that
        returns a value may replace the instance (``CONSTRUCTOR_RETURN_OVERRIDE``)."""
        em, rule = self.em, RULES["ts_construction"]
        node = em.node(
            f"tsclass:{cls.locator}#{cls.name}",
            "scope",
            cls.name or "class",
            cls.locator,
            {"kind": "class"},
        )
        conditions = list(cls.conditions)
        if int(cls.get("fields") or 0):
            conditions.append("FIELD_INITIALISERS_NOT_LOWERED")
        chain: list[tuple[TsTarget, str]] = []
        current: TsTarget | None = cls
        implicit = False
        for _ in range(4):
            if current is None:
                break
            constructor = self._constructor(current)
            if constructor is not None:
                chain.append((constructor, "inherited_constructor" if implicit else "constructor"))
                break
            implicit = True
            defining = self.views.get((current.locator or "", str(current.get("defining") or "")))
            current = self._resolve_base(defining, current, 0) if defining else None
        if chain:
            constructor_view = self.views.get((chain[0][0].locator or "", chain[0][0].scope or ""))
            returns = (
                constructor_view.input.scope.summary.get("returns") if constructor_view else None
            )
            if returns:
                conditions.append("CONSTRUCTOR_RETURN_OVERRIDE")
        elif cls.get("extends") and current is None:
            conditions.append("BASE_CLASS_UNRESOLVED")
        em.edge(
            "constructs",
            key,
            node,
            "unknown" if "CONSTRUCTOR_RETURN_OVERRIDE" in conditions else cls.tier,
            rule,
            tuple(sorted(set(conditions))),
            {"class": cls.name},
            [ev],
        )
        for constructor, phase in chain:
            em.edge(
                "invokes",
                key,
                scope_key(constructor.locator or "", constructor.scope or ""),
                constructor.tier,
                rule,
                ("IMPLICIT_SUPER_CONSTRUCTOR",) if phase == "inherited_constructor" else (),
                {"phase": phase},
                [ev],
            )

    def _constructor(self, cls: TsTarget) -> TsTarget | None:
        class_id = cls.get("symbol")
        if class_id is None:
            return None
        for symbol in self.inp.symbols:
            if symbol.name == "constructor" and str(symbol.parent_id) == class_id:
                view = self.by_owner.get(symbol.id)
                if view is not None:
                    return TsTarget(
                        "scope",
                        view.input.locator,
                        view.input.scope.key,
                        tier=cls.tier,
                        conditions=cls.conditions,
                    )
        return None

    def _resolve_base(self, defining: ScopeView, cls: TsTarget, depth: int) -> TsTarget | None:
        extends = cls.get("extends")
        if not extends:
            return None
        bases = [
            t for t in self.value(defining, _thaw(extends), None, depth + 1) if t.kind == "class"
        ]
        return bases[0] if len(bases) == 1 else None

    def _base_class(self, view: ScopeView, depth: int) -> TsTarget | None:
        """The base class of the class whose method ``view`` is (for ``super(...)``)."""
        cursor: ScopeView | None = view
        while cursor is not None and cursor.input.scope.kind != "method":
            cursor = self.parent(cursor)
        if cursor is None or cursor.input.scope.owner_symbol_id is None:
            return None
        owner = self.symbols.get(cursor.input.scope.owner_symbol_id)
        parent = self.symbols.get(owner.parent_id) if owner and owner.parent_id else None
        if parent is None or parent.kind != "class":
            return None
        holder = self.parent(cursor)
        while holder is not None and parent.name not in holder.by_name:
            holder = self.parent(holder)
        if holder is None:
            return None
        for binding in holder.by_name[parent.name]:
            if binding["kind"] == "class":
                cls = self.interpret(holder, binding, depth + 1)[0]
                return self._resolve_base(holder, cls, depth)
        return None

    def mutation(
        self, view: ScopeView, callsite: FlowCallsite, key: str, ev: str, t: TsTarget
    ) -> None:
        em, rule = self.em, RULES["tanstack"]
        data = dict(t.data)
        owner = self.views.get((data.get("locator") or "", data.get("scope") or ""))
        options = _object(_thaw(data.get("options")))
        method = data.get("method")
        if owner is None:
            em.edge("unresolved", key, None, "unknown", rule, ("MUTATION_OWNER_UNKNOWN",), {}, [ev])
            return
        node = em.node("scheduler:tanstack_mutation", "scheduler", "TanStack mutation", None, {})
        functions = self.value(owner, options.get("mutationFn") or {}, None, 1)
        for fn in functions:
            if fn.kind != "scope" or not fn.locator or not fn.scope:
                continue
            target = scope_key(fn.locator, fn.scope)
            em.edge(
                "invokes_callback",
                key,
                target,
                "inferred",
                rule,
                (),
                {
                    "scheduler": "tanstack_mutation",
                    "method": method,
                    "rejection_propagates": method == "mutateAsync",
                },
                [ev],
            )
            em.edge("scheduled_by", node, target, "inferred", rule, (), {}, [ev])
            for name, outcome in (
                ("onMutate", "before"),
                ("onSuccess", "fulfilled"),
                ("onError", "rejected"),
                ("onSettled", "settled"),
            ):
                for cb in self.value(owner, options.get(name) or {}, None, 1):
                    if cb.kind == "scope" and cb.locator and cb.scope:
                        em.edge(
                            "continues_with",
                            target,
                            scope_key(cb.locator, cb.scope),
                            "inferred",
                            rule,
                            (),
                            {"outcome": outcome, "level": "hook"},
                            [ev],
                            name,
                        )
            per_call = next((a.get("shape") for a in callsite.arguments if a.get("pos") == 1), None)
            for name, outcome in (
                ("onSuccess", "fulfilled"),
                ("onError", "rejected"),
                ("onSettled", "settled"),
            ):
                for cb in self.value(
                    view, _object(per_call or {}).get(name) or {}, callsite.cfg_node, 1
                ):
                    if cb.kind == "scope" and cb.locator and cb.scope:
                        em.edge(
                            "continues_with",
                            target,
                            scope_key(cb.locator, cb.scope),
                            "inferred",
                            rule,
                            ("MAY_NOT_RUN_IF_UNMOUNTED",),
                            {"outcome": outcome, "level": "call", "callsite": key},
                            [ev],
                            f"{key}:{name}",
                        )

    def descriptor(
        self,
        view: ScopeView,
        callsite: FlowCallsite,
        key: str,
        ev: str,
        t: TsTarget,
        endpoints: list[Any],
    ) -> None:
        em, rule = self.em, RULES["descriptors"]
        verb = str(t.get("verb") or "")
        first = next((a.get("shape") for a in callsite.arguments if a.get("pos") == 0), None) or {}
        path: str | None = None
        if verb in _VERBS_UPPER:
            path = first.get("path") if first.get("k") == "lit" else _template_path(first)
        elif verb in _VERBS_LOWER:
            path = _object(first).get("url", {}).get("path") if first.get("k") == "object" else None
            verb = verb.upper()
        else:
            return
        node = em.node(
            f"descriptor:{key}",
            "http_request",
            f"{verb} {path or '?'}",
            view.input.locator,
            {"method": verb, "path": path, "client": t.get("package"), "source": "descriptor"},
        )
        conditions = ("DESCRIPTOR_REQUEST_SITE", HTTP_CAVEAT, *t.conditions)
        em.edge("issues_request", key, node, "inferred", rule, conditions, {}, [ev])
        if path is None:
            em.edge("unresolved", node, None, "unknown", rule, ("OPAQUE_PATH",), {}, [ev])
            return
        base = t.get("base")
        if isinstance(base, str):  # a root-relative literal ``baseUrl`` prefixes the path
            path = base.rstrip("/") + "/" + path.lstrip("/")
        origin = "BASE_FROM_FACTORY_LITERAL" if isinstance(base, str) else "BASE_UNKNOWN"
        matches = [e for e in endpoints if _endpoint_matches(e, verb, path)]
        if not matches:
            em.edge(
                "unresolved", node, None, "unknown", rule, ("NO_CANDIDATE",), {"path": path}, [ev]
            )
        for e in matches[:FANOUT]:
            em.node(
                e.key,
                "api_endpoint",
                e.attributes.get("function") or e.key,
                e.locator,
                {"paths": e.attributes.get("paths"), "methods": e.attributes.get("methods")},
                [em.stage14_ev(e)],
            )
            extra = ("AMBIGUOUS_CANDIDATE",) if len(matches) > 1 else ()
            em.edge(
                "calls_endpoint",
                node,
                e.key,
                "inferred",
                rule,
                (*conditions, origin, *extra, *_method_conditions(e)),
                {"method": verb, "path": path},
                [ev, em.stage14_ev(e)],
            )


# ----------------------------------------------------------------------------- helpers


def _package(qualified: str) -> str:
    if qualified.startswith("@"):
        return "/".join(qualified.split("/")[:2]).split(".")[0] if "/" in qualified else qualified
    return qualified.split(".")[0].split("/")[0]


def _label(shape: dict[str, Any]) -> str:
    if shape.get("k") == "name":
        return f"{shape.get('id')}()"
    if shape.get("k") == "super":
        return "super()"
    if shape.get("k") == "attr":
        root = shape.get("root") or {}
        head = (
            root.get("id")
            if root.get("k") == "name"
            else ("this" if root.get("k") == "this" else "…")
        )
        return f"{head}.{'.'.join(shape.get('path') or [])}()"
    return "…()"


def _invocation(kind: str, awaited: bool) -> str:
    if kind == "js_async_function":
        return "awaits" if awaited else "invokes_until_first_await"
    if kind == "js_generator_function":
        return "creates_generator"
    if kind == "js_async_generator_function":
        return "creates_async_generator"
    return "invokes"


def _object(shape: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if shape.get("k") != "object":
        return {}
    return {
        str(item.get("key")): item.get("shape") or {}
        for item in shape.get("keys") or []
        if not item.get("spread")
    }


_SCHEDULING_GLOBALS = {
    "setTimeout": "timer",
    "setInterval": "timer",
    "requestAnimationFrame": "animation_frame",
    "queueMicrotask": "microtask",
}


def _deps(shape: dict[str, Any] | None) -> list[str] | None:
    if shape is None:
        return None
    if shape.get("k") == "collection":
        return [i.get("id") for i in shape.get("items") or [] if i.get("k") == "name"]
    return ["?"]


def _template_path(shape: dict[str, Any]) -> str | None:
    if shape.get("k") != "template":
        return None
    text = ""
    for part in shape.get("parts") or []:
        if "s" in part:
            static = part["s"].get("path")
            if static is None and not text:
                return None
            text += static if static is not None else "{?}"
        else:
            text += "{param}"
    return text or None


def _segments(path: str) -> list[str]:
    return [s for s in path.split("?", 1)[0].strip("/").split("/") if s]


def _endpoint_matches(endpoint: Any, method: str, path: str) -> bool:
    """Segment-aligned match of a descriptor path against an endpoint's full paths. A
    placeholder matches exactly one segment; typed converters accept only their characters."""
    methods = {str(m).upper() for m in endpoint.attributes.get("methods") or []}
    if methods and method not in methods:
        return False
    wanted = _segments(path)
    for item in endpoint.attributes.get("paths") or []:
        full = item.get("path") if isinstance(item, dict) else None
        if not full:
            continue
        pattern = _segments(full)
        if len(pattern) != len(wanted) and not (
            pattern and pattern[-1].startswith("{") and ":path}" in pattern[-1]
        ):
            continue
        if all(_segment(p, w) for p, w in zip(pattern, wanted, strict=False)):
            return True
    return False


def _method_conditions(endpoint: Any) -> tuple[str, ...]:
    return () if endpoint.attributes.get("methods") else ("ENDPOINT_METHOD_UNKNOWN",)


def _segment(pattern: str, value: str) -> bool:
    if value == "{param}" or value == "{?}":
        return pattern.startswith("{")
    if pattern.startswith("{") and pattern.endswith("}"):
        converter = pattern[1:-1].split(":", 1)[1] if ":" in pattern else "str"
        if converter == "int":
            return value.isdigit()
        return True
    return pattern == value


def _freeze(value: Any) -> Any:
    import json

    return json.dumps(value, sort_keys=True)


def _thaw(value: Any) -> dict[str, Any]:
    import json

    return json.loads(value) if isinstance(value, str) else {}
