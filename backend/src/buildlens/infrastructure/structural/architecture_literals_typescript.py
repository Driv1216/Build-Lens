"""Additive ``ts-architecture-literals.v1`` pass over TS/TSX/JS/JSX (tree-sitter, parse only).

``http_call`` and ``url_call`` rows carry ``callee_scope`` (``ts-callee-scope.v1``): whether
the callee's root identifier is declared by an enclosing function, block or the module, so a
parameter or local named ``fetch``, ``window``, ``globalThis`` or ``axios`` is never taken for
the global or imported sink.

It re-parses the same bytes and never alters the declaration extraction. Rows (plan F7, C-2, C-3):

* ``http_call``: syntactic sink candidates ``fetch``/``window.fetch``/``globalThis.fetch``,
  ``axios(...)`` and ``axios.<verb>(...)`` with the URL argument structure and init literal;
* ``url_call``: any other call with a URL-shaped string/template argument (never request
  evidence by itself; used only to bind callers of a qualified wrapper);
* ``url_constant``: module-level ``const N = <URL> | <expr> ?? <URL> | <expr> || <URL>``;
* ``url_helper``: a module-level function whose body is one return of a URL-shaped value;
* ``frontend_route``: JSX ``<Route path=... element={<X/>}>``;
* ``import_guard``: module-scope ``require`` nested in a conditional construct;
* ``type_import``: statement-level ``import type`` (distinguishes it from ``{ type A }``);
* ``opaque_dynamic_import``: ``import()``/``require()`` with a non-literal argument.

Context flags describe syntax only: ``conditional`` (if/else, ternary, logical right side,
switch, catch), ``loop`` and ``callback`` (a nested function inside the enclosing top-level one).
Top-level statements containing syntax errors are skipped and the extraction is ``partial``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from uuid import uuid4

import tree_sitter

from buildlens.domain.architecture_literals import (
    CLASSIFICATION_KINDS,
    CLASSIFICATION_ROWS_PER_ARTIFACT,
    ROWS_PER_ARTIFACT,
    Counters,
    LiteralKind,
    LiteralRejected,
    Part,
    clean,
    identifier_chain,
    path_value,
    url_shaped,
    url_template,
)
from buildlens.domain.extractors import TS_LITERALS, extractor_contract
from buildlens.domain.source import ContentRejectedError, decode_source, digest, screen_content
from buildlens.domain.structural import ArchitectureLiteral, ArtifactExtraction, ParseDiagnostic
from buildlens.infrastructure.structural.typescript import (
    LanguageUnavailableError,
    _language,
    _Lines,
    _string_value,
    _text,
    _unnamed,
    grammar_for,
)

_FUNCTIONS = {
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "function",
    "generator_function",
    "arrow_function",
    "method_definition",
}
_LOOPS = {"for_statement", "for_in_statement", "while_statement", "do_statement"}
# Scopes whose declarations shadow a callee name (functions are handled with ``_FUNCTIONS``).
_SCOPES = {
    "program", "statement_block", "class_body", "switch_body", "catch_clause",
    "for_statement", "for_in_statement",
}  # fmt: skip
_DECLARATIONS = {
    "function_declaration", "generator_function_declaration", "class_declaration",
    "abstract_class_declaration", "enum_declaration", "internal_module", "module",
}  # fmt: skip
_FETCH = {"fetch", "window.fetch", "globalThis.fetch"}
_AXIOS_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}
# axios.<verb>(url, config) vs axios.<verb>(url, data, config)
_AXIOS_CONFIG_POSITION = {"get": 1, "delete": 1, "head": 1, "options": 1, "post": 2, "put": 2}
_AXIOS_CONFIG_POSITION.update({"patch": 2, "request": 0})
_METHOD_LIMIT = 16
_WRAPPED = {"parenthesized_expression", "as_expression", "satisfies_expression"}
_WRAPPED.add("non_null_expression")


@dataclass(frozen=True, slots=True)
class _Context:
    depth: int = 0
    function: str | None = None
    conditional: bool = False
    loop: bool = False
    callback: bool = False
    guarded: bool = False
    construct: str | None = None
    parameters: tuple[str, ...] = ()
    route: int | None = None  # ordinal of the enclosing ``<Route>`` row


def _unwrap(node: tree_sitter.Node | None) -> tree_sitter.Node | None:
    while node is not None and node.type in _WRAPPED:
        node = node.named_children[0] if node.named_children else None
    return node


def _declared_name(node: tree_sitter.Node) -> str | None:
    """Name of a module-level function declaration or ``const X = () => ...``."""
    if node.type in {"function_declaration", "generator_function_declaration"}:
        return _text(node.child_by_field_name("name")) or None
    parent = node.parent
    if parent is not None and parent.type == "variable_declarator":
        name = parent.child_by_field_name("name")
        if name is not None and name.type == "identifier":
            return _text(name)
    return None


def _module_level(node: tree_sitter.Node) -> bool:
    """True when ``node`` is a top-level statement or the declaration of an export statement."""
    parent = node.parent
    if parent is not None and parent.type == "export_statement":
        parent = parent.parent
    return parent is not None and parent.type == "program"


class _Walker:
    def __init__(self, data: bytes) -> None:
        self.lines = _Lines(data)
        self.rows: list[ArchitectureLiteral] = []
        self.counters = Counters()
        self.evidence_rows = 0
        self.classification_rows = 0
        self.classification_complete = True
        self._declared: dict[int, frozenset[str]] = {}

    def add(
        self,
        kind: LiteralKind,
        node: tree_sitter.Node,
        subject: str | None,
        attributes: dict[str, object],
    ) -> None:
        if kind in CLASSIFICATION_KINDS:
            if self.classification_rows >= CLASSIFICATION_ROWS_PER_ARTIFACT:
                self.classification_complete = False
                self.counters.dropped_rows += 1
                return
            self.classification_rows += 1
        else:
            if self.evidence_rows >= ROWS_PER_ARTIFACT:
                self.counters.dropped_rows += 1
                return
            self.evidence_rows += 1
        self.rows.append(
            ArchitectureLiteral(len(self.rows), kind, self.lines.node(node), subject, attributes)
        )

    # -- value structure -------------------------------------------------------------------
    def parts(self, node: tree_sitter.Node | None, budget: int = 32) -> list[Part]:
        node = _unwrap(node)
        if node is None:
            return []
        if budget <= 0:
            return [("e", "<expr>")]
        if node.type == "string":
            return [("s", "".join(_text(c) for c in node.named_children if c.type != "comment"))]
        if node.type == "template_string":
            values: list[Part] = []
            for child in node.named_children:
                if child.type == "template_substitution":
                    inner = child.named_children[0] if child.named_children else None
                    values.append(("e", _text(inner)))
                elif child.type in {"string_fragment", "escape_sequence"}:
                    values.append(("s", _text(child)))
            return values
        if node.type == "binary_expression" and _text(node.child_by_field_name("operator")) == "+":
            return [
                *self.parts(node.child_by_field_name("left"), budget - 1),
                *self.parts(node.child_by_field_name("right"), budget - 1),
            ]
        return [("e", _text(node))]

    def template(self, parts: list[Part]) -> dict[str, object] | None:
        try:
            value = url_template(parts)
        except LiteralRejected as error:
            self.counters.reject(str(error))
            return None
        if value["truncated"]:
            self.counters.truncated += 1
        return value

    def url(self, node: tree_sitter.Node | None) -> dict[str, object]:
        if node is None:
            return {"state": "absent"}
        value = self.template(self.parts(node))
        return {"state": "sanitized_out"} if value is None else {"state": "template", **value}

    def init(self, node: tree_sitter.Node | None) -> dict[str, object]:
        node = _unwrap(node)
        if node is None:
            return {"kind": "absent"}
        if node.type == "identifier":
            return {"kind": "identifier", "name": _text(node)}
        if node.type != "object":
            return {"kind": "other"}
        method: dict[str, object] = {"state": "absent"}
        spreads: list[str] = []
        computed = False
        url: dict[str, object] | None = None
        keys: list[str] = []
        for child in node.named_children:
            if child.type == "spread_element":
                argument = child.named_children[0] if child.named_children else None
                spreads.append(identifier_chain(_text(argument)) or "<expr>")
            elif child.type == "shorthand_property_identifier":
                keys.append(_text(child))
                if _text(child) == "method":
                    method = {"state": "dynamic"}
            elif child.type == "pair":
                key = child.child_by_field_name("key")
                value = _unwrap(child.child_by_field_name("value"))
                if key is None or key.type == "computed_property_name":
                    computed = True
                    continue
                name = _string_value(key) if key.type == "string" else _text(key)
                keys.append(identifier_chain(name) or "<key>")
                if name == "method":
                    literal = _string_value(value) if value is not None else None
                    if literal is None:
                        method = {"state": "dynamic"}
                    else:
                        try:
                            text, truncated = clean(literal, _METHOD_LIMIT)
                        except LiteralRejected as error:
                            self.counters.reject(str(error))
                            method = {"state": "sanitized_out"}
                        else:
                            method = (
                                {"state": "dynamic"}
                                if truncated
                                else {"state": "literal", "value": text}
                            )
                elif name == "url":
                    url = self.url(value)
            elif child.type == "method_definition":
                keys.append("<method>")
        result: dict[str, object] = {
            "kind": "object",
            "method": method,
            "spreads": spreads,
            "computed": computed,
            "keys": keys[:32],
        }
        if url is not None:
            result["url"] = url
        return result

    def shape(self, node: tree_sitter.Node | None) -> str:
        node = _unwrap(node)
        if node is None:
            return "absent"
        if node.type in {"string", "template_string", "binary_expression"} and url_shaped(
            self.parts(node)
        ):
            return "url"
        if node.type == "object":
            return "object"
        if node.type == "identifier":
            return "identifier"
        return "other"

    # -- traversal -------------------------------------------------------------------------
    def run(self, root: tree_sitter.Node) -> list[tuple[int, int]]:
        dropped: list[tuple[int, int]] = []
        stack: list[tuple[tree_sitter.Node, _Context]] = []
        for statement in reversed(root.named_children):
            if statement.type == "comment":
                continue
            if statement.has_error or statement.is_error or statement.is_missing:
                span = self.lines.node(statement)
                dropped.append((span.start_line or 0, span.end_line or 0))
                continue
            stack.append((statement, _Context()))
        dropped.reverse()
        while stack:
            node, context = stack.pop()
            kind = node.type
            if kind == "import_statement" and "type" in _unnamed(node):
                self.add("type_import", node, None, {"statement": "import_type"})
            elif kind in {"lexical_declaration", "variable_declaration"} and _module_level(node):
                self.constants(node)
            elif kind in {"function_declaration", "generator_function_declaration"}:
                self.helper(node, node)
            elif kind == "call_expression":
                self.call(node, context)
            elif kind == "jsx_self_closing_element":
                self.route(node, context.route)
            elif kind == "jsx_element":
                opening = node.child_by_field_name("open_tag")
                ordinal = self.route(opening, context.route) if opening is not None else None
                if ordinal is not None:
                    context = replace(context, route=ordinal)
            for child, child_context in reversed(self.children(node, context)):
                stack.append((child, child_context))
        return dropped

    def children(
        self, node: tree_sitter.Node, context: _Context
    ) -> list[tuple[tree_sitter.Node, _Context]]:
        kind = node.type
        values: list[tuple[tree_sitter.Node, _Context]] = []
        if kind in _FUNCTIONS:
            name = _declared_name(node) if context.depth == 0 else None
            if context.depth == 0 and name is not None and not _module_level_function(node):
                name = None
            inner = _Context(
                depth=context.depth + 1,
                function=name if context.depth == 0 else context.function,
                callback=context.depth >= 1,
                guarded=context.guarded,
                construct=context.construct,
                parameters=_parameters(node) if context.depth == 0 else context.parameters,
                route=context.route,
            )
            return [(child, inner) for child in node.named_children]
        for index, child in enumerate(node.children):
            if not child.is_named:
                continue
            field = node.field_name_for_child(index)
            child_context = context
            if kind == "if_statement" and field in {"consequence", "alternative"}:
                child_context = _flag(context, "if", conditional=True)
            elif kind == "ternary_expression" and field in {"consequence", "alternative"}:
                child_context = _flag(context, "ternary", conditional=True)
            elif (
                kind == "binary_expression"
                and field == "right"
                and _text(node.child_by_field_name("operator")) in {"&&", "||", "??"}
            ):
                child_context = _flag(context, "logical", conditional=True)
            elif kind in {"switch_statement", "catch_clause"} and field != "value":
                child_context = _flag(context, kind.split("_")[0], conditional=True)
            elif kind == "try_statement":
                child_context = _flag(context, "try", conditional=False)
            elif kind in _LOOPS and field in {"body", None}:
                child_context = replace_loop(context)
            values.append((child, child_context))
        return values

    # -- rows ------------------------------------------------------------------------------
    def call(self, node: tree_sitter.Node, context: _Context) -> None:
        callee = node.child_by_field_name("function")
        arguments_node = node.child_by_field_name("arguments")
        arguments = [
            c
            for c in (arguments_node.named_children if arguments_node else [])
            if c.type != "comment"
        ]
        first = arguments[0] if arguments else None
        if callee is None:
            return
        flags = {
            "function": context.function,
            "parameters": list(context.parameters),
            "depth": context.depth,
            "conditional": context.conditional,
            "loop": context.loop,
            "callback": context.callback,
        }
        if callee.type == "import" or _text(callee) == "require":
            form = "dynamic_import" if callee.type == "import" else "require"
            if _string_value(first) is None:
                self.add(
                    "opaque_dynamic_import",
                    node,
                    None,
                    {"form": form, "scope": "module" if context.depth == 0 else "function"},
                )
            elif form == "require" and context.depth == 0 and context.guarded:
                self.add(
                    "import_guard",
                    node,
                    None,
                    {"guard": "conditional", "construct": context.construct, "form": "require"},
                )
            return
        text = identifier_chain(_text(callee))
        if text is None:
            return
        flags["callee_scope"] = self.callee_scope(node, text.split(".", 1)[0])
        if text in _FETCH:
            self.add(
                "http_call",
                node,
                text,
                {
                    "sink": "fetch",
                    "url": self.url(first),
                    "init": self.init(arguments[1] if len(arguments) > 1 else None),
                    "arity": len(arguments),
                    **flags,
                },
            )
            return
        verb = text.split(".", 1)[1] if text.startswith("axios.") else None
        if text == "axios" or (verb in _AXIOS_VERBS and text.count(".") == 1):
            if verb is None or verb == "request":
                config = (
                    arguments[1]
                    if len(arguments) > 1
                    else first
                    if self.shape(first) == "object"
                    else None
                )
                url_node = first if self.shape(first) != "object" else None
            else:
                position = _AXIOS_CONFIG_POSITION[verb]
                config = arguments[position] if len(arguments) > position else None
                url_node = first
            self.add(
                "http_call",
                node,
                text,
                {
                    "sink": "axios",
                    "verb": verb,
                    "url": self.url(url_node) if url_node is not None else {"state": "absent"},
                    "init": self.init(config),
                    "arity": len(arguments),
                    **flags,
                },
            )
            return
        shapes = [self.shape(argument) for argument in arguments[:4]]
        if "url" not in shapes:
            return
        position = shapes.index("url")
        self.add(
            "url_call",
            node,
            text,
            {
                "callee_root": text.split(".", 1)[0],
                "position": position,
                "url": self.url(arguments[position]),
                "arguments": shapes,
                "objects": {
                    str(index): self.init(arguments[index])
                    for index, shape in enumerate(shapes)
                    if shape in {"object", "identifier"}
                },
                "arity": len(arguments),
                **flags,
            },
        )

    # -- binding scope (``ts-callee-scope.v1``) ----------------------------------------------
    def callee_scope(self, call: tree_sitter.Node, name: str) -> str:
        """Where the callee's root identifier is declared, by syntax alone.

        ``local``: a parameter, catch binding, loop binding, or a ``var``/``let``/``const``/
        function/class/enum/namespace declaration of an enclosing function or block.
        ``module``: such a declaration at module level (the name is then neither the global
        nor an import, but may be a same-file wrapper). ``unknown``: a ``with`` statement
        encloses the call. ``none``: no declaration in any enclosing scope; imports are
        checked separately by the projection."""
        node = call.parent
        while node is not None:
            kind = node.type
            if kind == "with_statement":
                return "unknown"
            if kind in _FUNCTIONS or kind in _SCOPES:
                if name in self._declarations(node):
                    return "module" if kind == "program" else "local"
            node = node.parent
        return "none"

    def _declarations(self, scope: tree_sitter.Node) -> frozenset[str]:
        cached = self._declared.get(scope.id)
        if cached is not None:
            return cached
        names: set[str] = set()
        kind = scope.type
        if kind in _FUNCTIONS:
            parameters = scope.child_by_field_name("parameters")
            for parameter in parameters.named_children if parameters else []:
                names |= _pattern_names(parameter)
            single = scope.child_by_field_name("parameter")
            if single is not None:
                names |= _pattern_names(single)
            if kind in {"function_expression", "function", "generator_function"}:
                own = scope.child_by_field_name("name")
                if own is not None:
                    names.add(_text(own))
            body = scope.child_by_field_name("body")
            if body is not None:
                names |= _hoisted(body)
        elif kind == "program":
            for statement in scope.named_children:
                names |= _block_declarations(statement)
            names |= _hoisted(scope)
        elif kind == "catch_clause":
            binding = scope.child_by_field_name("parameter")
            if binding is not None:
                names |= _pattern_names(binding)
        elif kind in {"for_in_statement", "for_statement"}:
            if kind == "for_in_statement" and any(
                child.type in {"var", "let", "const"} for child in scope.children
            ):
                left = scope.child_by_field_name("left")
                if left is not None:
                    names |= _pattern_names(left)
            initializer = scope.child_by_field_name("initializer")
            if initializer is not None:
                names |= _block_declarations(initializer)
        elif kind == "switch_body":
            for case in scope.named_children:
                for statement in case.named_children:
                    names |= _block_declarations(statement)
        else:  # statement_block, class_body
            for statement in scope.named_children:
                names |= _block_declarations(statement)
        value = frozenset(names)
        self._declared[scope.id] = value
        return value

    def constants(self, node: tree_sitter.Node) -> None:
        if (
            node.type == "lexical_declaration"
            and _text(node.child_by_field_name("kind")) != "const"
        ):
            return
        for declarator in node.named_children:
            if declarator.type != "variable_declarator":
                continue
            name = declarator.child_by_field_name("name")
            value = _unwrap(declarator.child_by_field_name("value"))
            if name is None or name.type != "identifier" or value is None:
                continue
            if value.type in {"arrow_function", "function_expression", "function"}:
                self.helper(value, declarator)
                continue
            operator = (
                _text(value.child_by_field_name("operator"))
                if value.type == "binary_expression"
                else None
            )
            if operator in {"??", "||"}:
                right = value.child_by_field_name("right")
                parts = self.parts(right)
                if not url_shaped(parts):
                    continue
                template = self.template(parts)
                if template is None:
                    continue
                left = _unwrap(value.child_by_field_name("left"))
                self.add(
                    "url_constant",
                    declarator,
                    _text(name),
                    {
                        "value": template,
                        "operator": operator,
                        "primary": identifier_chain(_member_chain(left)) or "<expr>",
                    },
                )
                continue
            parts = self.parts(value)
            if value.type in {"string", "template_string", "binary_expression"} and url_shaped(
                parts
            ):
                template = self.template(parts)
                if template is not None:
                    self.add(
                        "url_constant",
                        declarator,
                        _text(name),
                        {"value": template, "operator": None, "primary": None},
                    )

    def helper(self, function: tree_sitter.Node, anchor: tree_sitter.Node) -> None:
        if not _module_level_function(function):
            return
        name = _declared_name(function)
        body = function.child_by_field_name("body")
        if name is None or body is None:
            return
        returned: tree_sitter.Node | None
        if body.type == "statement_block":
            statements = [c for c in body.named_children if c.type != "comment"]
            if len(statements) != 1 or statements[0].type != "return_statement":
                return
            returned = statements[0].named_children[0] if statements[0].named_children else None
        else:
            returned = body
        returned = _unwrap(returned)
        if returned is None or returned.type not in {
            "string",
            "template_string",
            "binary_expression",
        }:
            return
        parts = self.parts(returned)
        if not url_shaped(parts):
            return
        template = self.template(parts)
        if template is None:
            return
        self.add(
            "url_helper",
            anchor,
            name,
            {"parameters": list(_parameters(function)), "value": template},
        )

    def route(self, node: tree_sitter.Node, parent: int | None) -> int | None:
        """A ``<Route>`` row with its enclosing route; returns the new row's ordinal."""
        if _text(node.child_by_field_name("name")) != "Route":
            return None
        path: dict[str, object] = {"state": "absent"}
        element: str | None = None
        index = False
        for attribute in node.named_children:
            if attribute.type != "jsx_attribute" or not attribute.named_children:
                continue
            key = _text(attribute.named_children[0])
            value = attribute.named_children[1] if len(attribute.named_children) > 1 else None
            if key == "index":
                index = True
            elif key == "path":
                if value is not None and value.type == "jsx_expression" and value.named_children:
                    value = value.named_children[0]
                literal = _string_value(value)
                if literal is None:
                    path = {"state": "opaque"}
                else:
                    try:
                        text, truncated = path_value(literal)
                    except LiteralRejected as error:
                        self.counters.reject(str(error))
                        path = {"state": "sanitized_out"}
                    else:
                        path = (
                            {"state": "truncated"}
                            if truncated
                            else {"state": "literal", "value": text}
                        )
            elif key == "element" and value is not None and value.type == "jsx_expression":
                inner = value.named_children[0] if value.named_children else None
                if inner is not None and inner.type == "jsx_element":
                    inner = inner.child_by_field_name("open_tag")
                if inner is not None and inner.type in {
                    "jsx_self_closing_element",
                    "jsx_opening_element",
                }:
                    element = identifier_chain(_text(inner.child_by_field_name("name")))
        before = len(self.rows)
        self.add("frontend_route", node, element, {"path": path, "index": index, "parent": parent})
        return before if len(self.rows) > before else None


