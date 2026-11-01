"""Stage 15 Milestone B: Python call resolution, construction and framework semantics.

Rules (versioned in ``domain.flow.RULES``) apply in order and the first match wins:
local reaching definitions → enclosing scopes → module bindings → imports (Stage 13
``ImportResolver``) → builtins; attribute chains through modules, classes (C3 MRO over resolved
bases), local constructors and annotations. **No name is ever matched across files by text**:
a cross-file edge always goes through an import binding or a Stage 14 element.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from buildlens.application.flow_inputs import (
    Emitter,
    FlowInput,
    ScopeView,
    call_key,
    scope_key,
)
from buildlens.application.semantic_catalog import ImportResolver
from buildlens.domain.flow import FANOUT, RULES
from buildlens.domain.flow_evidence import FlowCallsite

_BUILTINS = {
    "print",
    "len",
    "isinstance",
    "issubclass",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "frozenset",
    "range",
    "enumerate",
    "zip",
    "map",
    "filter",
    "sorted",
    "reversed",
    "min",
    "max",
    "sum",
    "any",
    "all",
    "abs",
    "round",
    "repr",
    "hash",
    "id",
    "type",
    "getattr",
    "setattr",
    "hasattr",
    "delattr",
    "iter",
    "next",
    "open",
    "vars",
    "dir",
    "super",
    "object",
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "RuntimeError",
    "LookupError",
    "NotImplementedError",
    "AssertionError",
    "StopIteration",
    "OSError",
    "AttributeError",
    "IndexError",
    "PermissionError",
    "FileNotFoundError",
    "UnicodeDecodeError",
    "BaseException",
    "callable",
    "format",
    "divmod",
    "pow",
    "bytes",
    "bytearray",
    "memoryview",
    "chr",
    "ord",
    "staticmethod",
    "classmethod",
    "property",
    "exec",
    "eval",
    "compile",
    "globals",
    "locals",
    "__import__",
    "input",
    "breakpoint",
    "slice",
    "complex",
    "ascii",
    "bin",
    "hex",
    "oct",
}
_SESSION_OPS = {
    "add": "stage",
    "add_all": "stage",
    "delete": "stage",
    "merge": "stage",
    "get": "query",
    "query": "query",
    "scalars": "query",
    "scalar": "query",
    "execute": "query",
    "flush": "flush",
    "commit": "commit",
    "rollback": "rollback",
    "close": "close",
    "begin": "transaction_scope",
    "begin_nested": "transaction_scope",
}
_STATEMENTS = {
    "select": "query",
    "insert": "dml_executed",
    "update": "dml_executed",
    "delete": "dml_executed",
}
_RESPONSE_DEFAULTS = {"RedirectResponse": 307}
_ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "api_route", "route"}
_STATUS = re.compile(r"HTTP_(\d{3})_")


# Decorators that leave the decorated function's body as the callee (py-call-resolution.v1).
_TRANSPARENT_DECORATORS = {
    "staticmethod",
    "classmethod",
    "property",
    "abc.abstractmethod",
    "dataclasses.dataclass",
    "typing.overload",
    "typing.final",
    "typing.override",
    "typing_extensions.override",
    "functools.wraps",
}
# Registration decorators of supported frameworks: they return the function unchanged.
_REGISTRATION_PREFIXES = ("fastapi.", "starlette.", "flask.", "typer.", "click.", "pytest.")
_REGISTRATION_TAILS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "route",
    "api_route",
    "websocket",
    "middleware",
    "exception_handler",
    "on_event",
    "command",
    "callback",
    "group",
    "fixture",
    "parametrize",
}
_CLI_TAILS = {"command", "callback", "group"}
_CONTEXT_MANAGERS = {"contextlib.contextmanager", "contextlib.asynccontextmanager"}
_NORETURN = {
    "typing.NoReturn",
    "typing.Never",
    "typing_extensions.NoReturn",
    "typing_extensions.Never",
}
_GENERATOR_METHODS = {
    "send": ("resumes", None),
    "__next__": ("resumes", None),
    "asend": ("resumes", None),
    "__anext__": ("resumes", None),
    "throw": ("throws_into", None),
    "athrow": ("throws_into", None),
    "close": ("throws_into", "GeneratorExit"),
    "aclose": ("throws_into", "GeneratorExit"),
}


@dataclass(frozen=True, slots=True)
class Target:
    """What a name, attribute or call denotes. ``tier``/``conditions`` record how it was found."""

    # scope | class | module | external | builtin | instance | ext_instance | param | unknown |
    # generator (an un-run generator iterator) | context_manager | generator_method
    kind: str
    locator: str | None = None
    scope: str | None = None
    name: str | None = None
    tier: str = "derived"
    conditions: tuple[str, ...] = ()

    def weaker(self, tier: str, *conditions: str) -> Target:
        order = ("observed", "derived", "inferred", "unknown")
        new = tier if order.index(tier) > order.index(self.tier) else self.tier
        return replace(
            self, tier=new, conditions=tuple(sorted(set(self.conditions) | set(conditions)))
        )


def _unknown(code: str) -> Target:
    return Target("unknown", name=code, tier="unknown", conditions=(code,))


@dataclass(slots=True)
class _ClassInfo:
    locator: str
    key: str
    name: str
    methods: dict[str, str] = field(default_factory=dict)
    bases: list[dict[str, Any]] = field(default_factory=list)
    keywords: list[dict[str, Any]] = field(default_factory=list)
    decorators: list[dict[str, Any]] = field(default_factory=list)


class PythonFlow:
    def __init__(self, inp: FlowInput, em: Emitter, facts: Any) -> None:
        self.inp, self.em, self.facts = inp, em, facts
        self.views: dict[tuple[str, str], ScopeView] = {}
        self.children: dict[tuple[str, str], list[str]] = {}
        for sc in inp.scopes:
            if sc.language != "Python":
                continue
            self.views[(sc.locator, sc.scope.key)] = ScopeView.of(sc)
            if sc.scope.parent_key is not None:
                self.children.setdefault((sc.locator, sc.scope.parent_key), []).append(sc.scope.key)
        self.classes: dict[tuple[str, str], _ClassInfo] = {}
        for (locator, key), view in self.views.items():
            scope = view.input.scope
            if scope.kind != "class_body":
                continue
            info = _ClassInfo(
                locator,
                key,
                scope.name or "",
                bases=list(scope.summary.get("bases") or []),
                keywords=list(scope.summary.get("keywords") or []),
                decorators=list(scope.summary.get("decorators") or []),
            )
            for child in self.children.get((locator, key), []):
                child_view = self.views.get((locator, child))
                if (
                    child_view
                    and child_view.input.scope.kind == "method"
                    and child_view.input.scope.name
                ):
                    info.methods[child_view.input.scope.name] = child
            self.classes[(locator, key)] = info
        self.resolver = ImportResolver(inp.locators)
        self._mro: dict[tuple[str, str], tuple[list[tuple[str, str]], bool]] = {}
        self._decorators: dict[tuple[str, str], list[str]] = {}
        # FastAPI bookkeeping for the response boundary: injected dependencies per dependant,
        # background registrations per scope, invocation targets per callsite (built lazily).
        self.injected: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.background: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {}
        self._invoked: dict[str, list[str]] | None = None
        self._handlers: list[tuple[str, str, str]] | None = None
        self._unlinked_handlers = False
        self._protocols: dict[tuple[str, str], bool] = {}
        self.by_identity: dict[UUID, ScopeView] = {}
        self.identity_of: dict[tuple[str, str], str] = {}
        owners = {s.id: s for s in inp.symbols}
        for view in self.views.values():
            owner = view.input.scope.owner_symbol_id
            if owner is not None and owner in owners and owners[owner].stable_identity_id:
                self.by_identity[owners[owner].stable_identity_id] = view  # type: ignore[index]
                self.identity_of[(view.input.locator, view.input.scope.key)] = str(
                    owners[owner].stable_identity_id
                )

    # ----------------------------------------------------------------- lookup

    def parent_view(self, view: ScopeView) -> ScopeView | None:
        parent = view.input.scope.parent_key
        return self.views.get((view.input.locator, parent)) if parent else None

    def lookup(self, view: ScopeView, name: str, node: int | None, depth: int = 0) -> list[Target]:
        if depth > 6:
            return [_unknown("RESOLUTION_DEPTH")]
        scope = view.input.scope
        if name in (scope.summary.get("global") or []):
            module = self.views.get((view.input.locator, "m"))
            return (
                self._bound(module, name, None, depth) if module else [_unknown("NAME_UNRESOLVED")]
            )
        if name in view.by_name:
            bindings = view.reaching_at(name, node) if node is not None else view.by_name[name]
            if not bindings:
                return [_unknown("UNBOUND_LOCAL")]
            return _conditional_import(
                bindings,
                self._merge(
                    [t for b in bindings for t in self.interpret(view, b, depth + 1)],
                    len(bindings),
                ),
            )
        cursor = self.parent_view(view)
        while cursor is not None:
            # Class bodies are not visible from nested functions (Python scoping).
            if cursor.input.scope.kind != "class_body":
                if name in cursor.by_name:
                    found = self._bound(cursor, name, None, depth)
                    if len(cursor.by_name[name]) > 1 and cursor.input.scope.kind != "module":
                        return [t.weaker("unknown", "CLOSURE_REBINDING") for t in found]
                    return found
            cursor = self.parent_view(cursor)
        if name in _BUILTINS:
            return [Target("builtin", name=name, tier="observed")]
        module = self.views.get((view.input.locator, "m"))
        if module is not None and module.input.scope.summary.get("star_imports"):
            return [_unknown("STAR_IMPORT")]
        return [_unknown("NAME_UNRESOLVED")]

    def _bound(
        self, view: ScopeView | None, name: str, node: int | None, depth: int
    ) -> list[Target]:
        if view is None or name not in view.by_name:
            return [_unknown("NAME_UNRESOLVED")]
        bindings = view.by_name[name] if node is None else view.reaching_at(name, node)
        return _conditional_import(
            bindings,
            self._merge(
                [t for b in bindings for t in self.interpret(view, b, depth + 1)], len(bindings)
            ),
        )

    @staticmethod
    def _merge(targets: list[Target], definitions: int) -> list[Target]:
        unique: dict[tuple[Any, ...], Target] = {}
        for t in targets:
            unique.setdefault((t.kind, t.locator, t.scope, t.name), t)
        values = list(unique.values())
        if len(values) > 1 or definitions > 1 and any(t.kind == "unknown" for t in values):
            values = [t.weaker("inferred", "MULTIPLE_REACHING_DEFINITIONS") for t in values]
        return values[: FANOUT + 1]

    def interpret(self, view: ScopeView, binding: dict[str, Any], depth: int) -> list[Target]:
        kind, value = binding["kind"], binding.get("value") or {}
        locator = view.input.locator
        if kind in {"def", "class"}:
            key = value.get("key")
            if key is None:
                return [_unknown("SCOPE_NOT_PLANNED")]
            return [Target("class" if (locator, key) in self.classes else "scope", locator, key)]
        if kind == "import":
            return self.import_target(locator, value, depth, binding["name"])
        if kind == "param":
            return [self.param_target(view, int(value.get("index", 0)), depth)]
        if kind == "with_as":
            entered = value.get("from") or {}
            if entered.get("k") == "call":
                made = self.call_result(view, entered, binding["node"], depth)
                if any(t.kind == "context_manager" for t in made):
                    return [_unknown("YIELDED_VALUE")]
                return [t.weaker("inferred", "CONTEXT_ENTER_RETURNS_INSTANCE") for t in made]
            return [_unknown("CONTEXT_VALUE_UNKNOWN")]
        if kind == "annotation":
            typed = self.type_of(view, (value.get("shape") or {}), depth)
            return (
                [t.weaker("inferred", "ANNOTATION_TRUSTED") for t in typed]
                if typed
                else [_unknown("ANNOTATION_UNRESOLVED")]
            )
        if kind in {"assign", "annassign", "walrus"}:
            return self.value_targets(view, value, binding["node"], depth)
        return [_unknown("VALUE_KIND_" + kind.upper())]

    def value_targets(
        self, view: ScopeView, value: dict[str, Any], node: int | None, depth: int
    ) -> list[Target]:
        k = value.get("k")
        if k == "name":
            return self.lookup(view, value["id"], node, depth + 1)
        if k == "attr":
            return self.attribute(view, value, node, depth + 1, final=False)
        if k == "scope":
            key = value.get("key")
            return (
                [Target("scope", view.input.locator, key)]
                if key
                else [_unknown("SCOPE_NOT_PLANNED")]
            )
        if k == "call":
            return self.call_result(view, value, node, depth)
        if k == "await":
            return [_unknown("AWAITED_VALUE")]
        return [_unknown("VALUE_" + str(k).upper())]

    def call_result(
        self, view: ScopeView, shape: dict[str, Any], node: int | None, depth: int
    ) -> list[Target]:
        """The value of ``callee(...)``: an instance when the callee is a class."""
        results: list[Target] = []
        for t in self.callee_targets(view, shape.get("callee") or {}, node, depth + 1):
            if t.kind == "class":
                results.append(
                    Target("instance", t.locator, t.scope, tier=t.tier, conditions=t.conditions)
                )
            elif t.kind == "external":
                results.append(
                    Target(
                        "ext_instance",
                        name=_external_result(t.name or ""),
                        tier="inferred",
                        conditions=t.conditions + ("EXTERNAL_CALL_RESULT",),
                    )
                )
            elif t.kind == "scope" and t.locator and t.scope:
                made = self.views.get((t.locator, t.scope))
                kind = made.input.scope.callable_kind if made else None
                if made is not None and set(self.decorator_names(made)) & _CONTEXT_MANAGERS:
                    results.append(
                        Target(
                            "context_manager",
                            t.locator,
                            t.scope,
                            tier=t.tier,
                            conditions=t.conditions,
                        )
                    )
                elif kind in {"generator_function", "async_generator_function"}:
                    results.append(
                        Target(
                            "generator",
                            t.locator,
                            t.scope,
                            name=kind,
                            tier=t.tier,
                            conditions=t.conditions,
                        )
                    )
                else:
                    results.append(_unknown("CALL_RESULT_UNKNOWN"))
            elif t.kind == "ext_instance" and (t.name or "").endswith("sessionmaker"):
                results.append(
                    Target(
                        "ext_instance",
                        name="sqlalchemy.orm.Session",
                        tier="inferred",
                        conditions=t.conditions + ("SESSIONMAKER_CALL_RETURNS_SESSION",),
                    )
                )
            elif t.kind == "method_of_ext" and t.name and t.name.endswith("sessionmaker.begin"):
                results.append(
                    Target(
                        "ext_instance",
                        name="sqlalchemy.orm.Session",
                        tier="inferred",
                        conditions=t.conditions + ("SESSION_BEGIN_CONTEXT",),
                    )
                )
            else:
                results.append(_unknown("CALL_RESULT_UNKNOWN"))
        return results or [_unknown("CALL_RESULT_UNKNOWN")]

    # ----------------------------------------------------------------- imports and modules

    def import_target(
        self, locator: str, value: dict[str, Any], depth: int, bound: str | None = None
    ) -> list[Target]:
        module, name, level = (
            value.get("module") or "",
            value.get("name"),
            int(value.get("level") or 0),
        )
        row = (
            {"form": "from", "from_module": module, "name": name, "level": level}
            if name is not None
            else {"form": "import", "name": module, "level": 0}
        )
        resolved = self.resolver.python(locator, row)
        if resolved.status == "external":
            qual = f"{module}.{name}" if name else module
            return [
                Target(
                    "external",
                    name=qual,
                    tier="derived",
                    conditions=(f"EXTERNAL_PACKAGE:{module.split('.')[0]}",),
                )
            ]
        if resolved.status != "resolved" or resolved.locator is None:
            return [_unknown(f"IMPORT_{resolved.status.upper()}")]
        if name is None or resolved.submodule:
            if name is None and "." in module and bound == module.split(".")[0]:
                # ``import a.b`` binds ``a``; attribute access walks submodules.
                head = self.resolver.python(
                    locator, {"form": "import", "name": module.split(".")[0], "level": 0}
                )
                if head.status == "resolved" and head.locator:
                    return [Target("module", head.locator)]
            return [Target("module", resolved.locator)]
        return self.module_member(resolved.locator, name, depth + 1)

    def module_member(self, locator: str, name: str, depth: int) -> list[Target]:
        if depth > 6:
            return [_unknown("RESOLUTION_DEPTH")]
        module = self.views.get((locator, "m"))
        if module is not None and name in module.by_name:
            return self._bound(module, name, None, depth)
        package = locator.rsplit("/", 1)[0] if locator.endswith("/__init__.py") else None
        if package is not None:
            for candidate in (f"{package}/{name}.py", f"{package}/{name}/__init__.py"):
                if candidate in self.inp.locators:
                    return [Target("module", candidate)]
        if module is None:
            return [_unknown("MODULE_NOT_EXTRACTED")]
        if module.input.scope.summary.get("star_imports"):
            return [_unknown("STAR_IMPORT")]
        return [_unknown("NAME_NOT_FOUND_IN_MODULE")]

    # ----------------------------------------------------------------- types and classes

    def type_of(self, view: ScopeView, shape: dict[str, Any], depth: int) -> list[Target]:
        """Instance targets for an annotation (``X``, ``m.X``, ``Annotated[X, …]``, ``Optional[X]``)."""  # noqa: E501
        k = shape.get("k")
        if k == "subscript":
            head = shape.get("value") or {}
            head_name = (
                head.get("id") if head.get("k") == "name" else (head.get("path") or [None])[-1]
            )
            index = shape.get("index") or []
            if head_name in {"Annotated", "Optional"} and index:
                return self.type_of(view, index[0], depth + 1)
            # Generic external types keep their head (``sessionmaker[Session]``).
            return [t for t in self.type_of(view, head, depth + 1)]
        if k in {"name", "attr"}:
            found = self.callee_targets(view, shape, None, depth + 1)
            values: list[Target] = []
            for t in found:
                if t.kind == "class":
                    values.append(
                        Target("instance", t.locator, t.scope, tier=t.tier, conditions=t.conditions)
                    )
                elif t.kind == "external":
                    values.append(
                        Target("ext_instance", name=t.name, tier=t.tier, conditions=t.conditions)
                    )
            return values
        return []

    def param_target(self, view: ScopeView, index: int, depth: int) -> Target:
        scope = view.input.scope
        params = view.parameters
        if index >= len(params):
            return _unknown("PARAMETER_UNKNOWN")
        param = params[index]
        if index == 0 and scope.kind == "method":
            parent = scope.parent_key
            if parent and (view.input.locator, parent) in self.classes:
                decorators = {_tail(d) for d in scope.summary.get("decorators") or []}
                if "staticmethod" in decorators:
                    pass
                elif "classmethod" in decorators:
                    return Target("class", view.input.locator, parent)
                else:
                    return Target("instance", view.input.locator, parent)
        annotation = param.get("annotation")
        if annotation:
            typed = self.type_of(view, annotation, depth)
            if typed:
                return typed[0].weaker("inferred", "ANNOTATION_TRUSTED")
        default = param.get("default") or {}
        provider = self.depends_provider(view, default)
        if provider is not None:
            outer = self.parent_view(view) or view
            for t in self.callee_targets(outer, provider, None, depth + 1):
                provided = self.provider_type(t, depth)
                if provided is not None:
                    return provided.weaker("inferred", "DEPENDS_PROVIDER_RETURN_TYPE")
        return Target(
            "param",
            view.input.locator,
            scope.key,
            name=param.get("name"),
            tier="unknown",
            conditions=("CALLABLE_PARAMETER",),
        )

    def provider_type(self, provider: Target, depth: int) -> Target | None:
        view = (
            self.views.get((provider.locator or "", provider.scope or ""))
            if provider.kind == "scope"
            else None
        )
        if view is None:
            return None
        for shape in view.input.scope.summary.get("returns") or []:
            if shape.get("k") == "call":
                for t in self.call_result(view, shape, None, depth + 1):
                    if t.kind in {"instance", "ext_instance"}:
                        return t
        return None

    def depends_provider(self, view: ScopeView, default: dict[str, Any]) -> dict[str, Any] | None:
        """The provider shape of ``Depends(p)``/``Security(p)`` (the callee bound to FastAPI)."""
        if default.get("k") != "call":
            return None
        if _tail(default.get("callee") or {}) not in {"Depends", "Security"}:
            return None
        parent = self.parent_view(view) or view
        bound = self.callee_targets(parent, default.get("callee") or {}, None, 0)
        if not any(t.kind == "external" and (t.name or "").startswith("fastapi") for t in bound):
            return None
        args = default.get("args") or []
        if args:
            return dict(args[0])
        for kw in default.get("kwargs") or []:
            if kw.get("kw") == "dependency":
                return dict(kw.get("shape") or {})
        return {"k": "class_dependency"}

    def mro(self, cls: tuple[str, str]) -> tuple[list[tuple[str, str]], bool]:
        """C3 linearisation over resolved repository bases; ``complete`` is False when a base is
        external, builtin other than ``object``, or unresolved (``MRO_INCOMPLETE``)."""
        if cls in self._mro:
            return self._mro[cls]
        self._mro[cls] = ([cls], False)  # cycle guard
        info = self.classes.get(cls)
        if info is None:
            return [cls], False
        defining = self.views.get(
            (info.locator, self.views[(info.locator, info.key)].input.scope.parent_key or "m")
        )
        complete = True
        base_mros: list[list[tuple[str, str]]] = []
        for base in info.bases:
            targets = self.callee_targets(defining, base, None, 1) if defining else []
            classes = [t for t in targets if t.kind == "class" and t.locator and t.scope]
            if len(classes) == 1:
                sub, ok = self.mro((classes[0].locator, classes[0].scope))  # type: ignore[arg-type]
                base_mros.append(list(sub))
                complete = complete and ok
            elif not (
                len(targets) == 1 and targets[0].kind == "builtin" and targets[0].name == "object"
            ):
                complete = False
        merged = _c3([cls], base_mros + [[m[0] for m in base_mros]] if base_mros else [])
        result = (merged if merged is not None else [cls], complete and merged is not None)
        self._mro[cls] = result
        return result

    def method(self, cls: tuple[str, str], name: str) -> tuple[Target | None, bool]:
        order, complete = self.mro(cls)
        for item in order:
            info = self.classes.get(item)
            if info and name in info.methods:
                tier = "derived" if item == cls or complete else "inferred"
                conditions = () if item == cls or complete else ("MRO_INCOMPLETE",)
                return Target(
                    "scope", item[0], info.methods[name], tier=tier, conditions=conditions
                ), complete
        return None, complete

    def subclasses(self, cls: tuple[str, str]) -> list[tuple[str, str]]:
        return [other for other in self.classes if other != cls and cls in self.mro(other)[0]]

    def field_type(self, cls: tuple[str, str], attribute: str, depth: int) -> list[Target]:
        """``self.attr`` types from stores in the class's methods (``__init__`` first)."""
        order, _ = self.mro(cls)
        for item in order:
            info = self.classes.get(item)
            if info is None:
                continue
            for method_name in sorted(info.methods, key=lambda n: n != "__init__"):
                view = self.views.get((item[0], info.methods[method_name]))
                if view is None:
                    continue
                stores = view.by_name.get(f"self.{attribute}") or []
                for store in stores:
                    found = self.value_targets(
                        view, store.get("value") or {}, store["node"], depth + 1
                    )
                    typed = [
                        t
                        for t in found
                        if t.kind in {"instance", "ext_instance", "class", "scope", "module"}
                    ]
                    if typed:
                        return [t.weaker("inferred", "FIELD_TYPE_FROM_ASSIGNMENT") for t in typed]
        return [_unknown("RECEIVER_TYPE_UNKNOWN")]

    # ----------------------------------------------------------------- callees

    def callee_targets(
        self, view: ScopeView, shape: dict[str, Any], node: int | None, depth: int = 0
    ) -> list[Target]:
        k = shape.get("k")
        if k == "name":
            return self.lookup(view, shape["id"], node, depth)
        if k == "attr":
            return self.attribute(view, shape, node, depth, final=True)
        if k == "call":
            return [_unknown("CALL_RESULT_RECEIVER")]
        if k == "subscript":
            return [_unknown("SUBSCRIPT_CALLEE")]
        return [_unknown("DYNAMIC_CALLEE")]

    def attribute(
        self, view: ScopeView, shape: dict[str, Any], node: int | None, depth: int, *, final: bool
    ) -> list[Target]:
        root = shape.get("root") or {}
        path = list(shape.get("path") or [])
        if root.get("k") == "call" and _tail(root.get("callee") or {}) == "super":
            current = self._super(view)
        elif root.get("k") == "name":
            current = self.lookup(view, root["id"], node, depth)
        elif root.get("k") == "call":
            current = self.call_result(view, root, node, depth)
        else:
            return [
                _unknown("SUBSCRIPT_CALLEE" if root.get("k") == "subscript" else "DYNAMIC_RECEIVER")
            ]
        for index, attribute in enumerate(path):
            last = index == len(path) - 1
            nxt: list[Target] = []
            for t in current:
                nxt.extend(self._member(t, attribute, last and final, depth))
            current = self._merge(nxt, 1)
        return current

    def _super(self, view: ScopeView) -> list[Target]:
        cursor: ScopeView | None = view
        while cursor is not None and cursor.input.scope.kind != "method":
            cursor = self.parent_view(cursor)
        if cursor is None or not cursor.input.scope.parent_key:
            return [_unknown("SUPER_OUTSIDE_METHOD")]
        cls = (cursor.input.locator, cursor.input.scope.parent_key)
        order, complete = self.mro(cls)
        if len(order) < 2:
            return [_unknown("MRO_INCOMPLETE")]
        nxt = order[1]
        return [
            Target(
                "instance",
                nxt[0],
                nxt[1],
                conditions=() if complete else ("MRO_INCOMPLETE",),
                tier="derived" if complete else "inferred",
            )
        ]

    def _member(self, t: Target, attribute: str, final: bool, depth: int) -> list[Target]:
        if t.kind == "module" and t.locator:
            return [
                x.weaker(t.tier, *t.conditions)
                for x in self.module_member(t.locator, attribute, depth + 1)
            ]
        if t.kind in {"class", "instance"} and t.locator and t.scope:
            cls = (t.locator, t.scope)
            found, complete = self.method(cls, attribute)
            if found is not None:
                return [found.weaker(t.tier, *t.conditions)]
            info = self.classes.get(cls)
            if info is not None and t.kind == "instance" and not final:
                return [
                    x.weaker(t.tier, *t.conditions) for x in self.field_type(cls, attribute, depth)
                ]
            if info is not None and t.kind == "instance" and final:
                typed = self.field_type(cls, attribute, depth)
                if any(x.kind in {"scope"} for x in typed):
                    return typed
            return [_unknown("MRO_INCOMPLETE" if not complete else "ATTRIBUTE_NOT_FOUND")]
        if t.kind == "external":
            return [
                Target(
                    "external", name=f"{t.name}.{attribute}", tier=t.tier, conditions=t.conditions
                )
            ]
        if t.kind == "ext_instance":
            return [
                Target(
                    "method_of_ext",
                    name=f"{t.name}.{attribute}",
                    tier=t.tier,
                    conditions=t.conditions,
                )
            ]
        if t.kind == "generator" and attribute in _GENERATOR_METHODS:
            return [replace(t, kind="generator_method", name=attribute)]
        if t.kind == "param":
            return [_unknown("RECEIVER_TYPE_UNKNOWN")]
        if t.kind == "unknown":
            return [t]
        return [_unknown("RECEIVER_TYPE_UNKNOWN")]

    # ----------------------------------------------------------------- emission

    def run(self) -> None:
        for (locator, key), view in sorted(self.views.items()):
            sc = view.input
            node = self.em.node(
                scope_key(locator, key),
                "function" if sc.scope.kind in {"function", "method"} else "scope",
                sc.scope.name or sc.scope.kind,
                locator,
                {
                    "kind": sc.scope.kind,
                    "callable_kind": sc.scope.callable_kind,
                    "scope": key,
                    "truncated": sc.scope.truncated,
                    "identity": self.identity_of.get((locator, key)),
                    "language": "Python",
                },
                [self.em.scope_ev(sc)],
            )
            if sc.scope.truncated:
                self.em.diagnose("FLOW_SCOPE_TRUNCATED", node, limit=sc.scope.truncated)
            if sc.scope.kind == "module":
                self.em.entry("module", locator, locator, node, locator, [self.em.scope_ev(sc)])
            elif sc.scope.kind in {"function", "method"} and any(
                name.startswith(("typer.", "click.")) and name.rsplit(".", 1)[-1] in _CLI_TAILS
                for name in self.decorator_names(view)
            ):
                self.em.entry(
                    "cli_command",
                    f"{locator}#{key}",
                    sc.scope.name or key,
                    node,
                    locator,
                    [self.em.scope_ev(sc)],
                )
            for callsite in sc.scope.callsites:
                self.callsite(view, callsite)
            self.iteration(view)
            self.managers(view)
            self.transactions(view)
            for cfg_node, executed in sc.scope.summary.get("executes") or []:
                if executed:
                    self.em.edge(
                        "invokes",
                        node,
                        scope_key(locator, executed),
                        "observed",
                        RULES["py_async"],
                        (),
                        {"phase": "definition", "cfg_node": cfg_node},
                        [self.em.scope_ev(sc)],
                    )

    def callsite(self, view: ScopeView, callsite: FlowCallsite) -> None:
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
                "language": "Python",
            },
            [ev],
        )
        em.edge(
            "contains_callsite",
            scope_key(sc.locator, sc.scope.key),
            key,
            "observed",
            RULES["py_resolution"],
            (),
            {"ordinal": callsite.ordinal},
            [ev],
        )
        targets = self.callee_targets(view, callsite.callee, callsite.cfg_node)
        if len(targets) > FANOUT:
            em.edge(
                "unresolved",
                key,
                None,
                "unknown",
                RULES["py_resolution"],
                ("DISPATCH_FANOUT",),
                {},
                [ev],
            )
            return
        for t in targets:
            self.emit_target(view, callsite, key, ev, t)
        self.frameworks(view, callsite, key, ev, targets)

    def emit_target(
        self, view: ScopeView, callsite: FlowCallsite, key: str, ev: str, t: Target
    ) -> None:
        em = self.em
        rule = RULES["py_resolution"]
        if t.kind == "scope" and t.locator and t.scope:
            target = scope_key(t.locator, t.scope)
            callee = self.views.get((t.locator, t.scope))
            if callee is not None and not self._transparent(callee):
                t = t.weaker("inferred", "DECORATOR_MAY_WRAP")
            em.edge("resolves_to", key, target, t.tier, rule, t.conditions, {}, [ev])
            relation = _invocation(
                callee.input.scope.callable_kind if callee else "sync_function", callsite.awaited
            )
            attributes: dict[str, Any] = {"awaited": callsite.awaited}
            conditions = t.conditions
            if callee is not None and relation in {"invokes", "awaits"}:
                returns = self._never_returns(callee)
                if returns is not None:
                    attributes["normal_continuation"] = False
                    conditions = tuple(sorted({*conditions, returns}))
                if self._protocol_method(callee):
                    conditions = tuple(sorted({*conditions, "PROTOCOL_METHOD_STUB"}))
            em.edge(
                relation,
                key,
                target,
                t.tier,
                RULES["py_async"],
                conditions,
                attributes,
                [ev],
            )
            self.callbacks(view, callsite, key, ev, callee)
            if (
                t.tier != "unknown"
                and callee
                and callee.input.scope.kind == "method"
                and callsite.callee.get("k") == "attr"
            ):
                self.dispatch(callsite, key, ev, t)
                self.protocol_candidates(key, ev, callee)
        elif t.kind == "generator_method" and t.locator and t.scope:
            relation, thrown = _GENERATOR_METHODS[t.name or ""]
            em.edge(
                relation,
                key,
                scope_key(t.locator, t.scope),
                t.tier,
                RULES["py_async"],
                tuple(sorted({*t.conditions, "GENERATOR_STATE_NOT_TRACKED"})),
                {"method": t.name, **({"exception": thrown} if thrown else {})},
                [ev],
            )
        elif t.kind == "class" and t.locator and t.scope:
            self.construct(callsite, key, ev, t)
        elif t.kind in {"external", "method_of_ext"}:
            package = (t.name or "?").split(".")[0]
            boundary = em.node(
                f"external:{package}", "integration_boundary", package, None, {"package": package}
            )
            em.edge(
                "unresolved",
                key,
                None,
                "unknown",
                rule,
                (f"EXTERNAL_PACKAGE:{package}",),
                {"qualified": t.name},
                [ev],
            )
            process_exit = t.kind == "external" and t.name in {"sys.exit", "os._exit"}
            em.edge(
                "crosses_boundary",
                key,
                boundary,
                "derived" if t.kind == "external" else "inferred",
                rule,
                (*t.conditions, *(("PROCESS_EXIT",) if process_exit else ())),
                {"qualified": t.name, **({"normal_continuation": False} if process_exit else {})},
                [ev],
            )
        elif t.kind == "builtin":
            em.edge(
                "unresolved", key, None, "unknown", rule, ("BUILTIN",), {"builtin": t.name}, [ev]
            )
            if t.name in {"next", "anext"}:
                first = next((a for a in callsite.arguments if a.get("pos") == 0), None)
                for g in self.value_targets(
                    view, (first or {}).get("shape") or {}, callsite.cfg_node, 0
                ):
                    if g.kind == "generator" and g.locator and g.scope:
                        em.edge(
                            "resumes",
                            key,
                            scope_key(g.locator, g.scope),
                            g.tier,
                            RULES["py_async"],
                            tuple(sorted({*g.conditions, "GENERATOR_STATE_NOT_TRACKED"})),
                            {"method": t.name},
                            [ev],
                        )
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

    def dispatch(self, callsite: FlowCallsite, key: str, ev: str, t: Target) -> None:
        callee = self.views.get((t.locator or "", t.scope or ""))
        if callee is None or not callee.input.scope.parent_key or not callee.input.scope.name:
            return
        cls = (callee.input.locator, callee.input.scope.parent_key)
        candidates: list[Target] = []
        for sub in self.subclasses(cls):
            info = self.classes[sub]
            if callee.input.scope.name in info.methods:
                candidates.append(Target("scope", sub[0], info.methods[callee.input.scope.name]))
        if len(candidates) > FANOUT:
            self.em.edge(
                "unresolved",
                key,
                None,
                "unknown",
                RULES["py_resolution"],
                ("DISPATCH_FANOUT",),
                {},
                [ev],
            )
            return
        for c in candidates:
            self.em.edge(
                "dispatch_candidate",
                key,
                scope_key(c.locator or "", c.scope or ""),
                "inferred",
                RULES["py_resolution"],
                ("VIRTUAL_DISPATCH_CANDIDATES",),
                {},
                [ev],
            )

    def callbacks(
        self, view: ScopeView, callsite: FlowCallsite, key: str, ev: str, callee: ScopeView | None
    ) -> None:
        if callee is None:
            return
        indexes = set(callee.input.scope.summary.get("invokes_parameter") or [])
        if not indexes:
            return
        names = {p["index"]: p["name"] for p in callee.parameters}
        offset = (
            1 if callee.input.scope.kind == "method" and callsite.callee.get("k") == "attr" else 0
        )
        for argument in callsite.arguments:
            index = argument.get("pos")
            matched = (index is not None and index + offset in indexes) or (
                argument.get("kw") and any(names.get(i) == argument.get("kw") for i in indexes)
            )
            if not matched:
                continue
            for t in self.callee_targets(view, argument.get("shape") or {}, callsite.cfg_node):
                if t.kind == "scope" and t.locator and t.scope:
                    self.em.edge(
                        "invokes_callback",
                        key,
                        scope_key(t.locator, t.scope),
                        "inferred",
                        RULES["py_resolution"],
                        ("CALLBACK_INVOKED_BY_CALLEE", *t.conditions),
                        {},
                        [ev],
                    )

    def construct(self, callsite: FlowCallsite, key: str, ev: str, cls_t: Target) -> None:
        em, rule = self.em, RULES["py_construction"]
        cls = (cls_t.locator or "", cls_t.scope or "")
        info = self.classes[cls]
        target = scope_key(*cls)
        em.edge(
            "resolves_to",
            key,
            target,
            cls_t.tier,
            RULES["py_resolution"],
            cls_t.conditions,
            {},
            [ev],
        )
        defining = self.views.get((info.locator, self.views[cls].input.scope.parent_key or "m"))
        if not self._transparent(self.views[cls]):
            em.edge("constructs", key, target, "inferred", rule, ("DECORATOR_MAY_WRAP",), {}, [ev])
        for kw in info.keywords:
            if kw.get("kw") == "metaclass":
                meta = (
                    self.callee_targets(defining, kw.get("shape") or {}, None) if defining else []
                )
                calls = [
                    self.method((m.locator or "", m.scope or ""), "__call__")[0]
                    for m in meta
                    if m.kind == "class"
                ]
                if any(c is not None for c in calls):
                    em.edge(
                        "constructs",
                        key,
                        target,
                        "unknown",
                        rule,
                        ("METACLASS_CALL_OVERRIDE",),
                        {},
                        [ev],
                    )
                    for c in calls:
                        if c is not None:
                            em.edge(
                                "dispatch_candidate",
                                key,
                                scope_key(c.locator or "", c.scope or ""),
                                "inferred",
                                rule,
                                ("METACLASS_CALL_OVERRIDE",),
                                {},
                                [ev],
                            )
                    return
                if not (len(meta) == 1 and meta[0].kind == "builtin"):
                    em.edge(
                        "constructs",
                        key,
                        target,
                        "inferred",
                        rule,
                        ("METACLASS_NOT_RESOLVED",),
                        {},
                        [ev],
                    )
        order, complete = self.mro(cls)
        base_names = {_tail(b) for b in info.bases}
        if base_names & {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}:
            em.edge("constructs", key, target, "derived", rule, ("ENUM_CALL_IS_LOOKUP",), {}, [ev])
            return
        if (
            base_names & {"BaseModel", "BaseSettings", "SQLModel"}
            or not complete
            and _framework_base(info)
        ):
            em.edge(
                "constructs",
                key,
                target,
                "inferred",
                rule,
                ("CONSTRUCTION_FRAMEWORK_NOT_MODELLED",),
                {},
                [ev],
            )
        new, _ = self.method(cls, "__new__")
        init, _ = self.method(cls, "__init__")
        other = new is not None and self._new_returns_other(new, cls)
        em.edge(
            "constructs",
            key,
            target,
            cls_t.tier if complete and not other else "inferred",
            rule,
            (
                *(() if complete else ("MRO_INCOMPLETE",)),
                *(("NEW_RETURNS_OTHER_TYPE",) if other else ()),
            ),
            {"init_skipped": True} if other else {},
            [ev],
        )
        if new is not None:
            em.edge(
                "invokes",
                key,
                scope_key(new.locator or "", new.scope or ""),
                new.tier,
                rule,
                new.conditions,
                {"phase": "__new__"},
                [ev],
            )
        if other:
            return
        if init is not None:
            conditions = list(init.conditions)
            tier = init.tier
            if new is not None and not self._new_returns_super(new):
                conditions.append("INIT_CONDITIONAL_ON_NEW_RESULT")
                tier = "inferred"
            em.edge(
                "invokes",
                key,
                scope_key(init.locator or "", init.scope or ""),
                tier,
                rule,
                tuple(conditions),
                {"phase": "__init__"},
                [ev],
            )
            return
        generated = self._dataclass_init(info, defining)
        if generated:
            post, _ = self.method(cls, "__post_init__")
            if post is not None:
                em.edge(
                    "invokes",
                    key,
                    scope_key(post.locator or "", post.scope or ""),
                    "inferred",
                    rule,
                    ("DATACLASS_GENERATED_INIT",),
                    {"phase": "__post_init__"},
                    [ev],
                )

    # ----------------------------------------------------------------- decorators and protocols

    def decorator_names(self, view: ScopeView) -> list[str]:
        """Qualified names of a scope's decorators (``?`` when a decorator does not resolve)."""
        cached = self._decorators.get((view.input.locator, view.input.scope.key))
        if cached is not None:
            return cached
        names: list[str] = []
        defining = self.parent_view(view)
        for decorator in view.input.scope.summary.get("decorators") or []:
            shape = decorator.get("callee") if decorator.get("k") == "call" else decorator
            shape = shape or {}
            if shape.get("k") == "attr" and (shape.get("path") or [None])[-1] in {
                "setter",
                "getter",
                "deleter",
            }:
                names.append("property")
                continue
            targets = self.callee_targets(defining, shape, None) if defining else []
            resolved = [
                t.name or ""
                for t in targets
                if t.kind in {"external", "method_of_ext", "builtin"} and t.name
            ]
            names.append(resolved[0] if len(resolved) == 1 == len(targets) else "?")
        self._decorators[(view.input.locator, view.input.scope.key)] = names
        return names

    def _transparent(self, view: ScopeView) -> bool:
        for name in self.decorator_names(view):
            if name in _TRANSPARENT_DECORATORS or name in _CONTEXT_MANAGERS:
                continue
            if name.startswith(_REGISTRATION_PREFIXES) and (
                name.rsplit(".", 1)[-1] in _REGISTRATION_TAILS or name.startswith("pytest.mark")
            ):
                continue
            return False
        return True

    def _never_returns(self, callee: ScopeView) -> str | None:
        summary = callee.input.scope.summary
        annotation = summary.get("return_annotation")
        defining = self.parent_view(callee)
        if annotation and defining is not None:
            for t in self.callee_targets(defining, annotation, None):
                if t.kind == "external" and t.name in _NORETURN:
                    return "NORETURN_ANNOTATION_TRUSTED"
        if summary.get("normal_exit_reachable") is False and not summary.get("truncated"):
            return "CALLEE_NEVER_RETURNS"
        return None

    def _is_protocol(self, cls: tuple[str, str]) -> bool:
        if cls not in self._protocols:
            info = self.classes.get(cls)
            defining = (
                self.views.get((info.locator, self.views[cls].input.scope.parent_key or "m"))
                if info
                else None
            )
            self._protocols[cls] = bool(
                info
                and defining
                and any(
                    t.kind == "external"
                    and t.name in {"typing.Protocol", "typing_extensions.Protocol"}
                    for base in info.bases
                    for t in self.callee_targets(defining, base, None)
                )
            )
        return self._protocols[cls]

    def _protocol_method(self, callee: ScopeView) -> bool:
        parent = callee.input.scope.parent_key
        return (
            callee.input.scope.kind == "method"
            and parent is not None
            and self._is_protocol((callee.input.locator, parent))
        )

    def protocol_candidates(self, key: str, ev: str, callee: ScopeView | None) -> None:
        """Structural implementations of a ``Protocol`` method: repository classes defining every
        public method of the protocol (``PROTOCOL_IMPLEMENTATION_CANDIDATES``, capped)."""
        if callee is None or not self._protocol_method(callee):
            return
        protocol = (callee.input.locator, callee.input.scope.parent_key or "")
        required = {m for m in self.classes[protocol].methods if not m.startswith("_")}
        name = callee.input.scope.name or ""
        candidates = [
            (cls, info.methods[name])
            for cls, info in sorted(self.classes.items())
            if cls != protocol
            and not self._is_protocol(cls)
            and name in info.methods
            and required <= set(info.methods)
        ]
        if len(candidates) > FANOUT:
            self.em.edge(
                "unresolved",
                key,
                None,
                "unknown",
                RULES["py_resolution"],
                ("DISPATCH_FANOUT", "PROTOCOL_IMPLEMENTATION_CANDIDATES"),
                {},
                [ev],
            )
            return
        for cls, method in candidates:
            self.em.edge(
                "dispatch_candidate",
                key,
                scope_key(cls[0], method),
                "inferred",
                RULES["py_resolution"],
                ("PROTOCOL_IMPLEMENTATION_CANDIDATES",),
                {},
                [ev],
            )

    # ----------------------------------------------------------------- generators and managers

    def iteration(self, view: ScopeView) -> None:
        """``for x in g`` over a generator iterator: ``iterates`` (resumptions until exhaustion
        or ``break``; generator finalisation is not modelled)."""
        sc = view.input
        for binding in view.bindings:
            value = binding.get("value") or {}
            if binding["kind"] != "for_target" or value.get("k") != "iter_item":
                continue
            source = value.get("from") or {}
            for g in self.value_targets(view, source, binding["node"], 0):
                if g.kind != "generator" or not g.locator or not g.scope:
                    continue
                ordinal = source.get("callsite") if source.get("k") == "call" else None
                origin = (
                    call_key(sc.locator, sc.scope.key, int(ordinal))
                    if ordinal is not None
                    else scope_key(sc.locator, sc.scope.key)
                )
                callsite = next((c for c in sc.scope.callsites if c.ordinal == ordinal), None)
                self.em.edge(
                    "iterates",
                    origin,
                    scope_key(g.locator, g.scope),
                    g.tier,
                    RULES["py_async"],
                    tuple(sorted({*g.conditions, "GENERATOR_FINALIZATION_NOT_MODELLED"})),
                    {"async": bool(value.get("async")), "cfg_node": binding["node"]},
                    [self.em.callsite_ev(sc, callsite) if callsite else self.em.scope_ev(sc)],
                    "" if ordinal is not None else f"node:{binding['node']}",
                )

    def managers(self, view: ScopeView) -> None:
        """``with m(...)`` where ``m`` is a ``@contextmanager`` generator: entering resumes the
        body to its ``yield``; leaving resumes it normally or throws into it, so the generator's
        own ``except``/``finally`` decide suppression (``SUPPRESSION_DECIDED_BY_GENERATOR``)."""
        sc = view.input
        for callsite, _ in self.with_items(view):
            key = call_key(sc.locator, sc.scope.key, callsite.ordinal)
            for t in self.callee_targets(view, callsite.callee, callsite.cfg_node):
                made = (
                    self.views.get((t.locator or "", t.scope or "")) if t.kind == "scope" else None
                )
                if made is None or not set(self.decorator_names(made)) & _CONTEXT_MANAGERS:
                    continue
                target = scope_key(made.input.locator, made.input.scope.key)
                ev = self.em.callsite_ev(sc, callsite)
                for phase, attributes in (
                    ("enter", {"phase": "enter", "until": "first_yield"}),
                    (
                        "exit",
                        {
                            "phase": "exit",
                            "normal": "resume_normal",
                            "exceptional": "resume_throw",
                        },
                    ),
                ):
                    self.em.edge(
                        "resumes",
                        key,
                        target,
                        t.tier,
                        RULES["py_async"],
                        tuple(
                            sorted(
                                {
                                    *t.conditions,
                                    "CONTEXTMANAGER_GENERATOR",
                                    *(
                                        ("SUPPRESSION_DECIDED_BY_GENERATOR",)
                                        if phase == "exit"
                                        else ()
                                    ),
                                }
                            )
                        ),
                        attributes,
                        [ev],
                        phase,
                    )

    def transactions(self, view: ScopeView) -> None:
        """``with session.begin()`` / ``begin_nested()`` / ``sessionmaker.begin()``: a
        ``transaction_boundary`` that commits on normal and rolls back on exceptional completion
        of the body (CFG completion kinds). Body operations on the **same session identity**
        (the same reaching definitions of the receiver, or the ``as`` value of this item) get
        ``in_transaction``; others get no claim."""
        sc, em, rule = view.input, self.em, RULES["sqlalchemy"]
        for callsite, statement in self.with_items(view):
            targets = self.callee_targets(view, callsite.callee, callsite.cfg_node)
            begins = [
                t
                for t in targets
                if t.kind == "method_of_ext"
                and (t.name or "").startswith("sqlalchemy")
                and (t.name or "").rsplit(".", 1)[-1] in {"begin", "begin_nested"}
            ]
            if not begins:
                continue
            key = call_key(sc.locator, sc.scope.key, callsite.ordinal)
            ev = em.callsite_ev(sc, callsite)
            nested = (begins[0].name or "").endswith("begin_nested")
            boundary = em.node(
                f"txn:{key}",
                "transaction_boundary",
                "begin_nested()" if nested else "begin()",
                sc.locator,
                {
                    "nested": nested,
                    "commit_on": "normal_completion",
                    "rollback_on": "exceptional_completion",
                },
                [ev],
            )
            em.edge(
                "performs",
                key,
                boundary,
                "inferred",
                rule,
                tuple(sorted({*begins[0].conditions, "TRANSACTION_SCOPE"})),
                {"class": "transaction_scope"},
                [ev],
            )
            root = callsite.callee.get("root") or {}
            receiver = root.get("id") if root.get("k") == "name" else None
            before = (
                frozenset(b["ordinal"] for b in view.reaching_at(receiver, callsite.cfg_node))
                if receiver
                else frozenset()
            )
            end = (callsite.span.end_line or 0, callsite.span.end_column or 0)
            for other in sc.scope.callsites:
                position = (other.span.start_line or 0, other.span.start_column or 0)
                if (
                    other is callsite
                    or position < end
                    or not _within(
                        [
                            other.span.start_line or 0,
                            other.span.start_column or 0,
                            other.span.end_line or 0,
                            other.span.end_column or 0,
                        ],
                        statement,
                    )
                ):
                    continue
                operation = f"dbop:{call_key(sc.locator, sc.scope.key, other.ordinal)}"
                own = other.callee.get("root") or {}
                name = own.get("id") if own.get("k") == "name" else None
                if operation not in em.nodes or name is None:
                    continue
                reaching = view.reaching_at(name, other.cfg_node)
                same = (
                    name == receiver and before and {b["ordinal"] for b in reaching} == before
                ) or (
                    bool(reaching)
                    and all(
                        b["kind"] == "with_as"
                        and ((b.get("value") or {}).get("from") or {}).get("callsite")
                        == callsite.ordinal
                        for b in reaching
                    )
                )
                if not same:
                    continue
                em.edge(
                    "in_transaction",
                    operation,
                    boundary,
                    "inferred",
                    rule,
                    ("SESSION_IDENTITY_BY_REACHING_DEFINITION",),
                    {},
                    [em.callsite_ev(sc, other)],
                )

    def with_items(self, view: ScopeView) -> list[tuple[FlowCallsite, list[int]]]:
        """(callsite of a ``with`` item, the with-statement span) for each ``with`` item that is
        a call. The span locates the statement body (callsites after the item, inside it)."""
        nodes = (view.input.scope.cfg or {}).get("nodes") or []
        withs = [n["span"] for n in nodes if n.get("kind") == "with" and n.get("span")]
        found: list[tuple[FlowCallsite, list[int]]] = []
        for node in nodes:
            if node.get("kind") != "with_enter" or not node.get("span"):
                continue
            span = node["span"]
            for callsite in view.input.scope.callsites:
                c = callsite.span
                if [c.start_line, c.start_column, c.end_line, c.end_column] == span:
                    owner = [w for w in withs if _within(span, w)]
                    if owner:
                        found.append((callsite, min(owner, key=_extent)))
        return found

    def _new_returns_super(self, new: Target) -> bool:
        view = self.views.get((new.locator or "", new.scope or ""))
        returns = view.input.scope.summary.get("returns") if view else None
        if not returns:
            return False
        for shape in returns:
            callee = shape.get("callee") or {} if shape.get("k") == "call" else {}
            root = callee.get("root") or {}
            ok = (
                callee.get("k") == "attr"
                and callee.get("path") == ["__new__"]
                and (_tail(root.get("callee") or {}) == "super" or root.get("id") == "object")
            )
            if not ok:
                return False
        return True

    def _new_returns_other(self, new: Target, cls: tuple[str, str]) -> bool:
        """Every ``return`` of ``__new__`` constructs a resolved class outside ``cls``'s tree."""
        view = self.views.get((new.locator or "", new.scope or ""))
        returns = view.input.scope.summary.get("returns") if view else None
        if not view or not returns:
            return False
        family = {cls, *self.subclasses(cls)}
        for shape in returns:
            if shape.get("k") != "call":
                return False
            targets = self.callee_targets(view, shape.get("callee") or {}, None)
            if not targets or any(
                t.kind != "class" or (t.locator or "", t.scope or "") in family for t in targets
            ):
                return False
        return True

    def _dataclass_init(self, info: _ClassInfo, defining: ScopeView | None) -> bool:
        for decorator in info.decorators:
            base = decorator.get("callee") if decorator.get("k") == "call" else decorator
            targets = self.callee_targets(defining, base or {}, None) if defining else []
            if not any(t.kind == "external" and t.name == "dataclasses.dataclass" for t in targets):
                continue
            if decorator.get("k") == "call":
                for kw in decorator.get("kwargs") or []:
                    if kw.get("kw") == "init":
                        shape = kw.get("shape") or {}
                        if shape.get("t") != "bool":
                            return False  # non-literal: unknown, no link
                        return bool(shape.get("v"))
            return True
        return False

    # ----------------------------------------------------------------- frameworks

    def frameworks(
        self, view: ScopeView, callsite: FlowCallsite, key: str, ev: str, targets: list[Target]
    ) -> None:
        em = self.em
        names = {t.name for t in targets if t.kind in {"external", "method_of_ext"}}
        # asyncio scheduling (py-async-semantics.v2)
        if names & {"asyncio.create_task", "asyncio.ensure_future"} or any(
            (n or "").endswith("TaskGroup.create_task") for n in names
        ):
            self._scheduled(view, callsite, key, ev, "asyncio_task")
        if "asyncio.run" in names:
            for argument in callsite.arguments[:1]:
                for target in self._inner_call_targets(view, argument.get("shape") or {}):
                    em.entry(
                        "asyncio_run",
                        key,
                        "asyncio.run()",
                        target,
                        view.input.locator,
                        [ev],
                    )
        if "asyncio.gather" in names:
            for argument in callsite.arguments:
                inner = argument.get("shape") or {}
                inner = inner.get("value", inner) if inner.get("k") == "starred" else inner
                for t in self._inner_call_targets(view, inner):
                    em.edge(
                        "awaits",
                        key,
                        t,
                        "inferred",
                        RULES["py_async"],
                        ("ORDER_NOT_DETERMINED",),
                        {"all": True},
                        [ev],
                    )
        # FastAPI background tasks
        if callsite.callee.get("k") == "attr" and (callsite.callee.get("path") or [])[-1:] == [
            "add_task"
        ]:
            root = (
                self.callee_targets(
                    view,
                    {**callsite.callee, "path": callsite.callee["path"][:-1]},
                    callsite.cfg_node,
                )
                if len(callsite.callee["path"]) > 1
                else self.callee_targets(view, callsite.callee.get("root") or {}, callsite.cfg_node)
            )
            if any(
                t.kind == "ext_instance" and (t.name or "").endswith("BackgroundTasks")
                for t in root
            ):
                scheduler = em.node(
                    "scheduler:fastapi_background",
                    "scheduler",
                    "FastAPI background tasks",
                    None,
                    {"order": "after_response_send"},
                )
                args = [a for a in callsite.arguments if a.get("pos") == 0]
                for task in (
                    self.callee_targets(view, args[0]["shape"], callsite.cfg_node) if args else []
                ):
                    if task.kind == "scope" and task.locator and task.scope:
                        resource = any(
                            self._yield_backed(view, (a.get("shape") or {}).get("id"), 0)
                            for a in callsite.arguments
                            if a.get("pos") != 0 and (a.get("shape") or {}).get("k") == "name"
                        )
                        conditions = (
                            "ANNOTATION_TRUSTED",
                            *task.conditions,
                            *(("BACKGROUND_TASK_USES_DEPENDENCY_RESOURCE",) if resource else ()),
                        )
                        self.background.setdefault(
                            scope_key(view.input.locator, view.input.scope.key), []
                        ).append((key, scope_key(task.locator, task.scope), conditions))
                        em.edge(
                            "registers",
                            key,
                            scope_key(task.locator, task.scope),
                            "inferred",
                            RULES["fastapi"],
                            conditions,
                            {"scheduler": "fastapi_background"},
                            [ev],
                        )
                        em.edge(
                            "scheduled_by",
                            scheduler,
                            scope_key(task.locator, task.scope),
                            "inferred",
                            RULES["fastapi"],
                            (),
                            {"after": "response_send"},
                            [ev],
                        )
        self.sqlalchemy(view, callsite, key, ev, targets)

    def _inner_call_targets(self, view: ScopeView, shape: dict[str, Any]) -> list[str]:
        if shape.get("k") != "call" or shape.get("callsite") is None:
            return []
        inner = next(
            (c for c in view.input.scope.callsites if c.ordinal == shape["callsite"]), None
        )
        if inner is None:
            return []
        return [
            scope_key(t.locator or "", t.scope or "")
            for t in self.callee_targets(view, inner.callee, inner.cfg_node)
            if t.kind == "scope"
        ]

    def _scheduled(
        self, view: ScopeView, callsite: FlowCallsite, key: str, ev: str, scheduler_kind: str
    ) -> None:
        scheduler = self.em.node(
            f"scheduler:{scheduler_kind}", "scheduler", scheduler_kind, None, {}
        )
        for argument in callsite.arguments:
            if argument.get("pos") != 0:
                continue
            for target in self._inner_call_targets(view, argument.get("shape") or {}):
                self.em.edge(
                    "registers",
                    key,
                    target,
                    "derived",
                    RULES["py_async"],
                    ("ORDER_NOT_DETERMINED",),
                    {"scheduler": scheduler_kind},
                    [ev],
                )
                self.em.edge(
                    "scheduled_by",
                    scheduler,
                    target,
                    "derived",
                    RULES["py_async"],
                    ("ORDER_NOT_DETERMINED",),
                    {},
                    [ev],
                )

    def sqlalchemy(
        self, view: ScopeView, callsite: FlowCallsite, key: str, ev: str, targets: list[Target]
    ) -> None:
        methods = [
            t
            for t in targets
            if t.kind == "method_of_ext" and (t.name or "").startswith("sqlalchemy")
        ]
        if not methods:
            return
        method = (methods[0].name or "").rsplit(".", 1)[-1]
        op = _SESSION_OPS.get(method)
        if op is None or not any(
            ".Session" in (t.name or "") or "AsyncSession" in (t.name or "") for t in methods
        ):
            return
        conditions = set(methods[0].conditions)
        node_key = self.em.node(
            f"dbop:{key}",
            "db_operation",
            f"{method}()",
            view.input.locator,
            {"operation": method, "class": op},
        )
        tables: list[tuple[str, str]] = []
        first = next((a.get("shape") for a in callsite.arguments if a.get("pos") == 0), None) or {}
        if method in {"execute", "scalars", "scalar"} and first.get("k") == "call":
            statement = _tail(first.get("callee") or {})
            if statement == "text":
                conditions.add("RAW_SQL_NOT_PARSED")
            elif statement in _STATEMENTS:
                op = _STATEMENTS[statement] if method == "execute" else "query"
                tables = self._tables(view, (first.get("args") or [{}])[0], callsite.cfg_node)
        elif method in {"get", "query"} and first:
            tables = self._tables(view, first, callsite.cfg_node)
        elif op == "stage":
            if first.get("k") == "call":  # session.add(M(...))
                tables = self._tables(view, first.get("callee") or {}, callsite.cfg_node)
            elif first.get("k") == "name":
                for binding in view.reaching_at(first["id"], callsite.cfg_node):
                    value = binding.get("value") or {}
                    if value.get("k") == "call":
                        tables += self._tables(view, value.get("callee") or {}, binding["node"])
            if not tables:
                conditions.add("TABLE_UNKNOWN_FOR_INSTANCE")
        self.em.nodes[node_key] = replace(
            self.em.nodes[node_key], attributes={"operation": method, "class": op}
        )
        self.em.edge(
            "performs",
            key,
            node_key,
            "inferred",
            RULES["sqlalchemy"],
            tuple(sorted(conditions)),
            {"class": op},
            [ev],
        )
        for table, table_ev in tables:
            element = self.facts.stage14_node(table)
            if element is not None:
                self.em.node(
                    table,
                    "db_table",
                    str(element.attributes.get("table") or table.removeprefix("db_table:")),
                    element.locator,
                    {},
                    [self.em.stage14_ev(element)],
                )
            self.em.edge(
                "on_table",
                node_key,
                table,
                "inferred",
                RULES["sqlalchemy"],
                tuple(sorted(conditions)),
                {"class": op},
                [ev, table_ev],
            )

    def _tables(
        self, view: ScopeView, shape: dict[str, Any], node: int | None
    ) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for t in self.callee_targets(view, shape, node):
            if t.kind != "class" or not t.locator or not t.scope:
                continue
            name = self.classes[(t.locator, t.scope)].name
            for element in self.facts.maps_table.get((t.locator, name), []):
                if element.target_key:
                    found.append((element.target_key, self.em.stage14_ev(element)))
        return found

    # ----------------------------------------------------------------- FastAPI handlers

    def handlers(self, endpoints: list[Any]) -> None:
        for element in endpoints:
            identity = _identity(element.catalog_entity_key)
            view = self.by_identity.get(identity) if identity else None
            if view is None:
                self.em.diagnose("ENDPOINT_HANDLER_NOT_FOUND", element.key)
                continue
            handler = scope_key(view.input.locator, view.input.scope.key)
            self.em.node(
                element.key,
                "api_endpoint",
                element.attributes.get("function") or element.key,
                element.locator,
                {
                    "paths": element.attributes.get("paths"),
                    "methods": element.attributes.get("methods"),
                },
                [self.em.stage14_ev(element)],
            )
            self.em.edge(
                "handled_by",
                element.key,
                handler,
                "derived",
                RULES["stage14_stitching"],
                (),
                {},
                [self.em.stage14_ev(element), self.em.scope_ev(view.input)],
            )
            paths = [
                item.get("path")
                for item in element.attributes.get("paths") or []
                if isinstance(item, dict) and item.get("path")
            ]
            methods = element.attributes.get("methods") or []
            self.em.entry(
                "http_endpoint",
                element.key,
                f"{'/'.join(methods) or '?'} {paths[0] if paths else '?'}",
                handler,
                view.input.locator,
                [self.em.stage14_ev(element)],
                {"methods": methods, "paths": paths},
            )
            self.injects(view, handler, set(), 0)
            outcomes = self.responses(view, handler)
            self.boundary(view, handler, outcomes)

    def injects(self, view: ScopeView, source: str, seen: set[str], depth: int) -> None:
        if depth > 6 or source in seen:
            return
        seen.add(source)
        parent = self.parent_view(view) or view
        version = self.facts.fastapi_alternatives(view.input.locator)
        for param in view.parameters:
            default = param.get("default") or {}
            provider = self.depends_provider(view, default)
            annotation = param.get("annotation") or {}
            if (
                provider is None
                and annotation.get("k") == "subscript"
                and _tail(annotation.get("value") or {}) == "Annotated"
            ):
                for extra in (annotation.get("index") or [])[1:]:
                    provider = self.depends_provider(view, extra) or provider
                    if provider is not None:
                        default = extra
            if provider is None:
                continue
            scope_kw = next(
                (kw.get("shape") for kw in default.get("kwargs") or [] if kw.get("kw") == "scope"),
                None,
            )
            for t in (
                self.callee_targets(parent, provider, None)
                if provider.get("k") != "class_dependency"
                else []
            ):
                if t.kind != "scope" or not t.locator or not t.scope:
                    continue
                target = scope_key(t.locator, t.scope)
                provided = self.views.get((t.locator, t.scope))
                kind = provided.input.scope.callable_kind if provided else "sync_function"
                computed = _literal_text(scope_kw) or (
                    "request"
                    if kind in {"generator_function", "async_generator_function"}
                    else None
                )
                ev = [self.em.scope_ev(view.input)]
                cache = next(
                    (
                        kw.get("shape") or {}
                        for kw in default.get("kwargs") or []
                        if kw.get("kw") == "use_cache"
                    ),
                    {"k": "lit", "t": "bool", "v": True},
                )
                use_cache = cache.get("v") if cache.get("t") == "bool" else None
                self.injected.setdefault(source, []).append(
                    (target, {"phase": "setup", "scope": computed})
                )
                self.em.edge(
                    "injects",
                    source,
                    target,
                    t.tier,
                    RULES["fastapi"],
                    (*t.conditions, *(() if use_cache is not None else ("USE_CACHE_UNKNOWN",))),
                    {
                        "phase": "setup",
                        "param": param.get("name"),
                        "scope": computed,
                        "use_cache": use_cache,
                    },
                    ev,
                    "setup",
                )
                if kind in {"generator_function", "async_generator_function"}:
                    alternatives = [
                        a for a in version["alternatives"] if a["scope"] in {computed, "any"}
                    ] or version["alternatives"]
                    self.injected.setdefault(source, []).append(
                        (target, {"phase": "teardown", "alternatives": alternatives})
                    )
                    self.em.edge(
                        "injects",
                        source,
                        target,
                        "inferred" if len(alternatives) > 1 else t.tier,
                        RULES["fastapi"],
                        tuple(
                            sorted(
                                {
                                    *t.conditions,
                                    *version["conditions"],
                                    *(("SEMANTICS_ALTERNATIVE",) if len(alternatives) > 1 else ()),
                                }
                            )
                        ),
                        {
                            "phase": "teardown",
                            "param": param.get("name"),
                            "scope": computed,
                            "alternatives": alternatives,
                            "fastapi": version["version"],
                        },
                        ev,
                        "teardown",
                    )
                if provided is not None:
                    self.injects(provided, target, seen, depth + 1)

    def responses(self, view: ScopeView, handler: str) -> list[tuple[str, str, dict[str, Any]]]:
        """Exit-specific ``response_outcome``s; returns (outcome key, tier, attributes)."""
        em, rule = self.em, RULES["fastapi_responses"]
        scope = view.input.scope
        ev = [em.scope_ev(view.input)]
        status: int | None = None
        status_known = True
        for decorator in scope.summary.get("decorators") or []:
            if (
                decorator.get("k") == "call"
                and _tail(decorator.get("callee") or {}) in _ROUTE_VERBS
            ):
                for kw in decorator.get("kwargs") or []:
                    if kw.get("kw") == "status_code":
                        status = _status(kw.get("shape") or {})
                        status_known = status is not None
        app = self.facts.app_conditions
        outcomes: list[tuple[str, dict[str, Any], tuple[str, ...]]] = []
        dynamic = [
            b
            for b in view.bindings
            if b["kind"] == "attribute_store" and b["name"].endswith(".status_code")
        ]
        for index, shape in enumerate(scope.summary.get("returns") or []):
            callee = _tail(shape.get("callee") or {}) if shape.get("k") == "call" else ""
            if callee.endswith("Response"):
                if any(kw.get("kw") == "background" for kw in shape.get("kwargs") or []):
                    self._drop_background(view, handler)
                code = next(
                    (
                        _status(kw.get("shape") or {})
                        for kw in shape.get("kwargs") or []
                        if kw.get("kw") == "status_code"
                    ),
                    None,
                )
                code = code if code is not None else _RESPONSE_DEFAULTS.get(callee, 200)
                outcomes.append(
                    (
                        f"return:{index}",
                        {
                            "exit": "return",
                            "status": code,
                            "response_class": callee,
                            "returned_instance": True,
                        },
                        ("RESPONSE_INSTANCE_USED_AS_IS",),
                    )
                )
            else:
                base = status if status is not None else (200 if status_known else None)
                outcomes.append(
                    (
                        f"return:{index}",
                        {"exit": "return", "status": base},
                        () if base is not None else ("STATUS_UNKNOWN",),
                    )
                )
        for binding in dynamic:
            code = _status(binding.get("value") or {})
            outcomes.append(
                (
                    f"status:{binding['ordinal']}",
                    {"exit": "return", "status": code},
                    ("DYNAMIC_STATUS_ASSIGNMENT", "STATUS_DEPENDS_ON_PATH")
                    if code
                    else ("STATUS_UNKNOWN",),
                )
            )
        for callsite in scope.callsites:
            targets = self.callee_targets(view, callsite.callee, callsite.cfg_node)
            if any(
                t.kind == "external" and (t.name or "").endswith("HTTPException") for t in targets
            ):
                code = next(
                    (
                        _status(a.get("shape") or {})
                        for a in callsite.arguments
                        if a.get("kw") == "status_code" or a.get("pos") == 0
                    ),
                    None,
                )
                outcomes.append(
                    (
                        f"raise:{callsite.ordinal}",
                        {"exit": "raise", "status": code, "callsite": callsite.ordinal},
                        (*(() if code else ("STATUS_UNKNOWN",)), *app.get("raise", ())),
                    )
                )
        if any(
            p.get("default") is None or self.depends_provider(view, p.get("default") or {}) is None
            for p in view.parameters
        ):
            outcomes.append(
                (
                    "validation",
                    {"exit": "validation", "status": 422},
                    (
                        "FRAMEWORK_VALIDATION_422",
                        *app.get("validation", ()),
                        *(
                            ("DEPENDENCY_SIDE_EFFECTS_ON_422_POSSIBLE",)
                            if self.injected.get(handler)
                            else ()
                        ),
                    ),
                )
            )
        # HTTPException raised below the handler: in a callee it reaches through resolved
        # invocations, or in an injected dependency. Propagation through intermediate callers'
        # ``except`` clauses is not verified (stated on the edge).
        own = {f"call:{view.input.locator}#{scope.key}:"}
        for raised, code, origin in self._nested_http_raises(handler):
            if any(raised.startswith(prefix) for prefix in own):
                continue
            outcomes.append(
                (
                    f"raise:{raised}",
                    {"exit": "raise", "status": code, "raised_at": raised},
                    (
                        origin,
                        "PROPAGATION_THROUGH_CALLERS_NOT_VERIFIED",
                        *(() if code else ("STATUS_UNKNOWN",)),
                        *app.get("raise", ()),
                    ),
                )
            )
        unhandled = (
            "DEFAULT_SERVER_ERROR_HANDLING_ASSUMED",
            "RESPONSE_MAY_HAVE_STARTED",
            *app.get("raise", ()),
            *(("EXCEPTION_HANDLER_REGISTERED_NOT_LINKED",) if self._unlinked() else ()),
        )
        outcomes.append(
            ("unhandled", {"exit": "unhandled", "status": None, "alternatives": [500]}, unhandled)
        )
        emitted: list[tuple[str, str, dict[str, Any]]] = []
        for name, attributes, mine in outcomes:
            conditions = tuple(sorted({*mine, *app.get("all", ())}))
            node = em.node(
                f"response:{handler}:{name}",
                "response_outcome",
                f"{attributes.get('status') or '?'} ({attributes['exit']})",
                view.input.locator,
                attributes,
            )
            tier = "inferred" if conditions else "derived"
            em.edge(
                "exits_with",
                handler,
                node,
                tier,
                rule,
                conditions,
                {"exit": attributes["exit"]},
                ev,
            )
            emitted.append((node, tier, attributes))
            raised_key: str | None = (
                attributes.get("raised_at")
                if "raised_at" in attributes
                else call_key(view.input.locator, scope.key, attributes["callsite"])
                if "callsite" in attributes
                else None
            )
            if raised_key is not None:
                em.edge(
                    "raises_to",
                    raised_key,
                    node,
                    tier,
                    rule,
                    conditions,
                    {"exception": "HTTPException"},
                    ev,
                )
            self._link_exception_handlers(node, attributes, ev)
        return emitted

    # ----------------------------------------------------------------- FastAPI helpers

    def _http_raises(self, view: ScopeView) -> list[tuple[FlowCallsite, int | None]]:
        found: list[tuple[FlowCallsite, int | None]] = []
        for callsite in view.input.scope.callsites:
            targets = self.callee_targets(view, callsite.callee, callsite.cfg_node)
            if any(
                t.kind == "external" and (t.name or "").endswith("HTTPException") for t in targets
            ):
                found.append(
                    (
                        callsite,
                        next(
                            (
                                _status(a.get("shape") or {})
                                for a in callsite.arguments
                                if a.get("kw") == "status_code" or a.get("pos") == 0
                            ),
                            None,
                        ),
                    )
                )
        return found

    def _invocations(self) -> dict[str, list[str]]:
        """Scope → scopes it invokes or awaits (resolved, any tier), from the emitted edges."""
        if self._invoked is None:
            calls: dict[str, list[str]] = {}
            owner: dict[str, str] = {}
            for e in self.em.edges.values():
                if e.relation == "contains_callsite" and e.target_key:
                    owner[e.target_key] = e.source_key
            for e in self.em.edges.values():
                if e.relation in {"invokes", "awaits"} and e.target_key in self.em.nodes:
                    source = owner.get(e.source_key)
                    if source and e.attributes.get("phase") != "definition":
                        calls.setdefault(source, []).append(e.target_key or "")
            self._invoked = calls
        return self._invoked

    def _nested_http_raises(self, handler: str) -> list[tuple[str, int | None, str]]:
        """(raising callsite key, status, RAISED_IN_CALLEE | RAISED_IN_DEPENDENCY) below a
        handler, within three invocation hops and the dependency tree."""
        found: list[tuple[str, int | None, str]] = []
        seen: set[str] = set()
        frontier = [(handler, 0, "RAISED_IN_CALLEE")]
        frontier += [
            (target, 1, "RAISED_IN_DEPENDENCY")
            for target, data in self._dependency_tree(handler)
            if data["phase"] == "setup"
        ]
        while frontier:
            current, depth, origin = frontier.pop(0)
            if current in seen or depth > 3:
                continue
            seen.add(current)
            view = self.views.get(_split_scope(current))
            if view is None:
                continue
            if current != handler:
                for callsite, code in self._http_raises(view):
                    found.append(
                        (
                            call_key(view.input.locator, view.input.scope.key, callsite.ordinal),
                            code,
                            origin,
                        )
                    )
            for callee in self._invocations().get(current, []):
                frontier.append((callee, depth + 1, origin))
        return sorted(set(found), key=lambda item: item[0])

    def _dependency_tree(self, source: str) -> list[tuple[str, dict[str, Any]]]:
        values: list[tuple[str, dict[str, Any]]] = []
        stack, seen = [source], {source}
        while stack:
            current = stack.pop()
            for target, data in self.injected.get(current, []):
                values.append((target, data))
                if target not in seen:
                    seen.add(target)
                    stack.append(target)
        return values

    def _yield_backed(self, view: ScopeView, name: str | None, depth: int) -> bool:
        """Is parameter ``name`` provided (transitively) by a yield dependency?"""
        if name is None or depth > 4:
            return False
        for param in view.parameters:
            if param.get("name") != name:
                continue
            provider = self.depends_provider(view, param.get("default") or {})
            if provider is None:
                return False
            parent = self.parent_view(view) or view
            for t in self.callee_targets(parent, provider, None):
                provided = self.views.get((t.locator or "", t.scope or ""))
                if provided is None:
                    continue
                if provided.input.scope.callable_kind in {
                    "generator_function",
                    "async_generator_function",
                }:
                    return True
                if any(
                    self._yield_backed(provided, p.get("name"), depth + 1)
                    for p in provided.parameters
                ):
                    return True
        return False

    def _drop_background(self, view: ScopeView, handler: str) -> None:
        """A returned ``Response`` with its own ``background`` replaces the injected tasks."""
        for callsite_key, task, conditions in self.background.get(handler, []):
            self.em.edge(
                "registers",
                callsite_key,
                task,
                "inferred",
                RULES["fastapi"],
                (*conditions, "BACKGROUND_TASKS_DROPPED_BY_RESPONSE"),
                {"scheduler": "fastapi_background"},
                [],
            )

    def exception_handlers(self) -> list[tuple[str, str, str]]:
        """Statically registered handlers: (exception identity, handler scope, evidence)."""
        if self._handlers is not None:
            return self._handlers
        found: list[tuple[str, str, str]] = []
        for view in self.views.values():
            sc = view.input
            for decorator in sc.scope.summary.get("decorators") or []:
                if decorator.get("k") == "call" and _tail(decorator.get("callee") or {}) == (
                    "exception_handler"
                ):
                    args = decorator.get("args") or []
                    parent = self.parent_view(view) or view
                    identity = self._exception_identity(parent, args[0] if args else {})
                    if identity:
                        found.append(
                            (identity, scope_key(sc.locator, sc.scope.key), self.em.scope_ev(sc))
                        )
                    else:
                        self._unlinked_handlers = True
            for callsite in sc.scope.callsites:
                if _tail(callsite.callee) != "add_exception_handler":
                    continue
                args = {a.get("pos"): a.get("shape") or {} for a in callsite.arguments}
                first = args.get(0) or {}
                if first.get("k") == "name" and any(
                    b["kind"] == "for_target" for b in view.by_name.get(first["id"], [])
                ):
                    continue  # table-driven registration: EXCEPTION_HANDLER_TABLE_NOT_STATIC
                identity = self._exception_identity(view, first)
                handlers = [
                    t
                    for t in self.callee_targets(view, args.get(1) or {}, callsite.cfg_node)
                    if t.kind == "scope" and t.locator and t.scope
                ]
                if identity and len(handlers) == 1:
                    found.append(
                        (
                            identity,
                            scope_key(handlers[0].locator or "", handlers[0].scope or ""),
                            self.em.callsite_ev(sc, callsite),
                        )
                    )
                else:
                    self._unlinked_handlers = True
        self._handlers = sorted(found)
        return self._handlers

    def _unlinked(self) -> bool:
        self.exception_handlers()
        return self._unlinked_handlers

    def _exception_identity(self, view: ScopeView, shape: dict[str, Any]) -> str | None:
        targets = self.callee_targets(view, shape, None) if shape else []
        if len(targets) != 1:
            return None
        t = targets[0]
        if t.kind == "external" and t.name:
            return t.name.rsplit(".", 1)[-1]
        if t.kind == "builtin" and t.name:
            return t.name
        if t.kind == "class" and t.locator and t.scope:
            return f"class:{t.locator}#{t.scope}"
        return None

    def _link_exception_handlers(
        self, outcome: str, attributes: dict[str, Any], ev: list[str]
    ) -> None:
        """``raises_to`` from an outcome to the static handlers that may produce the response
        instead: ``HTTPException`` and ``RequestValidationError`` by identity; an unhandled
        exception to every other registered handler (type match not determined)."""
        exit_kind = attributes["exit"]
        for identity, handler, evidence in self.exception_handlers():
            if exit_kind == "raise" and identity.endswith("HTTPException"):
                conditions: tuple[str, ...] = ("EXCEPTION_HANDLER_BY_TYPE",)
            elif exit_kind == "validation" and identity == "RequestValidationError":
                conditions = ("EXCEPTION_HANDLER_BY_TYPE",)
            elif exit_kind == "unhandled" and identity not in {
                "HTTPException",
                "RequestValidationError",
            }:
                conditions = ("EXCEPTION_TYPE_MATCH_NOT_DETERMINED",)
            else:
                continue
            self.em.edge(
                "raises_to",
                outcome,
                handler,
                "inferred",
                RULES["fastapi_responses"],
                conditions,
                {"exception": identity, "via": "exception_handler"},
                [*ev, evidence],
            )

    def boundary(
        self, view: ScopeView, handler: str, outcomes: list[tuple[str, str, dict[str, Any]]]
    ) -> None:
        """The ``framework_response_send`` boundary: every outcome is sent through it; yield-
        dependency teardowns run on the side the version table determines; background tasks
        run after the send (``fastapi-semantics.v1``)."""
        em, rule = self.em, RULES["fastapi"]
        scheduler = em.node(
            "scheduler:framework_response_send",
            "scheduler",
            "Framework response send",
            None,
            {"order": "outcome, then teardown or send per the version table"},
        )
        ev = [em.scope_ev(view.input)]
        for outcome, tier, _ in outcomes:
            em.edge(
                "response_boundary",
                outcome,
                scheduler,
                tier,
                rule,
                (),
                {"handler": handler, "step": "send"},
                ev,
            )
        sides: set[str] = set()
        backgrounds: set[str] = set()
        for target, data in self._dependency_tree(handler):
            if data["phase"] != "teardown":
                continue
            alternatives = data["alternatives"]
            sides |= {a["teardown"] for a in alternatives}
            backgrounds |= {a["background"] for a in alternatives}
            several = len({a["teardown"] for a in alternatives}) > 1
            em.edge(
                "response_boundary",
                scheduler,
                target,
                "inferred" if several else "derived",
                rule,
                ("SEMANTICS_ALTERNATIVE",) if several else (),
                {
                    "handler": handler,
                    "step": "teardown",
                    "sides": sorted({a["teardown"] for a in alternatives}),
                },
                ev,
                f"teardown:{handler}",
            )
        for callsite_key, task, conditions in self.background.get(handler, []):
            current = em.edges.get(f"registers|{callsite_key}|{task}")
            dropped = current is not None and "BACKGROUND_TASKS_DROPPED_BY_RESPONSE" in (
                current.conditions
            )
            em.edge(
                "response_boundary",
                scheduler,
                task,
                "inferred",
                rule,
                tuple(
                    sorted(
                        {
                            *conditions,
                            "RESPONSE_MAY_HAVE_STARTED",
                            *(("BACKGROUND_TASKS_DROPPED_BY_RESPONSE",) if dropped else ()),
                        }
                    )
                ),
                {
                    "handler": handler,
                    "step": "background",
                    "after": "response_send",
                    "relative_to_teardown": sorted(backgrounds) or None,
                    "callsite": callsite_key,
                },
                ev,
                f"background:{handler}",
            )


