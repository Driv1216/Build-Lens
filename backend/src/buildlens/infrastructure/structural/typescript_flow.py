"""Stage 15 ``ts-flow.v2``: per-scope CFGs, callsites, bindings and JSX bindings for TS/TSX/JS.

Parse-only tree-sitter, the same grammars, decoding and partial-parse rule as the declaration
pass: a top-level statement containing an ERROR or MISSING node is skipped (``partial``).
Positions come from byte offsets only (never ``Node.start_point``; see ``typescript._Lines``).
Callsite spans use the declaration pass's convention (1-based lines, UTF-8 byte columns) plus
decoded offsets, so Milestone B can join them to Stage 14 literal rows by exact span.

Scopes: the program, every function declaration/expression, arrow function, method, and class
static block. Classes have no scope of their own; field initialisers belong to construction
and are recorded on the class's methods' parent scope as JSX/callsites only when evaluated.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

import tree_sitter

from buildlens.domain import flow_cfg as C
from buildlens.domain.flow_evidence import (
    BINDINGS_PER_SCOPE,
    CALLSITES_PER_ARTIFACT,
    CALLSITES_PER_SCOPE,
    CFG_NODES_PER_SCOPE,
    JSX_PER_SCOPE,
    SCOPES_PER_ARTIFACT,
    SHAPE_DEPTH,
    SHAPE_ITEMS,
    SUMMARY_SHAPES,
    TS_FLOW,
    DisplayText,
    FlowCallsite,
    FlowExtraction,
    FlowJsxBinding,
    FlowScope,
    display,
    flow_pass_contract,
    literal,
)
from buildlens.domain.source import ContentRejectedError, decode_source, digest, screen_content
from buildlens.domain.structural import ParseDiagnostic, SourceSpan
from buildlens.infrastructure.structural.typescript import (
    LanguageUnavailableError,
    _language,
    _Lines,
    _Offsets,
    grammar_for,
)

# (symbol id, kind, decoded start offset, decoded end offset)
SymbolRegion = tuple[UUID, str, int, int]

_FUNCTIONS = {
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "function",
    "generator_function",
    "arrow_function",
    "method_definition",
    "class_static_block",
}
_STRING = {"string", "template_string"}
_TRANSPARENT = {
    "parenthesized_expression",
    "as_expression",
    "satisfies_expression",
    "non_null_expression",
    "type_assertion",
}
_DYNAMIC = {"eval", "Function"}


class _Truncated(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _text(node: tree_sitter.Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _named(node: tree_sitter.Node | None) -> list[tree_sitter.Node]:
    if node is None:
        return []
    return [c for c in node.named_children if c.type != "comment"]


def _unwrap(node: tree_sitter.Node | None) -> tree_sitter.Node | None:
    while node is not None and node.type in _TRANSPARENT:
        inner = _named(node)
        node = inner[0] if inner else None
    return node


def _string_value(node: tree_sitter.Node) -> str | None:
    if node.type == "string":
        return "".join(
            _text(c)
            for c in node.named_children
            if c.type in {"string_fragment", "escape_sequence"}
        )
    if node.type == "template_string" and not any(
        c.type == "template_substitution" for c in node.named_children
    ):
        return "".join(_text(c) for c in node.named_children if c.type == "string_fragment")
    return None


@dataclass(slots=True)
class _Plan:
    key: str
    parent: str | None
    kind: str
    node: tree_sitter.Node
    name: str | None
    owner: UUID | None = None


@dataclass(slots=True)
class _Budget:
    callsites: int = 0


class _Context:
    """Per-artifact coordinates and the scope plan."""

    def __init__(
        self, source: str, data: bytes, tree: tree_sitter.Tree, regions: list[SymbolRegion]
    ) -> None:
        self.data = data
        self.lines = _Lines(data)
        self.offsets = _Offsets(source)
        self.tree = tree
        self.regions = regions
        self.plans: list[_Plan] = []
        self.keys: dict[tuple[int, int], str] = {}
        self.dropped: list[tuple[int, int]] = []
        self.kept: list[tree_sitter.Node] = []
        self.unmatched = 0

    def ident(self, node: tree_sitter.Node) -> tuple[int, int]:
        return (node.start_byte, node.end_byte)

    def span(self, node: tree_sitter.Node | None) -> list[int] | None:
        if node is None:
            return None
        s = self.lines.node(node)
        return [s.start_line or 0, s.start_column or 0, s.end_line or 0, s.end_column or 0]

    def source_span(self, node: tree_sitter.Node) -> SourceSpan:
        return self.lines.node(node)

    def decoded(self, node: tree_sitter.Node) -> tuple[int, int]:
        return self.offsets.char(node.start_byte), self.offsets.char(node.end_byte)

    def plan(self) -> None:
        root = self.tree.root_node
        for statement in _named(root):
            if statement.has_error or statement.is_error or statement.is_missing:
                s = self.lines.node(statement)
                self.dropped.append((s.start_line or 0, s.end_line or 0))
            else:
                self.kept.append(statement)
        self.plans.append(_Plan("m", None, "module", root, None))
        self._children(self.kept, "m")
        self._owners()

    def _children(self, nodes: Iterable[tree_sitter.Node], parent: str) -> None:
        count = 0
        stack = list(reversed(list(nodes)))
        while stack:
            current = stack.pop()
            if current.type in _FUNCTIONS:
                key = f"{parent}.{count}"
                count += 1
                if len(self.plans) >= SCOPES_PER_ARTIFACT:
                    raise _Truncated("FLOW_SCOPE_LIMIT")
                name_node = current.child_by_field_name("name")
                kind = {
                    "arrow_function": "arrow",
                    "function_expression": "function_expression",
                    "function": "function_expression",
                    "generator_function": "function_expression",
                    "method_definition": "method",
                    "class_static_block": "class_static_block",
                }.get(current.type, "function")
                self.keys[self.ident(current)] = key
                self.plans.append(_Plan(key, parent, kind, current, _text(name_node) or None))
                # Parameter defaults run at call time inside the function: they belong to it.
                self._children(_named(current), key)
                continue
            stack.extend(reversed(_named(current)))

    def _owners(self) -> None:
        """Bind each symbol to the outermost function node inside its declaration region."""
        candidates = sorted(
            ((self.decoded(p.node), p) for p in self.plans[1:]),
            key=lambda item: (item[0][0], -item[0][1]),
        )
        for symbol_id, kind, start, end in self.regions:
            if kind not in {"function", "method"}:
                continue
            inside = [p for (s, e), p in candidates if s >= start and e <= end and p.owner is None]
            outermost = [
                p
                for p in inside
                if not any(
                    q is not p
                    and q.node.start_byte <= p.node.start_byte
                    and q.node.end_byte >= p.node.end_byte
                    for q in inside
                )
            ]
            if len(outermost) == 1:
                outermost[0].owner = symbol_id
            else:
                self.unmatched += 1


@dataclass(slots=True)
class _Scope:
    plan: _Plan
    ctx: _Context
    budget: _Budget
    builder: C.CfgBuilder = field(default_factory=lambda: C.CfgBuilder(CFG_NODES_PER_SCOPE))
    callsites: list[FlowCallsite] = field(default_factory=list)
    call_ordinal: dict[tuple[int, int], int] = field(default_factory=dict)
    bindings: list[dict[str, Any]] = field(default_factory=list)
    jsx: list[FlowJsxBinding] = field(default_factory=list)
    texts: list[DisplayText] = field(default_factory=list)
    returns: list[dict[str, Any]] = field(default_factory=list)
    raises: list[dict[str, Any]] = field(default_factory=list)
    yields: int = 0
    awaits: int = 0
    dynamic: set[str] = field(default_factory=set)
    called_names: set[str] = field(default_factory=set)
    executes: list[list[Any]] = field(default_factory=list)
    copies: dict[int, list[int]] = field(default_factory=dict)
    jsx_seen: set[tuple[int, int]] = field(default_factory=set)
    uses_with: bool = False

    # -- shapes ---------------------------------------------------------------------------
    def shape(self, node: tree_sitter.Node | None, depth: int = 0) -> dict[str, Any]:
        node = _unwrap(node)
        if node is None:
            return {"k": "none"}
        if depth >= SHAPE_DEPTH:
            return {"k": "deep"}
        d = depth + 1
        t = node.type
        if t in {"identifier", "shorthand_property_identifier", "property_identifier"}:
            name = _text(node)
            if name == "undefined":
                return {"k": "lit", "t": "undefined"}
            return {"k": "name", "id": name}
        if t == "this":
            return {"k": "this"}
        if t == "super":
            return {"k": "super"}
        if t == "member_expression":
            path: list[str] = []
            optional: list[int] = []
            root: tree_sitter.Node | None = node
            while root is not None and root.type == "member_expression":
                if root.child_by_field_name("optional_chain") is not None:
                    optional.append(len(path))
                path.append(_text(root.child_by_field_name("property")))
                root = _unwrap(root.child_by_field_name("object"))
            value: dict[str, Any] = {
                "k": "attr",
                "root": self.shape(root, d),
                "path": list(reversed(path)),
            }
            if optional:
                value["optional"] = [len(path) - 1 - i for i in optional]
            return value
        if t == "subscript_expression":
            return {
                "k": "subscript",
                "value": self.shape(node.child_by_field_name("object"), d),
                "index": [self.shape(node.child_by_field_name("index"), d)],
            }
        if t in {"call_expression", "new_expression"}:
            callee = node.child_by_field_name(
                "function" if t == "call_expression" else "constructor"
            )
            args_node = node.child_by_field_name("arguments")
            args = _named(args_node) if args_node is not None else []
            value = {
                "k": "call" if t == "call_expression" else "new",
                "callee": self.shape(callee, d),
                "args": [self.shape(a, d) for a in args[:SHAPE_ITEMS]],
            }
            if args_node is not None and args_node.type == "template_string":
                value["tagged"] = True
            ordinal = self.call_ordinal.get(self.ctx.ident(node))
            if ordinal is not None:
                value["callsite"] = ordinal
            return value
        if t in _STRING:
            text = _string_value(node)
            if text is not None:
                return _string_shape(text)
            return self.template(node, d)
        if t == "number":
            raw = _text(node).replace("_", "")
            try:
                return (
                    literal(int(raw, 0))
                    if raw.isdigit() or raw.lower().startswith(("0x", "0o", "0b"))
                    else literal(float(raw))
                )
            except ValueError:
                return {"k": "lit", "t": "number"}
        if t in {"true", "false"}:
            return literal(t == "true")
        if t == "null":
            return literal(None)
        if t == "await_expression":
            inner = _named(node)
            return {"k": "await", "value": self.shape(inner[0] if inner else None, d)}
        if t in _FUNCTIONS:
            return {"k": "scope", "key": self.ctx.keys.get(self.ctx.ident(node))}
        if t == "object":
            keys: list[dict[str, Any]] = []
            for child in _named(node)[: SHAPE_ITEMS * 2]:
                if child.type == "pair":
                    key_node = child.child_by_field_name("key")
                    key_name: str | None = (
                        _string_value(key_node)
                        if key_node is not None and key_node.type in _STRING
                        else _text(key_node)
                    )
                    keys.append(
                        {
                            "key": key_name,
                            "shape": self.shape(child.child_by_field_name("value"), d),
                        }
                    )
                elif child.type == "shorthand_property_identifier":
                    keys.append({"key": _text(child), "shape": {"k": "name", "id": _text(child)}})
                elif child.type == "method_definition":
                    keys.append(
                        {
                            "key": _text(child.child_by_field_name("name")),
                            "shape": self.shape(child, d),
                        }
                    )
                elif child.type == "spread_element":
                    inner = _named(child)
                    keys.append(
                        {"spread": True, "shape": self.shape(inner[0] if inner else None, d)}
                    )
            return {"k": "object", "keys": keys, "size": len(_named(node))}
        if t == "array":
            items = _named(node)
            return {
                "k": "collection",
                "t": "array",
                "items": [self.shape(i, d) for i in items[:SHAPE_ITEMS]],
                "size": len(items),
            }
        if t == "spread_element":
            inner = _named(node)
            return {"k": "starred", "value": self.shape(inner[0] if inner else None, d)}
        if t in {"jsx_element", "jsx_self_closing_element"}:
            opening = node.child_by_field_name("open_tag") if t == "jsx_element" else node
            return {
                "k": "jsx",
                "element": _text(opening.child_by_field_name("name")) if opening else None,
            }
        if t == "assignment_expression":
            return self.shape(node.child_by_field_name("right"), d)
        if t == "binary_expression":
            operator = _text(node.child_by_field_name("operator"))
            if operator in {"??", "||"}:
                return {
                    "k": "fallback",
                    "op": operator,
                    "options": [
                        self.shape(node.child_by_field_name("left"), d),
                        self.shape(node.child_by_field_name("right"), d),
                    ],
                }
        return {"k": "other", "node": t}

    def template(self, node: tree_sitter.Node, depth: int) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        for child in node.named_children[: SHAPE_ITEMS * 2]:
            if child.type == "string_fragment":
                parts.append({"s": _string_shape(_text(child))})
            elif child.type == "template_substitution":
                inner = _named(child)
                parts.append({"e": self.shape(inner[0] if inner else None, depth)})
        return {"k": "template", "parts": parts}

    def text(self, key: str, node: tree_sitter.Node | None) -> str | None:
        if node is None:
            return None
        item = display(f"{self.plan.key}#{key}", _text(node))
        self.texts.append(item)
        return item.key

    def bind(self, name: str, node: int, kind: str, value: dict[str, Any]) -> None:
        if len(self.bindings) >= BINDINGS_PER_SCOPE:
            raise _Truncated("FLOW_BINDING_LIMIT")
        self.bindings.append(
            {
                "name": name,
                "ordinal": len(self.bindings),
                "node": node,
                "kind": kind,
                "value": value,
            }
        )

    def bind_pattern(
        self, pattern: tree_sitter.Node | None, node: int, kind: str, value: dict[str, Any]
    ) -> None:
        pattern = _unwrap(pattern)
        if pattern is None:
            return
        t = pattern.type
        if t in {"identifier", "shorthand_property_identifier_pattern"}:
            self.bind(_text(pattern), node, kind, value)
        elif t == "object_pattern":
            for child in _named(pattern):
                if child.type == "shorthand_property_identifier_pattern":
                    self.bind(
                        _text(child),
                        node,
                        kind,
                        {"k": "property", "from": value, "key": _text(child)},
                    )
                elif child.type == "pair_pattern":
                    key = _text(child.child_by_field_name("key"))
                    self.bind_pattern(
                        child.child_by_field_name("value"),
                        node,
                        kind,
                        {"k": "property", "from": value, "key": key},
                    )
                elif child.type == "object_assignment_pattern":
                    left = child.child_by_field_name("left")
                    self.bind_pattern(
                        left,
                        node,
                        kind,
                        {"k": "property", "from": value, "key": _text(left), "default": True},
                    )
                elif child.type == "rest_pattern":
                    inner = _named(child)
                    self.bind_pattern(
                        inner[0] if inner else None, node, kind, {"k": "unpack_rest", "from": value}
                    )
        elif t == "array_pattern":
            for index, child in enumerate(_named(pattern)):
                if child.type == "rest_pattern":
                    inner = _named(child)
                    self.bind_pattern(
                        inner[0] if inner else None, node, kind, {"k": "unpack_rest", "from": value}
                    )
                else:
                    self.bind_pattern(
                        child, node, kind, {"k": "unpack", "from": value, "index": index}
                    )
        elif t == "assignment_pattern":
            self.bind_pattern(pattern.child_by_field_name("left"), node, kind, value)
        elif t == "rest_pattern":
            inner = _named(pattern)
            self.bind_pattern(
                inner[0] if inner else None, node, kind, {"k": "unpack_rest", "from": value}
            )
        elif t == "member_expression":
            obj = _unwrap(pattern.child_by_field_name("object"))
            if obj is not None and obj.type in {"identifier", "this"}:
                self.bind(
                    f"{_text(obj)}.{_text(pattern.child_by_field_name('property'))}",
                    node,
                    "attribute_store",
                    value,
                )

    # -- atoms ------------------------------------------------------------------------------
    def atom(self, test: tree_sitter.Node | None) -> dict[str, Any]:
        test = _unwrap(test)
        if test is None:
            return {"op": "opaque", "reason": "EMPTY"}
        t = test.type
        if t == "identifier":
            return {"op": "truthy", "var": _text(test)}
        if t in {"true", "false", "null", "number"} or (
            t in _STRING and _string_value(test) is not None
        ):
            return {"op": "const", "lit": self.shape(test)}
        if t == "binary_expression":
            operator = _text(test.child_by_field_name("operator"))
            left = _unwrap(test.child_by_field_name("left"))
            right = _unwrap(test.child_by_field_name("right"))
            if left is None or right is None:
                return {"op": "opaque", "reason": "BINARY"}
            if operator in {"===", "!==", "==", "!="}:
                for var, other in ((left, right), (right, left)):
                    if var.type == "identifier":
                        lit = self.shape(other)
                        if lit.get("k") == "lit":
                            nullish = lit.get("t") in {"none", "undefined"}
                            if operator in {"==", "!="} and not nullish:
                                return {"op": "opaque", "reason": "LOOSE_EQUALITY"}
                            if operator in {"==", "!="}:
                                return {"op": "nullish", "var": _text(var), "neg": operator == "!="}
                            return {
                                "op": "strict_eq",
                                "var": _text(var),
                                "lit": lit,
                                "neg": operator == "!==",
                            }
                    if (
                        var.type == "unary_expression"
                        and _text(var.child_by_field_name("operator")) == "typeof"
                    ):
                        arg = _unwrap(var.child_by_field_name("argument"))
                        name = _string_value(other) if other.type in _STRING else None
                        if (
                            arg is not None
                            and arg.type == "identifier"
                            and name is not None
                            and operator in {"===", "!=="}
                        ):
                            return {
                                "op": "typeof",
                                "var": _text(arg),
                                "type": name,
                                "neg": operator == "!==",
                            }
                return {"op": "opaque", "reason": "COMPARISON"}
            if operator == "instanceof" and left.type == "identifier":
                return {"op": "instanceof", "var": _text(left), "class": _text(right)}
            return {"op": "opaque", "reason": "BINARY"}
        if t == "member_expression":
            # Opaque for feasibility, but the identifier path is analysis-safe (e.g. a wrapper's
            # ``response.ok`` guard, recognised by Milestone B response handling).
            path = self.shape(test)
            names = _identifier_path(path)
            value: dict[str, Any] = {"op": "opaque", "reason": "ATTRIBUTE"}
            if names is not None:
                value["path"] = names
            return value
        if t in {"call_expression", "new_expression"}:
            return {"op": "opaque", "reason": "CALL"}
        return {"op": "opaque", "reason": t.upper()}


def _identifier_path(shape: dict[str, Any]) -> list[str] | None:
    """``a.b.c`` as identifiers when the member chain is rooted at a name or ``this``."""
    if shape.get("k") != "attr":
        return None
    root = shape.get("root") or {}
    if root.get("k") == "name":
        return [str(root["id"]), *shape.get("path", [])]
    if root.get("k") == "this":
        return ["this", *shape.get("path", [])]
    return None


def _string_shape(text: str) -> dict[str, Any]:
    value = literal(text)
    # URL-shaped path literals keep a sanitised path (origin+path only, as Stage 14's
    # ``url-origin-path-only.v1``) so Milestone B can match request descriptors.
    if text.startswith("/") and len(text) <= 256 and not any(ch.isspace() for ch in text):
        path = text.split("?", 1)[0].split("#", 1)[0]
        try:
            screen_content(path)
        except ContentRejectedError:
            return value
        value["path"] = path
    return value


class _Lowerer:
    def __init__(self, scope: _Scope) -> None:
        self.s = scope
        self.b = scope.builder
        self.ctx = scope.ctx

    # -- expressions ------------------------------------------------------------------------
    def expr(self, node: tree_sitter.Node | None, cur: int | None) -> int | None:
        if node is None or cur is None:
            return cur
        t = node.type
        if t in _TRANSPARENT:
            for child in _named(node):
                cur = self.expr(child, cur)
            return cur
        if t in _FUNCTIONS:
            return cur  # a nested scope is created, not run
        if t == "binary_expression":
            operator = _text(node.child_by_field_name("operator"))
            if operator in {"&&", "||", "??"}:
                return self.short_circuit(
                    node,
                    operator,
                    node.child_by_field_name("left"),
                    node.child_by_field_name("right"),
                    cur,
                )
        if t == "augmented_assignment_expression":
            operator = _text(node.child_by_field_name("operator"))
            left = node.child_by_field_name("left")
            if operator in {"&&=", "||=", "??="}:
                end = self.short_circuit(
                    node, operator[:-1], left, node.child_by_field_name("right"), cur
                )
                assert end is not None
                self.s.bind_pattern(left, end, "augassign", {"k": "other", "node": "augmented"})
                return end
            cur = self.expr(node.child_by_field_name("right"), cur)
            assert cur is not None
            self.s.bind_pattern(left, cur, "augassign", {"k": "other", "node": "augmented"})
            return cur
        if t == "ternary_expression":
            cur = self.expr(node.child_by_field_name("condition"), cur)
            assert cur is not None
            test = node.child_by_field_name("condition")
            branch = self.b.follow(
                cur,
                "branch",
                self.ctx.span(test),
                atom=self.s.atom(test),
                text=self.s.text(f"b{len(self.b.nodes)}", test),
            )
            join = self.b.node("join")
            for arm, kind in (
                (node.child_by_field_name("consequence"), C.TRUE),
                (node.child_by_field_name("alternative"), C.FALSE),
            ):
                start = self.b.node("operand", self.ctx.span(arm) if arm else None)
                self.b.edge(branch, start, kind)
                end = self.expr(arm, start)
                if end is not None:
                    self.b.edge(end, join)
            return join
        if t == "await_expression":
            for child in _named(node):
                cur = self.expr(child, cur)
            assert cur is not None
            self.s.awaits += 1
            wait = self.b.follow(cur, "await", self.ctx.span(node))
            self.b.may_raise(wait)
            resume = self.b.node("resume")
            self.b.edge(wait, resume, C.RESUME_NORMAL)
            return resume
        if t == "yield_expression":
            for child in _named(node):
                cur = self.expr(child, cur)
            assert cur is not None
            self.s.yields += 1
            delegate = any(not c.is_named and _text(c) == "*" for c in node.children)
            point = self.b.follow(cur, "yield", self.ctx.span(node), delegate=delegate or None)
            resume = self.b.node("resume")
            self.b.edge(point, resume, C.RESUME_NORMAL)
            thrown = self.b.node("thrown")
            self.b.edge(point, thrown, C.RESUME_THROW)
            self.b.jump(thrown, C.RAISE)
            return resume
        if t in {"call_expression", "new_expression"}:
            return self.call(node, cur)
        if t == "member_expression" and node.child_by_field_name("optional_chain") is not None:
            obj = node.child_by_field_name("object")
            cur = self.expr(obj, cur)
            assert cur is not None
            branch = self.b.follow(
                cur,
                "branch",
                self.ctx.span(obj),
                atom={
                    "op": "nullish",
                    "var": _text(obj) if obj is not None and obj.type == "identifier" else None,
                    "neg": True,
                },
                optional_chain=True,
            )
            join = self.b.node("join")
            self.b.edge(branch, join, C.FALSE)
            rest = self.b.node("operand")
            self.b.edge(branch, rest, C.TRUE)
            self.b.edge(rest, join)
            return join
        if t == "assignment_expression":
            cur = self.expr(node.child_by_field_name("right"), cur)
            left = node.child_by_field_name("left")
            if left is not None and left.type in {"member_expression", "subscript_expression"}:
                cur = self.expr(left.child_by_field_name("object"), cur)
            assert cur is not None
            self.s.bind_pattern(
                left, cur, "assign", self.s.shape(node.child_by_field_name("right"))
            )
            return cur
        if t in {"jsx_element", "jsx_self_closing_element"}:
            return self.jsx(node, cur)
        for child in _named(node):
            cur = self.expr(child, cur)
        return cur

    def short_circuit(
        self,
        node: tree_sitter.Node,
        operator: str,
        left: tree_sitter.Node | None,
        right: tree_sitter.Node | None,
        cur: int,
    ) -> int | None:
        end = self.expr(left, cur)
        assert end is not None
        atom = (
            self.s.atom(left)
            if operator != "??"
            else {
                "op": "nullish",
                "var": _text(left) if left is not None and left.type == "identifier" else None,
                "neg": False,
            }
        )
        branch = self.b.follow(
            end, "branch", self.ctx.span(left) if left else None, atom=atom, short_circuit=operator
        )
        join = self.b.node("join")
        rest = self.b.node("operand", self.ctx.span(right) if right else None)
        # && evaluates the right side when the left is truthy; || when falsy; ?? when nullish.
        go_on = C.TRUE if operator in {"&&", "??"} else C.FALSE
        self.b.edge(branch, rest, go_on)
        self.b.edge(branch, join, C.FALSE if go_on == C.TRUE else C.TRUE)
        right_end = self.expr(right, rest)
        if right_end is not None:
            self.b.edge(right_end, join)
        return join

    def call(self, node: tree_sitter.Node, cur: int | None) -> int | None:
        s, b = self.s, self.b
        callee = node.child_by_field_name(
            "function" if node.type == "call_expression" else "constructor"
        )
        optional_call = node.type == "call_expression" and any(
            not c.is_named and _text(c) == "?." for c in node.children
        )
        cur = self.expr(callee, cur)
        args_node = node.child_by_field_name("arguments")
        join: int | None = None
        if optional_call:
            assert cur is not None
            branch = b.follow(
                cur,
                "branch",
                self.ctx.span(callee) if callee else None,
                atom={
                    "op": "nullish",
                    "var": _text(callee)
                    if callee is not None and callee.type == "identifier"
                    else None,
                    "neg": True,
                },
                optional_chain=True,
            )
            join = b.node("join")
            b.edge(branch, join, C.FALSE)
            cur = b.node("operand")
            b.edge(branch, cur, C.TRUE)
        if args_node is not None:
            for argument in _named(args_node):
                cur = self.expr(argument, cur)
        assert cur is not None
        existing = s.call_ordinal.get(self.ctx.ident(node))
        if existing is not None:  # the same call in another finally copy
            s.copies.setdefault(existing, []).append(cur)
            b.may_raise(cur)
            if join is not None:
                b.edge(cur, join)
                return join
            return cur
        if len(s.callsites) >= CALLSITES_PER_SCOPE:
            raise _Truncated("FLOW_CALLSITE_LIMIT")
        if s.budget.callsites >= CALLSITES_PER_ARTIFACT:
            raise _Truncated("FLOW_ARTIFACT_CALLSITE_LIMIT")
        s.budget.callsites += 1
        ordinal = len(s.callsites)
        s.call_ordinal[self.ctx.ident(node)] = ordinal
        callee_node = _unwrap(callee)
        if callee_node is not None and callee_node.type == "identifier":
            s.called_names.add(_text(callee_node))
            if _text(callee_node) in _DYNAMIC:
                s.dynamic.add(_text(callee_node))
        arguments: list[dict[str, Any]] = []
        if args_node is not None and args_node.type != "template_string":
            for index, argument in enumerate(_named(args_node)[: SHAPE_ITEMS * 4]):
                if argument.type == "spread_element":
                    inner = _named(argument)
                    arguments.append({"star": True, "shape": s.shape(inner[0] if inner else None)})
                else:
                    arguments.append({"pos": index, "shape": s.shape(argument)})
        start, end = self.ctx.decoded(node)
        awaited = node.parent is not None and node.parent.type == "await_expression"
        s.callsites.append(
            FlowCallsite(
                ordinal,
                cur,
                {
                    **s.shape(callee),
                    **({"new": True} if node.type == "new_expression" else {}),
                    **({"optional_call": True} if optional_call else {}),
                },
                tuple(arguments),
                awaited,
                self.ctx.source_span(node),
                start,
                end,
                s.text(f"c{ordinal}", node),
            )
        )
        b.may_raise(cur)
        if join is not None:
            b.edge(cur, join)
            return join
        return cur

    def jsx(self, node: tree_sitter.Node, cur: int | None) -> int | None:
        s = self.s
        opening = node.child_by_field_name("open_tag") if node.type == "jsx_element" else node
        if opening is not None:
            name_node = opening.child_by_field_name("name")
            element = _text(name_node)
            intrinsic = (
                bool(element) and (element[0].islower() or "-" in element) and "." not in element
            )
            for attribute in opening.children_by_field_name("attribute"):
                if attribute.type == "jsx_attribute":
                    parts = _named(attribute)
                    attr_name = _text(parts[0]) if parts else ""
                    value_node = parts[1] if len(parts) > 1 else None
                    inner = None
                    if value_node is not None and value_node.type == "jsx_expression":
                        inner_nodes = _named(value_node)
                        inner = inner_nodes[0] if inner_nodes else None
                        cur = self.expr(inner, cur)
                    shape = (
                        s.shape(inner)
                        if inner is not None
                        else (
                            s.shape(value_node)
                            if value_node is not None
                            else {"k": "lit", "t": "bool", "v": True}
                        )
                    )
                    self.record_jsx(attribute, element, intrinsic, attr_name, shape, cur)
                elif attribute.type == "jsx_expression":
                    inner_nodes = _named(attribute)
                    inner = inner_nodes[0] if inner_nodes else None
                    target = (
                        _named(inner)[0]
                        if inner is not None and inner.type == "spread_element" and _named(inner)
                        else inner
                    )
                    cur = self.expr(target, cur)
                    self.record_jsx(
                        attribute,
                        element,
                        intrinsic,
                        "...",
                        {"k": "starred", "value": s.shape(target)},
                        cur,
                    )
        if node.type == "jsx_element":
            for child in _named(node):
                if child.type in {"jsx_expression", "jsx_element", "jsx_self_closing_element"}:
                    cur = self.expr(child, cur)
        return cur

    def record_jsx(
        self,
        node: tree_sitter.Node,
        element: str,
        intrinsic: bool,
        attribute: str,
        value: dict[str, Any],
        cur: int | None,
    ) -> None:
        s = self.s
        if cur is None or self.ctx.ident(node) in s.jsx_seen:
            return
        s.jsx_seen.add(self.ctx.ident(node))
        if len(s.jsx) >= JSX_PER_SCOPE:
            raise _Truncated("FLOW_JSX_LIMIT")
        start, end = self.ctx.decoded(node)
        s.jsx.append(
            FlowJsxBinding(
                len(s.jsx),
                cur,
                element,
                intrinsic,
                attribute,
                value,
                self.ctx.source_span(node),
                start,
                end,
            )
        )

    # -- conditions ---------------------------------------------------------------------
    def cond(
        self, test: tree_sitter.Node | None, cur: int
    ) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        test = _unwrap(test)
        if test is None:
            return [(cur, C.TRUE)], []
        if test.type == "binary_expression":
            operator = _text(test.child_by_field_name("operator"))
            if operator in {"&&", "||"}:
                left, right = test.child_by_field_name("left"), test.child_by_field_name("right")
                lt, lf = self.cond(left, cur)
                start = self.b.node("operand", self.ctx.span(right) if right else None)
                if operator == "&&":
                    self.attach(lt, start)
                    rt, rf = self.cond(right, start)
                    return rt, lf + rf
                self.attach(lf, start)
                rt, rf = self.cond(right, start)
                return lt + rt, rf
        if test.type == "unary_expression" and _text(test.child_by_field_name("operator")) == "!":
            t, f = self.cond(test.child_by_field_name("argument"), cur)
            return f, t
        evaluated = self.expr(test, cur)
        assert evaluated is not None
        atom = self.s.atom(test)
        if atom["op"] == "const":
            truthy = _const_truthy(atom["lit"])
            if truthy is not None:
                branch = self.b.follow(evaluated, "branch", self.ctx.span(test), atom=atom)
                return ([(branch, C.TRUE)], []) if truthy else ([], [(branch, C.FALSE)])
        branch = self.b.follow(
            evaluated,
            "branch",
            self.ctx.span(test),
            atom=atom,
            text=self.s.text(f"b{len(self.b.nodes)}", test),
        )
        return [(branch, C.TRUE)], [(branch, C.FALSE)]

    def attach(
        self, pending: Iterable[tuple[int, str]], target: int, kind: str | None = None
    ) -> None:
        for source, edge in pending:
            self.b.edge(source, target, kind or edge, None if kind is None else edge)

    # -- statements -----------------------------------------------------------------------
    def block(self, statements: Iterable[tree_sitter.Node], cur: int | None) -> int | None:
        for statement in statements:
            if statement.type == "comment":
                continue
            if cur is None:
                cur = self.b.node("unreachable", self.ctx.span(statement))
            cur = self.statement(statement, cur)
        return cur

    def body(self, node: tree_sitter.Node | None, cur: int | None) -> int | None:
        if node is None:
            return cur
        if node.type == "statement_block":
            return self.block(_named(node), cur)
        return self.block([node], cur)

    def statement(self, s: tree_sitter.Node, cur: int, label: str | None = None) -> int | None:
        b, scope, t = self.b, self.s, s.type
        span = self.ctx.span(s)
        if t == "statement_block":
            return self.block(_named(s), cur)
        if t in {"function_declaration", "generator_function_declaration"}:
            name = _text(s.child_by_field_name("name"))
            node = b.follow(cur, "def", span)
            scope.bind(
                name, node, "def", {"k": "scope", "key": self.ctx.keys.get(self.ctx.ident(s))}
            )
            return node
        if t in {"class_declaration", "abstract_class_declaration"}:
            node = b.follow(cur, "class", span)
            extends: dict[str, Any] | None = None
            for child in _named(s):
                if child.type in {"decorator", "class_heritage"}:
                    node = self.expr(child, node) or node
                if child.type == "class_heritage":
                    for clause in _named(child):
                        if clause.type == "extends_clause":
                            value = clause.child_by_field_name("value") or (
                                _named(clause)[0] if _named(clause) else None
                            )
                            extends = scope.shape(value) if value is not None else None
            fields = 0
            body = s.child_by_field_name("body")
            if body is not None:
                for member in _named(body):
                    if member.type == "class_static_block":
                        # Static blocks run when the class definition is evaluated.
                        scope.executes.append([node, self.ctx.keys.get(self.ctx.ident(member))])
                        b.may_raise(node)
                    elif member.type in {"public_field_definition", "field_definition"} and any(
                        _text(c) == "static" for c in member.children if not c.is_named
                    ):
                        node = self.expr(member.child_by_field_name("value"), node) or node
                    elif member.type in {"public_field_definition", "field_definition"} and (
                        member.child_by_field_name("value") is not None
                    ):
                        fields += 1  # instance initialisers run at construction (not lowered)
            name = _text(s.child_by_field_name("name"))
            if name:
                scope.bind(
                    name,
                    node,
                    "class",
                    {"k": "class", "span": span, "extends": extends, "instance_fields": fields},
                )
            return node
        if t in {"lexical_declaration", "variable_declaration"}:
            node = b.follow(cur, "declare", span)
            for declarator in _named(s):
                if declarator.type != "variable_declarator":
                    continue
                value = declarator.child_by_field_name("value")
                end = self.expr(value, node)
                assert end is not None
                node = end
                scope.bind_pattern(
                    declarator.child_by_field_name("name"),
                    node,
                    "declare",
                    scope.shape(value) if value is not None else {"k": "lit", "t": "undefined"},
                )
            return node
        if t == "expression_statement":
            node = b.follow(cur, "stmt", span)
            for child in _named(s):
                node = self.expr(child, node)  # type: ignore[assignment]
            return node
        if t == "return_statement":
            node = b.follow(cur, "return", span)
            values = _named(s)
            end = self.expr(values[0], node) if values else node
            assert end is not None
            if values and len(scope.returns) < SUMMARY_SHAPES:
                scope.returns.append(scope.shape(values[0]))
            b.jump(end, C.RETURN)
            return None
        if t == "throw_statement":
            node = b.follow(cur, "raise", span)
            values = _named(s)
            end = self.expr(values[0], node) if values else node
            assert end is not None
            if len(scope.raises) < SUMMARY_SHAPES:
                scope.raises.append(scope.shape(values[0]) if values else {"k": "none"})
            b.jump(end, C.RAISE)
            return None
        if t == "if_statement":
            node = b.follow(cur, "if", span)
            tr, fa = self.cond(s.child_by_field_name("condition"), node)
            ends: list[int | None] = []
            consequence = s.child_by_field_name("consequence")
            if tr:
                start = b.node("block", self.ctx.span(consequence) if consequence else None)
                self.attach(tr, start)
                ends.append(self.body(consequence, start))
            if fa:
                alternative = s.child_by_field_name("alternative")
                if alternative is not None:
                    inner = _named(alternative)
                    start = b.node("block", self.ctx.span(alternative))
                    self.attach(fa, start)
                    ends.append(self.block(inner, start))
                else:
                    join = b.node("join")
                    self.attach(fa, join)
                    ends.append(join)
            return self.join(ends)
        if t == "labeled_statement":
            name = _text(s.child_by_field_name("label"))
            labelled = s.child_by_field_name("body")
            if labelled is None:
                return cur
            if labelled.type in {
                "for_statement",
                "for_in_statement",
                "while_statement",
                "do_statement",
                "switch_statement",
            }:
                return self.statement(labelled, cur, label=name)
            after = b.node("after_label")
            frame = b.switch(name, after)  # a labelled block: ``break label`` leaves it
            end = self.statement(labelled, cur)
            b.pop(frame)
            if end is not None:
                b.edge(end, after)
            return after if self.reached(after) else None
        if t == "while_statement":
            head = b.follow(cur, "loop_head", span, loop="while")
            after = b.node("after_loop")
            tr, fa = self.cond(s.child_by_field_name("condition"), head)
            frame = b.loop(label, after, head)
            if tr:
                start = b.node("block")
                self.attach(tr, start)
                end = self.body(s.child_by_field_name("body"), start)
                if end is not None:
                    b.edge(end, head, C.LOOP_BACK)
            b.pop(frame)
            self.attach(fa, after, C.LOOP_EXIT)
            return after if self.reached(after) else None
        if t == "do_statement":
            start = b.follow(cur, "loop_head", span, loop="do")
            after = b.node("after_loop")
            test = b.node("loop_test")
            frame = b.loop(label, after, test)
            end = self.body(s.child_by_field_name("body"), start)
            if end is not None:
                b.edge(end, test)
            b.pop(frame)
            tr, fa = self.cond(s.child_by_field_name("condition"), test)
            self.attach(tr, start, C.LOOP_BACK)
            self.attach(fa, after, C.LOOP_EXIT)
            return after if self.reached(after) else None
        if t == "for_statement":
            node = b.follow(cur, "for", span)
            initializer = s.child_by_field_name("initializer")
            if initializer is not None:
                node = (
                    self.statement(initializer, node)
                    if initializer.type
                    in {"lexical_declaration", "variable_declaration", "expression_statement"}
                    else self.expr(initializer, node)
                ) or node
            head = b.follow(node, "loop_head", span, loop="for")
            after = b.node("after_loop")
            condition = s.child_by_field_name("condition")
            cond_node = (
                condition if condition is not None and condition.type != "empty_statement" else None
            )
            if cond_node is not None and cond_node.type == "expression_statement":
                inner = _named(cond_node)
                cond_node = inner[0] if inner else None
            tr, fa = self.cond(cond_node, head) if cond_node is not None else ([(head, C.TRUE)], [])
            increment = b.node("loop_increment")
            frame = b.loop(label, after, increment)
            start = b.node("block")
            self.attach(tr, start)
            end = self.body(s.child_by_field_name("body"), start)
            if end is not None:
                b.edge(end, increment)
            b.pop(frame)
            inc_end = self.expr(s.child_by_field_name("increment"), increment)
            if inc_end is not None and self.reached(increment):
                b.edge(inc_end, head, C.LOOP_BACK)
            self.attach(fa, after, C.LOOP_EXIT)
            return after if self.reached(after) else None
        if t == "for_in_statement":
            node = b.follow(cur, "for", span)
            right = s.child_by_field_name("right")
            node = self.expr(right, node) or node
            asynchronous = any(not c.is_named and _text(c) == "await" for c in s.children)
            operator = _text(s.child_by_field_name("operator"))
            head = b.follow(
                node,
                "loop_head",
                span,
                loop=f"for_{operator or 'in'}{'_await' if asynchronous else ''}",
            )
            b.may_raise(head)
            after = b.node("after_loop")
            frame = b.loop(label, after, head)
            start = b.node("block")
            b.edge(head, start, C.TRUE, "next")
            scope.bind_pattern(
                s.child_by_field_name("left"),
                start,
                "for_target",
                {
                    "k": "iter_item",
                    "from": scope.shape(right),
                    "operator": operator,
                    "async": asynchronous,
                },
            )
            end = self.body(s.child_by_field_name("body"), start)
            if end is not None:
                b.edge(end, head, C.LOOP_BACK)
            b.pop(frame)
            b.edge(head, after, C.LOOP_EXIT)
            return after
        if t in {"break_statement", "continue_statement"}:
            node = b.follow(cur, t.split("_")[0], span)
            target = s.child_by_field_name("label")
            b.jump(
                node,
                C.BREAK if t == "break_statement" else C.CONTINUE,
                _text(target) if target else None,
            )
            return None
        if t == "switch_statement":
            return self.switch(s, cur, label)
        if t == "try_statement":
            return self.try_(s, cur)
        if t in {"import_statement", "export_statement"}:
            if t == "export_statement":
                declaration = s.child_by_field_name("declaration")
                if declaration is not None:
                    return self.statement(declaration, cur)
                values = [
                    c
                    for c in _named(s)
                    if c.type not in {"export_clause", "string", "namespace_export"}
                ]
                node = b.follow(cur, "stmt", span)
                for value in values:
                    node = self.expr(value, node) or node
                return node
            return cur
        if t == "with_statement":
            scope.uses_with = True
        if (
            t in {"empty_statement", "debugger_statement"}
            or t.endswith("_declaration")
            and t not in {"lexical_declaration", "variable_declaration"}
        ):
            # type aliases, interfaces, enums, ambient/namespace declarations: no runtime flow
            # in this model (enums and namespaces are recorded as opaque statements).
            if t in {"enum_declaration", "internal_module", "module"}:
                return b.follow(cur, "stmt", span, opaque=t)
            return cur
        node = b.follow(cur, "stmt", span)
        for child in _named(s):
            node = self.expr(child, node) or node
        return node

    def reached(self, node: int) -> bool:
        return any(edge[1] == node for edge in self.b.edges)

    def join(self, ends: list[int | None]) -> int | None:
        live = [e for e in ends if e is not None]
        if not live:
            return None
        if len(live) == 1:
            return live[0]
        join = self.b.node("join")
        for end in live:
            self.b.edge(end, join)
        return join

    def switch(self, s: tree_sitter.Node, cur: int, label: str | None) -> int | None:
        b, scope = self.b, self.s
        node = b.follow(cur, "switch", self.ctx.span(s))
        node = self.expr(s.child_by_field_name("value"), node) or node
        after = b.node("after_switch")
        frame = b.switch(label, after)
        cases = (
            [
                c
                for c in _named(s.child_by_field_name("body"))
                if c.type in {"switch_case", "switch_default"}
            ]
            if s.child_by_field_name("body")
            else []
        )
        bodies = [b.node("case_body", self.ctx.span(c)) for c in cases]
        previous: int = node
        default_index: int | None = None
        for index, case in enumerate(cases):
            if case.type == "switch_default":
                default_index = index
                continue
            test = b.node(
                "case_test",
                self.ctx.span(case),
                text=scope.text(f"p{index}", case.child_by_field_name("value"))
                if case.child_by_field_name("value")
                else None,
            )
            b.edge(previous, test, C.NORMAL if previous == node else C.NO_MATCH)
            value_end = self.expr(case.child_by_field_name("value"), test) or test
            b.edge(value_end, bodies[index], C.CASE, f"c{index}")
            previous = value_end
        # No case matched: the default runs (wherever it is placed), else the switch is left.
        b.edge(
            previous,
            bodies[default_index] if default_index is not None else after,
            C.NO_MATCH,
            "default" if default_index is not None else None,
        )
        for index, case in enumerate(cases):
            end = self.block(case.children_by_field_name("body"), bodies[index])
            if end is not None:
                # Fall-through into the next case body in source order.
                b.edge(
                    end,
                    bodies[index + 1] if index + 1 < len(cases) else after,
                    C.FALLTHROUGH if index + 1 < len(cases) else C.NORMAL,
                )
        b.pop(frame)
        return after if self.reached(after) else None

    def try_(self, s: tree_sitter.Node, cur: int) -> int | None:
        b, scope = self.b, self.s
        start = b.follow(cur, "try", self.ctx.span(s))
        finalizer = s.child_by_field_name("finalizer")
        handler = s.child_by_field_name("handler")
        final_frame = (
            b.finally_(lambda entry: self.body(finalizer.child_by_field_name("body"), entry))
            if finalizer is not None
            else None
        )
        dispatch = b.node("dispatch", self.ctx.span(s)) if handler is not None else None
        handler_frame = b.handlers(dispatch) if dispatch is not None else None
        body_end = self.body(s.child_by_field_name("body"), start)
        if handler_frame is not None:
            b.pop(handler_frame)
        ends: list[int | None] = [body_end]
        if handler is not None and dispatch is not None:
            node = b.node("handler", self.ctx.span(handler))
            b.edge(dispatch, node, C.EXCEPTION, "h0")  # ``catch`` catches everything
            parameter = handler.child_by_field_name("parameter")
            if parameter is not None:
                scope.bind_pattern(parameter, node, "except_as", {"k": "exception", "types": []})
            ends.append(self.body(handler.child_by_field_name("body"), node))
        if final_frame is not None:
            exits = [b.normal_completion_through_finally(end, final_frame) for end in ends]
            b.pop(final_frame)
            return self.join(list(dict.fromkeys(exits)))
        return self.join(ends)


def _const_truthy(lit: dict[str, Any]) -> bool | None:
    kind = lit.get("t")
    if kind in {"none", "undefined"}:
        return False
    if kind == "bool":
        return bool(lit.get("v"))
    if kind == "int":
        return bool(lit.get("v"))
    if kind == "str":
        return bool(lit.get("len"))
    return None


def _parameters(scope: _Scope, node: tree_sitter.Node) -> list[dict[str, Any]]:
    if node.type == "arrow_function" and node.child_by_field_name("parameter") is not None:
        single = node.child_by_field_name("parameter")
        return [
            {
                "name": _text(single),
                "index": 0,
                "kind": "positional",
                "pattern": None,
                "default": None,
                "_node": single,
                "_default": None,
            }
        ]
    params = node.child_by_field_name("parameters")
    values: list[dict[str, Any]] = []
    if params is None:
        return values
    for index, param in enumerate(_named(params)):
        pattern = (
            param.child_by_field_name("pattern")
            if param.type in {"required_parameter", "optional_parameter"}
            else param
        )
        default = (
            param.child_by_field_name("value")
            if param.type in {"required_parameter", "optional_parameter"}
            else None
        )
        if pattern is not None and pattern.type == "assignment_pattern":
            default = pattern.child_by_field_name("right")
            pattern = pattern.child_by_field_name("left")
        kind = "rest" if pattern is not None and pattern.type == "rest_pattern" else "positional"
        name = _text(pattern) if pattern is not None and pattern.type == "identifier" else None
        values.append(
            {
                "name": name,
                "index": index,
                "kind": kind,
                "pattern": None if name else (pattern.type if pattern is not None else None),
                "optional": param.type == "optional_parameter" or None,
                "default": scope.shape(default) if default is not None else None,
                "_node": pattern,
                "_default": default,
            }
        )
    return values


def _callable_kind(plan: _Plan) -> str:
    if plan.kind == "module":
        return "module_code"
    if plan.kind == "class_static_block":
        return "class_code"
    node = plan.node
    words = {_text(c) for c in node.children if not c.is_named}
    asynchronous = "async" in words
    generator = "*" in words or node.type in {
        "generator_function_declaration",
        "generator_function",
    }
    if asynchronous and generator:
        return "js_async_generator_function"
    if generator:
        return "js_generator_function"
    if asynchronous:
        return "js_async_function"
    return "sync_function"


def _lower(plan: _Plan, ctx: _Context, budget: _Budget) -> tuple[FlowScope, list[DisplayText]]:
    scope = _Scope(plan, ctx, budget)
    lowerer = _Lowerer(scope)
    node = plan.node
    truncated: str | None = None
    params: list[dict[str, Any]] = []
    try:
        if plan.kind == "module":
            end = lowerer.block(ctx.kept, C.ENTRY)
        else:
            params = _parameters(scope, node)
            cur: int | None = C.ENTRY
            for param in params:
                pattern = param.pop("_node")
                default_node = param.pop("_default")
                if default_node is not None:
                    # Defaults are evaluated at call time, inside the function.
                    cur = lowerer.expr(default_node, cur)
                if param["name"]:
                    scope.bind(
                        param["name"], C.ENTRY, "param", {"k": "param", "index": param["index"]}
                    )
                else:
                    scope.bind_pattern(
                        pattern, C.ENTRY, "param", {"k": "param", "index": param["index"]}
                    )
            body = node.child_by_field_name("body")
            if body is not None and body.type != "statement_block" and plan.kind == "arrow":
                ret = lowerer.b.follow(cur, "return", ctx.span(body))
                end_expr = lowerer.expr(body, ret)
                if end_expr is not None:
                    if len(scope.returns) < SUMMARY_SHAPES:
                        scope.returns.append(scope.shape(body))
                    lowerer.b.jump(end_expr, C.RETURN)
                end = None
            else:
                end = lowerer.body(body, cur)
        if end is not None:
            lowerer.b.edge(end, C.EXIT, C.FALLTHROUGH)
        cfg = lowerer.b.graph()
    except C.CfgLimitError:
        truncated, cfg = "FLOW_CFG_NODE_LIMIT", {}
    except _Truncated as error:
        truncated, cfg = error.code, {}
    except RecursionError:
        truncated, cfg = "FLOW_NESTING_LIMIT", {}
    start, end_offset = ctx.decoded(node) if plan.kind != "module" else (0, None)
    span = ctx.source_span(node) if plan.kind != "module" else SourceSpan(1, None, 0, None)
    if truncated:
        scope.callsites.clear()
        scope.jsx.clear()
        scope.texts.clear()
        summary: dict[str, Any] = {"truncated": truncated}
    else:
        graph = C.Graph.of(cfg)
        reach = graph.reachable(C.ENTRY)
        rebound = {b["name"] for b in scope.bindings if b["kind"] != "param"}
        for param in params:
            param.pop("_node", None)
            param.pop("_default", None)
        summary = {
            "parameters": params,
            "returns": scope.returns,
            "raises": scope.raises,
            "invokes_parameter": sorted(
                p["index"]
                for p in params
                if p["name"] and p["name"] in scope.called_names and p["name"] not in rebound
            ),
            "normal_exit_reachable": C.summary_flags(graph)["normal_exit_reachable"],
            "yield_points": scope.yields,
            "await_points": scope.awaits,
            "dynamic": sorted(scope.dynamic),
            "uses_with": scope.uses_with,
            "executes": scope.executes,
            "bindings": scope.bindings,
            "unreachable_nodes": sorted(
                n["id"] for n in cfg["nodes"] if n["id"] not in reach and n["id"] > C.RAISE_EXIT
            ),
        }
    return (
        FlowScope(
            plan.key,
            plan.parent,
            plan.kind,
            _callable_kind(plan),
            plan.name,
            span,
            start,
            end_offset,
            truncated,
            cfg,
            summary,
            tuple(
                replace(c, copies=tuple(scope.copies.get(c.ordinal, ()))) for c in scope.callsites
            ),
            tuple(scope.jsx),
            plan.owner,
        ),
        scope.texts,
    )


def extract_ts_flow(locator: str, data: bytes, regions: list[SymbolRegion]) -> FlowExtraction:
    contract = flow_pass_contract(TS_FLOW)
    fingerprint = hashlib.sha256(data).hexdigest()

    def failed(code: str) -> FlowExtraction:
        value = contract or {}
        return FlowExtraction(
            TS_FLOW,
            value,
            digest(value) if value else "0" * 64,
            "failed",
            fingerprint,
            (ParseDiagnostic(code, "TS/JS flow evidence could not be extracted.", "error"),),
        )

    if contract is None:
        return failed("LANGUAGE_UNAVAILABLE")
    try:
        grammar, _ = grammar_for(locator)
        source, _ = decode_source(data)
        screen_content(source)
        language = _language(grammar)
    except ContentRejectedError as error:
        return failed(error.code)
    except UnicodeDecodeError:
        return failed("ENCODING_ERROR")
    except LanguageUnavailableError as error:
        return failed(str(error))
    encoded = source.encode("utf-8")
    tree = tree_sitter.Parser(language).parse(encoded)
    ctx = _Context(source, encoded, tree, regions)
    diagnostics: list[ParseDiagnostic] = []
    try:
        ctx.plan()
    except _Truncated as error:
        diagnostics.append(
            ParseDiagnostic(error.code, "Too many scopes; the rest were not planned.", "warning")
        )
    except RecursionError:
        return failed("FLOW_NESTING_LIMIT")
    for start, end in ctx.dropped:
        diagnostics.append(
            ParseDiagnostic(
                "SYNTAX_RECOVERED",
                "A top-level statement with a syntax error was skipped.",
                "warning",
                SourceSpan(start, end),
            )
        )
    budget = _Budget()
    scopes: list[FlowScope] = []
    texts: list[DisplayText] = []
    for plan in ctx.plans:
        scope, scope_texts = _lower(plan, ctx, budget)
        scopes.append(scope)
        texts.extend(scope_texts)
    for code in sorted({s.truncated for s in scopes if s.truncated}):
        diagnostics.append(
            ParseDiagnostic(code, "A scope exceeded a flow cap and was truncated.", "warning")
        )
    if ctx.unmatched:
        diagnostics.append(
            ParseDiagnostic(
                "FLOW_DECLARATION_UNMATCHED",
                "A declaration had no single matching scope.",
                "warning",
            )
        )
    return FlowExtraction(
        TS_FLOW,
        contract,
        digest(contract),
        "partial" if diagnostics else "extracted",
        fingerprint,
        tuple(diagnostics),
        tuple(scopes),
        tuple(texts),
        ctx.unmatched,
    )