def _pattern_names(node: tree_sitter.Node) -> set[str]:
    """Identifiers bound by a parameter or declaration pattern (defaults and types excluded)."""
    kind = node.type
    if kind in {"identifier", "shorthand_property_identifier_pattern"}:
        return {_text(node)}
    if kind in {"required_parameter", "optional_parameter"}:
        pattern = node.child_by_field_name("pattern")
        return _pattern_names(pattern) if pattern is not None else set()
    if kind == "pair_pattern":
        value = node.child_by_field_name("value")
        return _pattern_names(value) if value is not None else set()
    if kind in {"assignment_pattern", "object_assignment_pattern"}:
        left = node.child_by_field_name("left")
        return _pattern_names(left) if left is not None else set()
    if kind in {"object_pattern", "array_pattern", "rest_pattern"}:
        names: set[str] = set()
        for child in node.named_children:
            names |= _pattern_names(child)
        return names
    return set()


def _block_declarations(statement: tree_sitter.Node) -> set[str]:
    """Names one statement declares in its own block (``var`` is handled by ``_hoisted``)."""
    kind = statement.type
    if kind in {"export_statement", "ambient_declaration"}:
        names: set[str] = set()
        for child in statement.named_children:
            names |= _block_declarations(child)
        return names
    if kind in {"lexical_declaration", "variable_declaration"}:
        names = set()
        for declarator in statement.named_children:
            if declarator.type == "variable_declarator":
                target = declarator.child_by_field_name("name")
                if target is not None:
                    names |= _pattern_names(target)
        return names
    if kind in _DECLARATIONS:
        name = statement.child_by_field_name("name")
        return {_text(name)} if name is not None else set()
    return set()