# ----------------------------------------------------------------------------- helpers


def _tail(shape: dict[str, Any]) -> str:
    if shape.get("k") == "name":
        return str(shape.get("id") or "")
    if shape.get("k") == "attr":
        return str((shape.get("path") or [""])[-1])
    return ""


def _label(shape: dict[str, Any]) -> str:
    if shape.get("k") == "name":
        return f"{shape.get('id')}()"
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
    if kind == "coroutine_function":
        return "awaits" if awaited else "creates_coroutine"
    if kind == "generator_function":
        return "creates_generator"
    if kind == "async_generator_function":
        return "creates_async_generator"
    if kind == "js_async_function":
        return "awaits" if awaited else "invokes_until_first_await"
    if kind in {"js_generator_function"}:
        return "creates_generator"
    if kind in {"js_async_generator_function"}:
        return "creates_async_generator"
    return "invokes"


def _external_result(qualified: str) -> str:
    return qualified


def _framework_base(info: _ClassInfo) -> bool:
    return any(_tail(b) in {"BaseModel", "BaseSettings"} for b in info.bases)


def _status(shape: dict[str, Any]) -> int | None:
    if shape.get("k") == "lit" and shape.get("t") == "int":
        value = int(shape["v"])
        return value if 100 <= value <= 599 else None
    name = _tail(shape)
    match = _STATUS.match(name) if name else None
    return int(match.group(1)) if match else None


