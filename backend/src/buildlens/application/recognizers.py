"""Labelled deterministic recognizers over one artifact's structural rows.

A recognizer reports a *shape* plus the same-file evidence that supports it. Strength is
``pattern_with_local_evidence`` when a supporting condition holds and ``pattern_only``
otherwise; conditions met and unmet are always listed. There is no numeric confidence, and a
match is a heuristic label, never a verified claim about runtime behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from buildlens.domain.semantic import Row

Strength = Literal["pattern_with_local_evidence", "pattern_only"]
RECOGNIZERS: tuple[str, ...] = (
    "fastapi-route.v1",
    "flask-route.v1",
    "sqlalchemy-declarative.v1",
    "alembic-table.v1",
    "react-component.v1",
    "react-hook.v1",
)

_ROUTE = re.compile(
    r"^(?P<receiver>[A-Za-z_][\w.]*)\.(?P<verb>get|post|put|patch|delete|options|head|trace|"
    r"api_route|websocket|route)\((?P<args>.*)\)$",
    re.DOTALL,
)
_STRING = re.compile(r"""^\s*(?P<quote>['"])(?P<value>(?:\\.|(?!(?P=quote)).)*)(?P=quote)""")
_METHODS = re.compile(r"methods\s*=\s*[\[(](?P<items>[^\])]*)[\])]")
_QUOTED = re.compile(r"""['"]([A-Za-z]+)['"]""")
_TABLENAME = re.compile(r"""^\s*(?P<quote>['"])(?P<value>[^'"]{1,128})(?P=quote)\s*$""")
_HOOK_NAME = re.compile(r"^use[A-Z0-9]")
_REACT_BASES = {"Component", "PureComponent", "React.Component", "React.PureComponent"}
_REACT_MODULES = {"react", "preact", "preact/compat"}
_JSX_SUFFIXES = (".tsx", ".jsx")


@dataclass(frozen=True, slots=True)
class Evidence:
    table: str
    row: Row


@dataclass(frozen=True, slots=True)
class Match:
    recognizer: str
    role: Literal["api_endpoint", "db_table", "component", "hook"]
    subject: Row
    subject_table: Literal["symbols", "artifact_analyses"]
    ordinal: int
    strength: Strength
    met: tuple[str, ...]
    unmet: tuple[str, ...]
    name: str
    attributes: dict[str, str | None] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactView:
    """The rows a recognizer may read: one artifact, nothing cross-file."""

    analysis: Row
    symbols: tuple[Row, ...]
    imports: tuple[Row, ...]
    facts_by_symbol: dict[str, Row]
    references: tuple[Row, ...]
    facts_available: bool


def _modules(view: ArtifactView) -> list[tuple[str, Row]]:
    values = []
    for row in view.imports:
        module = row.get("from_module") if row["form"] != "import" else row["name"]
        if module and not int(row.get("level") or 0):
            values.append((str(module), row))
    return values


def _imports(view: ArtifactView, roots: set[str]) -> list[Row]:
    return [
        row for module, row in _modules(view) if module in roots or module.split(".")[0] in roots
    ]


def _strength(supporting: list[bool]) -> Strength:
    return "pattern_with_local_evidence" if any(supporting) else "pattern_only"


def _conditions(values: dict[str, bool]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(name for name, ok in values.items() if ok),
        tuple(name for name, ok in values.items() if not ok),
    )


def _route_attributes(verb: str, args: str) -> dict[str, str | None]:
    literal = _STRING.match(args)
    method: str | None = None if verb in {"route", "api_route"} else verb.upper()
    found = _METHODS.search(args)
    if found:
        methods = [item.upper() for item in _QUOTED.findall(found.group("items"))]
        method = ",".join(methods) or method
    elif verb == "route":
        method = "GET"
    return {"method": method, "path": literal.group("value") if literal else None}


def routes(view: ArtifactView) -> list[Match]:
    """``fastapi-route.v1`` and ``flask-route.v1`` over decorated Python functions."""
    if view.analysis.get("language") not in {None, "Python"}:
        return []
    fastapi = _imports(view, {"fastapi"})
    flask = _imports(view, {"flask"})
    matches: list[Match] = []
    for symbol in view.symbols:
        if symbol["kind"] not in {"function", "method"}:
            continue
        ordinals: dict[str, int] = {}
        for decorator in symbol.get("decorators") or []:
            text = (decorator.get("expression") or {}).get("text")
            shape = _ROUTE.match(text or "")
            if not shape:
                continue
            verb = shape.group("verb")
            # ``.route`` is Flask's shape; verb shapes are FastAPI's unless only Flask is imported.
            if verb == "route":
                if fastapi and not flask:
                    continue
                recognizer, framework = "flask-route.v1", flask
            elif flask and not fastapi:
                recognizer, framework = "flask-route.v1", flask
            else:
                recognizer, framework = "fastapi-route.v1", fastapi
            attributes = _route_attributes(verb, shape.group("args"))
            met, unmet = _conditions(
                {
                    "decorator_shape": True,
                    "framework_import": bool(framework),
                    "literal_path": attributes["path"] is not None,
                }
            )
            ordinal = ordinals.get(recognizer, 0)
            ordinals[recognizer] = ordinal + 1
            label = " ".join(value for value in (attributes["method"], attributes["path"]) if value)
            matches.append(
                Match(
                    recognizer,
                    "api_endpoint",
                    symbol,
                    "symbols",
                    ordinal,
                    _strength([bool(framework)]),
                    met,
                    unmet,
                    label or f"{symbol['name']} route",
                    {**attributes, "receiver": shape.group("receiver")},
                    (
                        Evidence("symbols", symbol),
                        *(Evidence("import_references", row) for row in framework[:2]),
                    ),
                )
            )
    return matches


def sqlalchemy_tables(view: ArtifactView) -> list[Match]:
    """``sqlalchemy-declarative.v1``: a class assigning ``__tablename__`` (needs Python facts)."""
    if view.analysis.get("language") not in {None, "Python"} or not view.facts_available:
        return []
    imports = _imports(view, {"sqlalchemy", "flask_sqlalchemy", "sqlmodel"})
    matches: list[Match] = []
    for symbol in view.symbols:
        facts_row = view.facts_by_symbol.get(symbol["id"])
        if symbol["kind"] != "class" or facts_row is None:
            continue
        facts = facts_row.get("facts") or {}
        assignment = next(
            (
                item
                for item in facts.get("class_assignments") or []
                if item.get("name") == "__tablename__"
            ),
            None,
        )
        table_keyword = "table=True" in (facts.get("keywords") or [])
        if assignment is None and not table_keyword:
            continue
        literal = _TABLENAME.match(str((assignment or {}).get("value") or ""))
        met, unmet = _conditions(
            {
                "tablename_assignment": assignment is not None,
                "declarative_base": bool(facts.get("bases")),
                "orm_import": bool(imports),
            }
        )
        name = literal.group("value") if literal else symbol["name"]
        matches.append(
            Match(
                "sqlalchemy-declarative.v1",
                "db_table",
                symbol,
                "symbols",
                0,
                _strength([bool(imports)]),
                met,
                unmet,
                name,
                {"table": literal.group("value") if literal else None},
                (
                    Evidence("symbols", symbol),
                    Evidence("symbol_facts", facts_row),
                    *(Evidence("import_references", row) for row in imports[:2]),
                ),
            )
        )
    return matches


def alembic_tables(view: ArtifactView) -> list[Match]:
    """``alembic-table.v1``: ``op.create_table`` calls; the table name is not captured."""
    if view.analysis.get("language") not in {None, "Python"} or not view.facts_available:
        return []
    imports = [
        row
        for module, row in _modules(view)
        if module == "alembic" and row["form"] == "from" and row["name"] == "op"
    ]
    owners = {row["id"]: row for row in view.symbols}
    locator = str(view.analysis["locator"])
    location = "/versions/" in f"/{locator}"
    matches: list[Match] = []
    calls = [row for row in view.references if row["kind"] == "call"]
    for ordinal, call in enumerate(row for row in calls if row["target"] == "op.create_table"):
        owner = owners.get(call.get("owner_symbol_id") or "")
        met, unmet = _conditions(
            {
                "create_table_call": True,
                "alembic_op_import": bool(imports),
                "migration_location": location,
            }
        )
        matches.append(
            Match(
                "alembic-table.v1",
                "db_table",
                owner if owner is not None else view.analysis,
                "symbols" if owner is not None else "artifact_analyses",
                ordinal,
                _strength([bool(imports)]),
                met,
                unmet,
                f"table #{ordinal + 1} in {locator}",
                {"table": None, "operation": "op.create_table"},
                (
                    Evidence("symbol_references", call),
                    *(Evidence("import_references", row) for row in imports[:1]),
                ),
            )
        )
    return matches


def react(view: ArtifactView) -> list[Match]:
    """``react-component.v1`` and ``react-hook.v1`` over TS/JS declarations."""
    if view.analysis.get("language") not in {"TypeScript", "JavaScript"}:
        return []
    react_imports = [
        row for row in view.imports if str(row.get("from_module") or "") in _REACT_MODULES
    ]
    jsx_file = str(view.analysis["locator"]).lower().endswith(_JSX_SUFFIXES)
    by_owner: dict[str, list[Row]] = {}
    for row in view.references:
        if row.get("owner_symbol_id"):
            by_owner.setdefault(str(row["owner_symbol_id"]), []).append(row)
    matches: list[Match] = []
    for symbol in view.symbols:
        if symbol["kind"] not in {"function", "class"}:
            continue
        facts_row = view.facts_by_symbol.get(symbol["id"])
        facts = (facts_row or {}).get("facts") or {}
        refs = by_owner.get(symbol["id"], [])
        name = str(symbol["name"])
        evidence: list[Evidence] = [Evidence("symbols", symbol)]
        if facts_row is not None:
            evidence.append(Evidence("symbol_facts", facts_row))
        if symbol["kind"] == "function" and _HOOK_NAME.match(name):
            hooks = [row for row in refs if row["kind"] == "hook_call"]
            met, unmet = _conditions(
                {
                    "use_prefix_name": True,
                    "calls_hooks": bool(hooks),
                    "react_import": bool(react_imports),
                }
            )
            evidence += [Evidence("symbol_references", row) for row in hooks[:4]]
            matches.append(
                Match(
                    "react-hook.v1",
                    "hook",
                    symbol,
                    "symbols",
                    0,
                    _strength([bool(hooks)]),
                    met,
                    unmet,
                    name,
                    {"convention": "use-prefix naming"},
                    tuple(evidence),
                )
            )
            continue
        pascal = name[:1].isupper()
        default = symbol.get("name_source") == "export_default"
        returns_jsx = bool(facts.get("returns_jsx"))
        wrapper = facts.get("wrapper")
        bases = [row for row in refs if row["kind"] == "base" and row["target"] in _REACT_BASES]
        if not (pascal or default) or not (returns_jsx or wrapper or bases):
            continue
        evidence += [Evidence("symbol_references", row) for row in bases[:1]]
        met, unmet = _conditions(
            {
                "pascal_case_name": pascal,
                "returns_jsx": returns_jsx,
                "component_wrapper": bool(wrapper),
                "react_base_class": bool(bases),
                "jsx_file": jsx_file,
                "react_import": bool(react_imports),
            }
        )
        matches.append(
            Match(
                "react-component.v1",
                "component",
                symbol,
                "symbols",
                0,
                _strength([returns_jsx, bool(bases)]),
                met,
                unmet,
                name,
                {"wrapper": str(wrapper) if wrapper else None},
                tuple(evidence),
            )
        )
    return matches


def recognize(view: ArtifactView) -> list[Match]:
    return [*routes(view), *sqlalchemy_tables(view), *alembic_tables(view), *react(view)]