def _hoisted(body: tree_sitter.Node) -> set[str]:
    """``var`` and function declarations anywhere in a function body or the module, not
    inside nested functions (function-scoped, so conservatively bound everywhere in it)."""
    names: set[str] = set()
    stack = list(body.named_children)
    while stack:
        node = stack.pop()
        if node.type in _FUNCTIONS and node.type not in {
            "function_declaration",
            "generator_function_declaration",
        }:
            continue
        if node.type == "variable_declaration" or node.type in {
            "function_declaration",
            "generator_function_declaration",
        }:
            names |= _block_declarations(node)
            if node.type != "variable_declaration":
                continue
        stack.extend(node.named_children)
    return names


def _parameters(function: tree_sitter.Node) -> tuple[str, ...]:
    """Positional parameter names (``<pattern>`` for destructuring), at most 16."""
    parameters = function.child_by_field_name("parameters")
    names = []
    for parameter in parameters.named_children if parameters else []:
        if parameter.type == "comment":
            continue
        pattern = parameter.child_by_field_name("pattern") or parameter
        names.append(_text(pattern) if pattern.type == "identifier" else "<pattern>")
    if parameters is None:
        single = function.child_by_field_name("parameter")
        names = [_text(single)] if single is not None else []
    return tuple(names[:16])