def _literal_text(shape: dict[str, Any] | None) -> str | None:
    """Literal string values are hashed; recognise the two FastAPI scope literals by hash."""
    if not shape or shape.get("t") != "str":
        return None
    import hashlib

    for value in ("function", "request"):
        if shape.get("sha256") == hashlib.sha256(value.encode()).hexdigest():
            return value
    return None


def _identity(entity_key: str | None) -> UUID | None:
    if not entity_key or not entity_key.startswith("api_endpoint:"):
        return None
    try:
        return UUID(entity_key.split(":")[1])
    except (ValueError, IndexError):
        return None


def _c3(
    head: list[tuple[str, str]], sequences: list[list[tuple[str, str]]]
) -> list[tuple[str, str]] | None:
    result = list(head)
    seqs = [list(s) for s in sequences if s]
    while seqs:
        for seq in seqs:
            candidate = seq[0]
            if not any(candidate in other[1:] for other in seqs):
                break
        else:
            return None
        result.append(candidate)
        seqs = [[x for x in s if x != candidate] for s in seqs]
        seqs = [s for s in seqs if s]
    return result


def _conditional_import(bindings: list[dict[str, Any]], targets: list[Target]) -> list[Target]:
    """More than one reaching definition including an import (``try: import a`` / ``except``,
    or ``if``-guarded imports): ``CONDITIONAL_IMPORT``."""
    if len(bindings) > 1 and any(b["kind"] == "import" for b in bindings):
        return [t.weaker("inferred", "CONDITIONAL_IMPORT") for t in targets]
    return targets


def _within(inner: list[int], outer: list[int]) -> bool:
    return (outer[0], outer[1]) <= (inner[0], inner[1]) and (inner[2], inner[3]) <= (
        outer[2],
        outer[3],
    )


def _extent(span: list[int]) -> tuple[int, int]:
    return (span[2] - span[0], span[3] - span[1])


def _split_scope(key: str) -> tuple[str, str]:
    """``scope:{locator}#{key}`` → (locator, key)."""
    body = key.removeprefix("scope:")
    locator, _, scope = body.rpartition("#")
    return locator, scope