def _member_chain(node: tree_sitter.Node | None) -> str | None:
    text = _text(node) if node is not None else ""
    return text.replace("import.meta", "import_meta", 1) if text.startswith("import.meta") else text


def _module_level_function(node: tree_sitter.Node) -> bool:
    if node.type in {"function_declaration", "generator_function_declaration"}:
        return _module_level(node)
    declarator = node.parent
    declaration = declarator.parent if declarator is not None else None
    return (
        declarator is not None
        and declarator.type == "variable_declarator"
        and declaration is not None
        and declaration.type in {"lexical_declaration", "variable_declaration"}
        and _module_level(declaration)
    )


def _flag(context: _Context, construct: str, conditional: bool) -> _Context:
    return replace(
        context,
        conditional=context.conditional or conditional,
        guarded=True,
        construct=construct,
    )


def replace_loop(context: _Context) -> _Context:
    return replace(context, loop=True, guarded=True, construct="loop")


def extract_ts_literals(locator: str, data: bytes) -> ArtifactExtraction:
    contract = extractor_contract(TS_LITERALS)
    input_fingerprint = hashlib.sha256(data).hexdigest()

    def failed(code: str) -> ArtifactExtraction:
        return ArtifactExtraction(
            uuid4(),
            TS_LITERALS,
            contract or {},
            digest(contract) if contract else "0" * 64,
            "failed",
            input_fingerprint,
            (ParseDiagnostic(code, "Architecture literals could not be extracted.", "error"),),
        )

    if contract is None:
        return failed("LANGUAGE_UNAVAILABLE")
    try:
        grammar, _ = grammar_for(locator)
        source, _encoding = decode_source(data)
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
    walker = _Walker(encoded)
    dropped = walker.run(tree.root_node)
    counters = walker.counters
    diagnostics = tuple(
        ParseDiagnostic(
            "SYNTAX_RECOVERED",
            "A top-level statement containing a syntax error was not scanned for literals.",
            "warning",
        )
        for _ in dropped[:1]
    )
    if counters.dropped_rows:
        diagnostics += (
            ParseDiagnostic(
                "LITERAL_ROW_LIMIT",
                "Literal rows beyond the per-artifact cap were dropped.",
                "warning",
            ),
        )
    return ArtifactExtraction(
        uuid4(),
        TS_LITERALS,
        contract,
        digest(contract),
        "partial" if dropped or counters.dropped_rows else "extracted",
        input_fingerprint,
        diagnostics,
        artifact_facts={
            "grammar": grammar,
            "rows": len(walker.rows),
            "dropped_rows": counters.dropped_rows,
            "dropped_line_ranges": [list(item) for item in dropped],
            "sanitized_out": counters.sanitized_out,
            "sanitized_codes": dict(sorted(counters.codes.items())),
            "truncated_values": counters.truncated,
            "classification_complete": walker.classification_complete and not dropped,
        },
        literals=tuple(walker.rows),
    )
