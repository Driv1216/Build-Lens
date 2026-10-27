"""Parse-only TS/TSX/JS declaration extraction with tree-sitter (``*-declarations.v1``).

Nothing is executed or resolved: no tsconfig, no module resolution, no Node. Partial-parse
guarantees (Stage 13 plan G1–G3): any top-level statement whose range contains an ERROR or
MISSING node is dropped together with everything nested in it, and the extraction is then
``partial`` with the dropped line ranges listed.

Coordinates: tree-sitter parses the decoded source re-encoded as BOM-less UTF-8. Line and
column fields follow the existing ``ast`` convention (1-based lines, UTF-8 byte columns);
``start_offset``/``end_offset`` are decoded Unicode half-open offsets, the chunk convention.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID, uuid4

import tree_sitter
import tree_sitter_javascript
import tree_sitter_typescript

from buildlens.domain.extractors import (
    JAVASCRIPT_DECLARATIONS,
    TREE_SITTER_CANARY_DIGEST,
    TYPESCRIPT_DECLARATIONS,
    extractor_contract,
)
from buildlens.domain.source import ContentRejectedError, decode_source, digest, screen_content
from buildlens.domain.structural import (
    ArtifactExtraction,
    DecoratorObservation,
    ExportReference,
    ImportForm,
    ImportReference,
    ObservedExpression,
    ParseDiagnostic,
    SourceSpan,
    Symbol,
    SymbolFacts,
    SymbolKind,
    SymbolReference,
)

FRAGMENT_LIMIT = 512
TARGET_LIMIT = 512
REFERENCES_PER_SYMBOL = 256
REFERENCES_PER_ARTIFACT = 4096
_FUNCTION_VALUES = {
    "arrow_function",
    "function_expression",
    "function",
    "generator_function",
}
_WRAPPERS = {"memo", "forwardRef", "React.memo", "React.forwardRef"}
_HOOK = re.compile(r"^(?:[A-Za-z_$][\w$]*\.)?use[A-Z0-9][\w$]*$")
_SPACE: dict[str, Literal["value", "type", "namespace"]] = {
    "function": "value",
    "class": "value",
    "method": "value",
    "enum": "value",
    "interface": "type",
    "type_alias": "type",
    "namespace": "namespace",
}


class LanguageUnavailableError(Exception):
    pass


def grammar_for(locator: str) -> tuple[str, str]:
    """(grammar, extractor) chosen strictly by extension."""
    lowered = locator.lower()
    if lowered.endswith(".tsx"):
        return "tsx", TYPESCRIPT_DECLARATIONS
    if lowered.endswith(".ts"):
        return "typescript", TYPESCRIPT_DECLARATIONS
    if lowered.endswith((".js", ".jsx")):
        return "javascript", JAVASCRIPT_DECLARATIONS
    raise LanguageUnavailableError("UNSUPPORTED_LANGUAGE")


def _language(grammar: str) -> tree_sitter.Language:
    capsule = {
        "tsx": tree_sitter_typescript.language_tsx,
        "typescript": tree_sitter_typescript.language_typescript,
        "javascript": tree_sitter_javascript.language,
    }[grammar]()
    language = tree_sitter.Language(capsule)
    abi = language.abi_version
    if not tree_sitter.MIN_COMPATIBLE_LANGUAGE_VERSION <= abi <= tree_sitter.LANGUAGE_VERSION:
        raise LanguageUnavailableError("GRAMMAR_ABI_INCOMPATIBLE")
    return language


class _Offsets:
    """Map BOM-less UTF-8 byte offsets to decoded Unicode code-point offsets."""

    def __init__(self, text: str) -> None:
        self._ascii = text.isascii()
        self._bytes: list[int] = [0]
        self._chars: list[int] = [0]
        if not self._ascii:
            position = 0
            for index, character in enumerate(text):
                width = len(character.encode("utf-8"))
                position += width
                if width > 1:
                    self._bytes.append(position)
                    self._chars.append(index + 1)

    def char(self, byte: int) -> int:
        if self._ascii:
            return byte
        index = bisect_right(self._bytes, byte) - 1
        return self._chars[index] + (byte - self._bytes[index])


def _text(node: tree_sitter.Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    data = value.encode("utf-8")
    if len(data) <= limit:
        return value, False
    return data[:limit].decode("utf-8", errors="ignore"), True


class _Lines:
    """Line/column positions computed from byte offsets.

    ``Node.start_point``/``end_point`` are deliberately never used: in py-tree-sitter 0.26.0 an
    integer read from a ``Point`` (row/column above the small-int cache) can be freed with the
    Point while still referenced, corrupting the heap. Byte offsets are plain integers. Lines
    follow the chunk convention (``\\r\\n``, ``\\r`` or ``\\n``); columns are UTF-8 byte columns,
    the existing ``ast`` convention.
    """

    def __init__(self, data: bytes) -> None:
        self._starts = [0, *(match.end() for match in re.finditer(rb"\r\n|\r|\n", data))]

    def point(self, byte: int) -> tuple[int, int]:
        index = bisect_right(self._starts, byte) - 1
        return index + 1, byte - self._starts[index]

    def span(self, start: int, end: int) -> SourceSpan:
        (start_line, start_column), (end_line, end_column) = self.point(start), self.point(end)
        return SourceSpan(start_line, end_line, start_column, end_column)

    def node(self, node: tree_sitter.Node) -> SourceSpan:
        return self.span(node.start_byte, node.end_byte)


def _fragment(node: tree_sitter.Node | None, lines: _Lines) -> ObservedExpression | None:
    if node is None:
        return None
    value, truncated = _bounded(_text(node), FRAGMENT_LIMIT)
    return ObservedExpression(value, lines.node(node), truncated=truncated)


def _unnamed(node: tree_sitter.Node) -> set[str]:
    return {child.type for child in node.children if not child.is_named}


def _string_value(node: tree_sitter.Node | None) -> str | None:
    if node is None or node.type not in {"string", "template_string"}:
        return None
    if node.type == "template_string" and any(c.is_named for c in node.children):
        if any(c.type == "template_substitution" for c in node.children):
            return None
    return "".join(_text(c) for c in node.children if c.type == "string_fragment")


def _state(node: tree_sitter.Node) -> str:
    """Comment- and whitespace-insensitive structural fingerprint of a declaration."""
    value = hashlib.sha256(b"ts-declaration-cst.v1\0")
    stack: list[tuple[tree_sitter.Node, bool]] = [(node, False)]
    while stack:
        current, closing = stack.pop()
        if closing:
            value.update(b")")
            continue
        if current.type == "comment":
            continue
        if current.child_count == 0:
            value.update(current.type.encode() + b"\0")
            if current.is_named:
                value.update(current.text or b"")
            value.update(b"\1")
            continue
        value.update(b"(" + current.type.encode() + b"\0")
        stack.append((current, True))
        stack.extend((child, False) for child in reversed(current.children))
    return value.hexdigest()


def _function_value(declarator: tree_sitter.Node) -> tuple[tree_sitter.Node | None, str | None]:
    """The function bound by a declarator (unwrapping one memo/forwardRef call)."""
    value = declarator.child_by_field_name("value")
    if value is None:
        return None, None
    if value.type in _FUNCTION_VALUES:
        return value, None
    target = _wrapped(value)
    return (target, _text(value.child_by_field_name("function"))) if target else (None, None)


def _wrapped(node: tree_sitter.Node) -> tree_sitter.Node | None:
    if node.type != "call_expression":
        return None
    if _text(node.child_by_field_name("function")) not in _WRAPPERS:
        return None
    arguments = node.child_by_field_name("arguments")
    values = list(arguments.named_children) if arguments else []
    if len(values) == 1 and values[0].type in _FUNCTION_VALUES:
        return values[0]
    return None


@dataclass
class _Pending:
    node: tree_sitter.Node
    region: tree_sitter.Node
    kind: SymbolKind
    name: str
    name_source: str
    form: str
    exported: str | None
    parent: _Pending | None
    body: tree_sitter.Node | None
    wrapper: str | None = None
    signatures: int = 0
    merged: list[tree_sitter.Node] = field(default_factory=list)
    symbol_id: UUID = field(default_factory=uuid4)


@dataclass
class TypeScriptExtraction:
    outcome: Literal["parsed", "failed"]
    encoding: str | None
    line_count: int | None
    symbols: tuple[Symbol, ...]
    imports: tuple[ImportReference, ...]
    extraction: ArtifactExtraction
    diagnostics: tuple[ParseDiagnostic, ...] = ()


class _Extractor:
    def __init__(self, source: str, data: bytes, tree: tree_sitter.Tree) -> None:
        self.source, self.data, self.tree = source, data, tree
        self.offsets = _Offsets(source)
        self.lines = _Lines(data)
        self.pending: list[_Pending] = []
        self.imports: list[ImportReference] = []
        self.exports: list[ExportReference] = []
        self.references: list[SymbolReference] = []
        self.reference_counts: dict[UUID | None, int] = {}
        self.truncated_references = 0
        self.statement_ordinal = 0
        self.dropped: list[tuple[int, int]] = []

    # -- traversal -------------------------------------------------------------------------
    def run(self) -> None:
        statements = [c for c in self.tree.root_node.named_children if c.type != "comment"]
        kept: list[tree_sitter.Node] = []
        for statement in statements:
            if statement.has_error or statement.is_error or statement.is_missing:
                span = self.lines.node(statement)
                self.dropped.append((span.start_line or 0, span.end_line or 0))
            else:
                kept.append(statement)
        self._container(kept, None)

    def _container(self, statements: list[tree_sitter.Node], parent: _Pending | None) -> None:
        merged: dict[tuple[str, str], _Pending] = {}
        index = 0
        while index < len(statements):
            statement = statements[index]
            declaration, region, exported = self._unwrap(statement)
            if declaration is not None and declaration.type in {
                "function_signature",
                "method_signature",
            }:
                name = _text(declaration.child_by_field_name("name"))
                group = [statement]
                cursor = index + 1
                while cursor < len(statements):
                    following, _, _ = self._unwrap(statements[cursor])
                    if following is None or _text(following.child_by_field_name("name")) != name:
                        break
                    group.append(statements[cursor])
                    if following.type in {"function_declaration", "method_definition"}:
                        break
                    if following.type not in {"function_signature", "method_signature"}:
                        group.pop()
                        break
                    cursor += 1
                implementation, _, _ = self._unwrap(group[-1])
                assert implementation is not None
                if len(group) == 1:
                    self._declaration(implementation, statement, exported, parent, merged)
                    index += 1
                    continue
                pending = self._declare(
                    implementation,
                    group[0],
                    "method" if parent and parent.kind == "class" else "function",
                    name or "<anonymous>",
                    "identifier",
                    "overload_group",
                    exported,
                    parent,
                    implementation.child_by_field_name("body"),
                )
                pending.signatures = sum(
                    1
                    for member in group
                    if (unwrapped := self._unwrap(member)[0]) is not None
                    and unwrapped.type in {"function_signature", "method_signature"}
                )
                pending.region = group[0]
                pending.merged = [group[-1]]
                self._function_body(pending)
                index += len(group)
                continue
            if declaration is not None:
                handled = self._declaration(declaration, region, exported, parent, merged)
                if not handled:
                    self._scan(statement, parent)
            else:
                self._statement(statement, parent)
            index += 1

    def _unwrap(
        self, statement: tree_sitter.Node
    ) -> tuple[tree_sitter.Node | None, tree_sitter.Node, str | None]:
        node, exported = statement, None
        if node.type == "export_statement":
            inner = node.child_by_field_name("declaration")
            if inner is None:
                value = node.child_by_field_name("value")
                if "default" in _unnamed(node) and value is not None:
                    if value.type in {"class", *_FUNCTION_VALUES} or _wrapped(value):
                        return value, statement, "default"
                return None, statement, None
            node, exported = inner, "default" if "default" in _unnamed(node) else "named"
        if node.type == "ambient_declaration":
            inner_nodes = [c for c in node.named_children if c.type != "comment"]
            if len(inner_nodes) == 1:
                node = inner_nodes[0]
        if node.type == "expression_statement":
            inner_nodes = node.named_children
            if len(inner_nodes) == 1 and inner_nodes[0].type in {"internal_module", "module"}:
                node = inner_nodes[0]
        if node.type in {
            "function_declaration",
            "generator_function_declaration",
            "function_signature",
            "class_declaration",
            "abstract_class_declaration",
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
            "internal_module",
            "module",
            "lexical_declaration",
            "variable_declaration",
            "method_definition",
            "method_signature",
            "abstract_method_signature",
            "public_field_definition",
            "field_definition",
        }:
            return node, statement, exported
        return None, statement, None

    def _declaration(
        self,
        node: tree_sitter.Node,
        region: tree_sitter.Node,
        exported: str | None,
        parent: _Pending | None,
        merged: dict[tuple[str, str], _Pending],
    ) -> bool:
        kind_name = node.type
        in_class = parent is not None and parent.kind == "class"
        default = exported == "default"
        if kind_name in {"lexical_declaration", "variable_declaration"}:
            declarators = [c for c in node.named_children if c.type == "variable_declarator"]
            single = len(declarators) == 1
            for declarator in declarators:
                target, wrapper = _function_value(declarator)
                name_node = declarator.child_by_field_name("name")
                if target is None or name_node is None or name_node.type != "identifier":
                    if exported and name_node is not None and name_node.type == "identifier":
                        name = _text(name_node)
                        self._export(None, "named", name, name, None, declarator)
                    self._scan(declarator, parent)
                    continue
                pending = self._declare(
                    target,
                    region if single else declarator,
                    "method" if in_class else "function",
                    _text(name_node),
                    "identifier",
                    "variable_function",
                    exported,
                    parent,
                    target.child_by_field_name("body"),
                )
                pending.wrapper = wrapper
                self._function_body(pending)
            return True
        if kind_name in {"public_field_definition", "field_definition"}:
            value = node.child_by_field_name("value")
            name_node = node.child_by_field_name("name") or node.child_by_field_name("property")
            if value is None or value.type not in _FUNCTION_VALUES or name_node is None:
                self._scan(node, parent)
                return True
            pending = self._declare(
                value,
                node,
                "method",
                _text(name_node),
                "computed" if name_node.type == "computed_property_name" else "identifier",
                "field_function",
                None,
                parent,
                value.child_by_field_name("body"),
            )
            self._decorators(node, pending)
            self._function_body(pending)
            return True
        if kind_name in {"method_definition", "abstract_method_signature", "method_signature"}:
            name_node = node.child_by_field_name("name")
            pending = self._declare(
                node,
                node,
                "method" if in_class else "function",
                _text(name_node) or "<anonymous>",
                "computed"
                if name_node is not None and name_node.type == "computed_property_name"
                else "identifier",
                "def" if kind_name == "method_definition" else "signature",
                None,
                parent,
                node.child_by_field_name("body"),
            )
            self._decorators(node, pending)
            self._function_body(pending)
            return True
        name_node = node.child_by_field_name("name")
        literal = _string_value(name_node) if name_node is not None else None
        name = literal if literal is not None else _text(name_node)
        name_source = "identifier" if name else "export_default"
        name = name or "default"
        if kind_name in {
            "function_declaration",
            "generator_function_declaration",
            "function_signature",
            *_FUNCTION_VALUES,
        }:
            target = node
            wrapper = None
            if default and node.type == "call_expression":
                target = _wrapped(node) or node
                wrapper = _text(node.child_by_field_name("function"))
            pending = self._declare(
                target,
                region,
                "method" if in_class else "function",
                name,
                name_source,
                "signature" if kind_name == "function_signature" else "def",
                exported,
                parent,
                target.child_by_field_name("body"),
            )
            pending.wrapper = wrapper
            self._function_body(pending)
            return True
        if default and _wrapped(node):
            target = _wrapped(node)
            assert target is not None
            pending = self._declare(
                target,
                region,
                "function",
                "default",
                "export_default",
                "variable_function",
                exported,
                parent,
                target.child_by_field_name("body"),
            )
            pending.wrapper = _text(node.child_by_field_name("function"))
            self._function_body(pending)
            return True
        if kind_name in {"class_declaration", "abstract_class_declaration", "class"}:
            pending = self._declare(
                node, region, "class", name, name_source, "class", exported, parent, None
            )
            self._decorators(node, pending)
            for heritage in node.named_children:
                if heritage.type != "class_heritage":
                    continue
                for clause in heritage.named_children:
                    # TS wraps bases in extends/implements clauses; JS holds the expression.
                    values = (
                        clause.named_children
                        if clause.type in {"extends_clause", "implements_clause"}
                        else [clause]
                    )
                    for value in values:
                        if value.type not in {"type_arguments", "comment"}:
                            self._reference(pending.symbol_id, "base", value)
            body = node.child_by_field_name("body")
            if body is not None:
                members = [c for c in body.named_children if c.type not in {"comment", "decorator"}]
                self._container(members, pending)
            return True
        if kind_name in {"interface_declaration", "internal_module", "module"}:
            kind: SymbolKind = "interface" if kind_name == "interface_declaration" else "namespace"
            key = (kind, name)
            body = node.child_by_field_name("body")
            existing = merged.get(key)
            if existing is not None:
                existing.merged.append(region)
                target_parent = existing
            else:
                target_parent = self._declare(
                    node, region, kind, name, name_source, "def", exported, parent, None
                )
                merged[key] = target_parent
            if kind == "interface":
                for value in node.named_children:
                    if value.type == "extends_type_clause":
                        for item in value.named_children:
                            self._reference(target_parent.symbol_id, "base", item)
            elif body is not None:
                self._container(
                    [c for c in body.named_children if c.type != "comment"], target_parent
                )
            return True
        if kind_name in {"type_alias_declaration", "enum_declaration"}:
            self._declare(
                node,
                region,
                "type_alias" if kind_name == "type_alias_declaration" else "enum",
                name,
                name_source,
                "def",
                exported,
                parent,
                None,
            )
            return True
        return False

    def _declare(
        self,
        node: tree_sitter.Node,
        region: tree_sitter.Node,
        kind: SymbolKind,
        name: str,
        name_source: str,
        form: str,
        exported: str | None,
        parent: _Pending | None,
        body: tree_sitter.Node | None,
    ) -> _Pending:
        pending = _Pending(node, region, kind, name, name_source, form, exported, parent, body)
        if node.parent is not None and node.parent.type == "ambient_declaration":
            pending.form = "ambient"
        self.pending.append(pending)
        if exported:
            self._export(
                pending.symbol_id,
                "default" if exported == "default" else "named",
                "default" if exported == "default" else name,
                name,
                None,
                region,
            )
        return pending

    def _decorators(self, node: tree_sitter.Node, pending: _Pending) -> None:
        for child in node.named_children:
            if child.type == "decorator":
                value = child.named_children[0] if child.named_children else child
                callee = (
                    value.child_by_field_name("function")
                    if value.type == "call_expression"
                    else value
                )
                self._reference(pending.symbol_id, "decorator", callee or value)

    def _function_body(self, pending: _Pending) -> None:
        if pending.body is not None:
            self._scan(pending.body, pending)

    def _statement(self, statement: tree_sitter.Node, parent: _Pending | None) -> None:
        kind = statement.type
        if kind == "import_statement":
            self._import(statement, parent)
            return
        if kind == "export_statement":
            self._export_statement(statement, parent)
            return
        if kind == "expression_statement":
            self._commonjs(statement)
        self._scan(statement, parent)

    # -- nested scanning -------------------------------------------------------------------
    def _scan(self, root: tree_sitter.Node, owner: _Pending | None) -> None:
        """Visit a subtree for nested declarations and references (iterative, no recursion)."""
        stack: list[tree_sitter.Node] = [root]
        owner_id = owner.symbol_id if owner else None
        while stack:
            node = stack.pop()
            kind = node.type
            if node is not root and kind in {
                "function_declaration",
                "generator_function_declaration",
                "class_declaration",
                "abstract_class_declaration",
            }:
                self._declaration(node, node, None, owner, {})
                continue
            if (
                node is not root
                and kind in {"lexical_declaration", "variable_declaration"}
                and any(
                    _function_value(d)[0] is not None
                    for d in node.named_children
                    if d.type == "variable_declarator"
                )
            ):
                self._declaration(node, node, None, owner, {})
                continue
            if kind == "call_expression":
                callee = node.child_by_field_name("function")
                arguments = node.child_by_field_name("arguments")
                first = (
                    arguments.named_children[0] if arguments and arguments.named_children else None
                )
                if callee is not None and callee.type == "import":
                    self._dynamic(node, "dynamic_import", first, owner_id)
                elif callee is not None and _text(callee) == "require":
                    self._dynamic(node, "require", first, owner_id)
                elif callee is not None:
                    text = _text(callee)
                    self._reference(owner_id, "hook_call" if _HOOK.match(text) else "call", callee)
            elif kind in {"jsx_opening_element", "jsx_self_closing_element"}:
                name = node.child_by_field_name("name")
                if name is not None:
                    self._reference(owner_id, "jsx_element", name)
            elif kind == "throw_statement":
                value = node.named_children[0] if node.named_children else None
                if value is not None and value.type == "new_expression":
                    constructor = value.child_by_field_name("constructor")
                    if constructor is not None:
                        self._reference(owner_id, "raise", constructor)
            elif kind in _FUNCTION_VALUES or kind == "class":
                pass  # anonymous callbacks are not symbols; their content still belongs to owner
            stack.extend(reversed(node.named_children))

    # -- imports/exports -------------------------------------------------------------------
    def _import(self, node: tree_sitter.Node, owner: _Pending | None) -> None:
        owner_id = owner.symbol_id if owner else None
        source = _string_value(node.child_by_field_name("source"))
        type_only = "type" in _unnamed(node)
        form: ImportForm = "es_import_type" if type_only else "es_import"
        items: list[tuple[str, str | None, ImportForm]] = []
        for child in node.named_children:
            if child.type == "import_require_clause":
                binding = child.named_children[0] if child.named_children else None
                module = _string_value(child.child_by_field_name("source"))
                items.append(("*", _text(binding) or None, "require"))
                source = module
            elif child.type == "import_clause":
                for part in child.named_children:
                    if part.type == "identifier":
                        items.append(("default", _text(part), form))
                    elif part.type == "namespace_import":
                        alias = part.named_children[0] if part.named_children else None
                        items.append(("*", _text(alias) or None, form))
                    elif part.type == "named_imports":
                        for specifier in part.named_children:
                            if specifier.type != "import_specifier":
                                continue
                            name_node = specifier.child_by_field_name("name")
                            alias_node = specifier.child_by_field_name("alias")
                            item_form: ImportForm = (
                                "es_import_type" if "type" in _unnamed(specifier) else form
                            )
                            items.append((_text(name_node), _text(alias_node) or None, item_form))
        if source is None:
            return
        if not items:
            items.append(("", None, form))
        for ordinal, (imported, local, item_form) in enumerate(items):
            self.imports.append(
                ImportReference(
                    uuid4(),
                    UUID(int=0),
                    owner_id,
                    self.statement_ordinal,
                    ordinal,
                    item_form,
                    source,
                    imported,
                    local,
                    0,
                    self.lines.node(node),
                )
            )
        self.statement_ordinal += 1

    def _dynamic(
        self,
        node: tree_sitter.Node,
        form: ImportForm,
        argument: tree_sitter.Node | None,
        owner_id: UUID | None,
    ) -> None:
        module = _string_value(argument)
        if module is None:
            return
        self.imports.append(
            ImportReference(
                uuid4(),
                UUID(int=0),
                owner_id,
                self.statement_ordinal,
                0,
                form,
                module,
                "*",
                None,
                0,
                self.lines.node(node),
            )
        )
        self.statement_ordinal += 1

    def _export(
        self,
        owner_id: UUID | None,
        kind: str,
        name: str,
        local_name: str | None,
        source_module: str | None,
        node: tree_sitter.Node,
    ) -> None:
        self.exports.append(
            ExportReference(
                len(self.exports),
                owner_id,
                kind,
                name,
                local_name,
                source_module,
                self.lines.node(node),
            )
        )

    def _export_statement(self, node: tree_sitter.Node, parent: _Pending | None) -> None:
        source = _string_value(node.child_by_field_name("source"))
        clause = next((c for c in node.named_children if c.type == "export_clause"), None)
        if clause is not None:
            for specifier in clause.named_children:
                if specifier.type != "export_specifier":
                    continue
                name = _text(specifier.child_by_field_name("name"))
                alias = _text(specifier.child_by_field_name("alias")) or None
                self._export(
                    None, "reexport" if source else "named", alias or name, name, source, node
                )
            return
        if source is not None:
            namespace = next((c for c in node.named_children if c.type == "namespace_export"), None)
            alias = (
                _text(namespace.named_children[0])
                if namespace and namespace.named_children
                else "*"
            )
            self._export(None, "reexport_all", alias, None, source, node)
            return
        value = node.child_by_field_name("value")
        if value is not None and "default" in _unnamed(node):
            local = None
            if value.type == "identifier":
                local = _text(value)
            elif value.type == "call_expression":
                arguments = value.child_by_field_name("arguments")
                first = (
                    arguments.named_children[0] if arguments and arguments.named_children else None
                )
                local = _text(first) if first is not None and first.type == "identifier" else None
            self._export(None, "default", "default", local, None, node)
            self._scan(node, parent)
            return
        identifiers = [c for c in node.named_children if c.type == "identifier"]
        if identifiers and "=" in _unnamed(node):
            self._export(None, "commonjs", "export=", _text(identifiers[0]), None, node)

    def _commonjs(self, statement: tree_sitter.Node) -> None:
        expression = statement.named_children[0] if statement.named_children else None
        if expression is None or expression.type != "assignment_expression":
            return
        left = expression.child_by_field_name("left")
        if left is None or left.type != "member_expression":
            return
        target = _text(left)
        if target == "module.exports":
            self._export(None, "commonjs", "module.exports", None, None, statement)
        elif target.startswith("exports.") or target.startswith("module.exports."):
            name = _text(left.child_by_field_name("property"))
            self._export(None, "commonjs", name, None, None, statement)

    def _reference(self, owner_id: UUID | None, kind: str, node: tree_sitter.Node) -> None:
        text = _text(node).strip()
        if not text:
            return
        count = self.reference_counts.get(owner_id, 0)
        if count >= REFERENCES_PER_SYMBOL or len(self.references) >= REFERENCES_PER_ARTIFACT:
            self.truncated_references += 1
            return
        self.reference_counts[owner_id] = count + 1
        value, _ = _bounded(" ".join(text.split()), TARGET_LIMIT)
        self.references.append(
            SymbolReference(len(self.references), owner_id, kind, value, self.lines.node(node))
        )

    # -- assembly --------------------------------------------------------------------------
    def symbols(self) -> tuple[list[Symbol], list[SymbolFacts]]:
        symbols: list[Symbol] = []
        facts: list[SymbolFacts] = []
        qualified: dict[UUID, str] = {}
        by_container: dict[tuple[UUID | None, str], list[_Pending]] = {}
        for item in self.pending:
            by_container.setdefault(
                (item.parent.symbol_id if item.parent else None, item.name), []
            ).append(item)
        for ordinal, item in enumerate(self.pending):
            parent_id = item.parent.symbol_id if item.parent else None
            name = item.name
            qualified[item.symbol_id] = f"{qualified[parent_id]}.{name}" if parent_id else name
            region = item.region
            end_node = item.merged[-1] if item.form == "overload_group" and item.merged else region
            start_byte, end_byte = region.start_byte, end_node.end_byte
            spans = tuple(
                (self.offsets.char(extra.start_byte), self.offsets.char(extra.end_byte))
                for extra in item.merged
                if item.form != "overload_group"
            )
            span = self.lines.span(start_byte, end_byte)
            node = item.node
            unnamed = _unnamed(node)
            parameters = node.child_by_field_name("parameters")
            type_parameters = node.child_by_field_name("type_parameters")
            return_type = node.child_by_field_name("return_type")
            symbols.append(
                Symbol(
                    item.symbol_id,
                    UUID(int=0),
                    parent_id,
                    ordinal,
                    item.kind,
                    name,
                    qualified[item.symbol_id],
                    "async" in unnamed,
                    span,
                    span.start_line,
                    None,
                    self._decorator_observations(item, self.lines),
                    tuple(
                        expression
                        for child in (type_parameters.named_children if type_parameters else [])
                        if (expression := _fragment(child, self.lines)) is not None
                    ),
                    state_fingerprint=_state(region)
                    if not item.merged or item.form == "overload_group"
                    else digest([_state(region), *[_state(extra) for extra in item.merged]]),
                    declaration_form=item.form,
                    declaration_space=_SPACE[item.kind],
                    name_source=item.name_source,
                    exported=item.exported,
                    start_offset=self.offsets.char(start_byte),
                    end_offset=self.offsets.char(end_byte),
                    additional_spans=spans,
                )
            )
            same_name = by_container[(parent_id, name)]
            symbol_facts: dict[str, object] = {
                "generator": "*" in unnamed or node.type.startswith("generator"),
                "abstract": node.type.startswith("abstract") or "abstract" in unnamed,
                "static": "static" in unnamed,
                "accessor": "get" if "get" in unnamed else "set" if "set" in unnamed else None,
                "ambient": self._ambient(item),
                "parameters": [
                    self._parameter(child)
                    for child in (parameters.named_children if parameters else [])
                    if child.type != "comment"
                ],
                "return_type": _bounded(_text(return_type).lstrip(": "), FRAGMENT_LIMIT)[0]
                if return_type is not None
                else None,
                "returns_jsx": self._returns_jsx(item),
                "wrapper": item.wrapper,
                "signature_count": item.signatures or None,
                "merge_count": len(item.merged) + 1
                if item.merged and item.form != "overload_group"
                else None,
                "merges_with": sorted(
                    {
                        other.kind
                        for other in same_name
                        if other is not item and other.kind != item.kind
                    }
                ),
            }
            facts.append(SymbolFacts(item.symbol_id, symbol_facts))
        return symbols, facts

    @staticmethod
    def _ambient(item: _Pending) -> bool:
        current: _Pending | None = item
        while current is not None:
            if current.form == "ambient":
                return True
            current = current.parent
        return False

    @staticmethod
    def _decorator_observations(item: _Pending, lines: _Lines) -> tuple[DecoratorObservation, ...]:
        holder = item.node
        if item.form == "field_function" and item.node.parent is not None:
            holder = item.node.parent
        values: list[DecoratorObservation] = []
        for child in holder.named_children:
            if child.type == "decorator":
                expression = _fragment(child, lines)
                if expression is not None:
                    values.append(
                        DecoratorObservation(len(values), "decorator", expression, expression.span)
                    )
        return tuple(values)

    @staticmethod
    def _parameter(node: tree_sitter.Node) -> dict[str, object]:
        pattern = node.child_by_field_name("pattern") or node
        annotation = node.child_by_field_name("type")
        return {
            "name": _bounded(_text(pattern), 128)[0],
            "optional": node.type == "optional_parameter",
            "type": _bounded(_text(annotation).lstrip(": "), FRAGMENT_LIMIT)[0]
            if annotation is not None
            else None,
        }

    @staticmethod
    def _returns_jsx(item: _Pending) -> bool:
        jsx = {"jsx_element", "jsx_self_closing_element", "jsx_fragment"}
        body = item.body
        if body is None:
            return False
        if body.type in jsx or (
            body.type == "parenthesized_expression"
            and any(c.type in jsx for c in body.named_children)
        ):
            return True
        stack = list(body.named_children)
        while stack:
            node = stack.pop()
            if node.type in _FUNCTION_VALUES or node.type in {
                "function_declaration",
                "class_declaration",
                "method_definition",
            }:
                continue
            if node.type == "return_statement":
                for value in node.named_children:
                    inner = value
                    while inner.type == "parenthesized_expression" and inner.named_children:
                        inner = inner.named_children[0]
                    if inner.type in jsx:
                        return True
            stack.extend(node.named_children)
        return False


def failure(
    locator: str, data: bytes, code: str, message: str | None = None
) -> TypeScriptExtraction:
    """A failed extraction; source text is never echoed into diagnostics."""
    _, extractor = grammar_for(locator)
    contract = extractor_contract(extractor)
    diagnostic = ParseDiagnostic(code, message or "Source could not be extracted.", "error")
    return TypeScriptExtraction(
        "failed",
        None,
        None,
        (),
        (),
        ArtifactExtraction(
            uuid4(),
            extractor,
            contract or {},
            digest(contract) if contract else "0" * 64,
            "failed",
            hashlib.sha256(data).hexdigest(),
            (diagnostic,),
        ),
        (diagnostic,),
    )


def extract(locator: str, data: bytes) -> TypeScriptExtraction:
    grammar, extractor = grammar_for(locator)
    contract = extractor_contract(extractor)
    input_fingerprint = hashlib.sha256(data).hexdigest()

    def failed(code: str, message: str) -> TypeScriptExtraction:
        return failure(locator, data, code, message)

    if contract is None:
        return failed("LANGUAGE_UNAVAILABLE", "The pinned grammar is not installed.")
    try:
        source, encoding = decode_source(data)
        screen_content(source)
    except ContentRejectedError as error:
        return failed(error.code, "Source could not be decoded or was rejected.")
    except UnicodeDecodeError:
        return failed("ENCODING_ERROR", "Source could not be decoded or was rejected.")
    try:
        language = _language(grammar)
    except LanguageUnavailableError as error:
        return failed(str(error), "The pinned grammar is not usable.")
    encoded = source.encode("utf-8")
    tree = tree_sitter.Parser(language).parse(encoded)
    extractor_state = _Extractor(source, encoded, tree)
    extractor_state.run()
    symbols, facts = extractor_state.symbols()
    diagnostics = tuple(
        ParseDiagnostic(
            "SYNTAX_RECOVERED",
            "A top-level statement containing a syntax error was not extracted.",
            "warning",
            SourceSpan(start, end),
        )
        for start, end in extractor_state.dropped
    )
    extraction = ArtifactExtraction(
        uuid4(),
        extractor,
        contract,
        digest(contract),
        "partial" if extractor_state.dropped else "extracted",
        input_fingerprint,
        diagnostics,
        artifact_facts={
            "grammar": grammar,
            "comment_ranges": _comment_ranges(tree, extractor_state.offsets),
            "dropped_line_ranges": [list(item) for item in extractor_state.dropped],
            "truncated_references": extractor_state.truncated_references,
            "declaration_file": locator.lower().endswith(".d.ts"),
        },
        symbol_facts=tuple(facts),
        references=tuple(extractor_state.references),
        exports=tuple(extractor_state.exports),
    )
    return TypeScriptExtraction(
        "parsed",
        encoding,
        len(source.splitlines()),
        tuple(symbols),
        tuple(extractor_state.imports),
        extraction,
        diagnostics,
    )


def _comment_ranges(tree: tree_sitter.Tree, offsets: _Offsets) -> list[list[int]]:
    ranges: list[list[int]] = []
    stack = [tree.root_node]
    while stack and len(ranges) < 10_000:
        node = stack.pop()
        if node.type == "comment":
            ranges.append([offsets.char(node.start_byte), offsets.char(node.end_byte)])
            continue
        stack.extend(reversed(node.children))
    return ranges


CANARY = {
    "canary.ts": b"""import { a } from "./a";
export function f(x: string): void;
export function f(x: any) { return x; }
export default class { m(): void {} }
interface I { a: string }
interface I { b: number }
namespace N { export const g = () => 1; }
type U<T> = T[];
enum E { A }
""",
    "canary.tsx": b"""import React from "react";
export const Button = memo(({ label }: { label: string }) => <button>{label}</button>);
export function useThing() { const [v] = useState(0); return v; }
""",
    "canary.js": b"""const x = require("x");
class C extends Base { f = () => 1; m() { throw new Error("e"); } }
module.exports = { C };
""",
}


def canary_digest() -> str:
    """Declarations extracted from the built-in canary by the installed grammars."""
    values = []
    for locator, data in sorted(CANARY.items()):
        result = extract(locator, data)
        values.append(
            [
                locator,
                result.extraction.outcome,
                [
                    [
                        s.kind,
                        s.qualified_name,
                        s.declaration_form,
                        s.declaration_space,
                        s.start_offset,
                        s.end_offset,
                        s.exported,
                    ]
                    for s in result.symbols
                ],
                [[i.form, i.from_module, i.name, i.alias] for i in result.imports],
                [[e.kind, e.name, e.local_name] for e in result.extraction.exports],
                [[r.kind, r.target] for r in result.extraction.references],
            ]
        )
    return digest(values)


def verify_canary() -> str | None:
    """``None`` when the installed grammars reproduce the recorded canary, else an error code."""
    try:
        value = canary_digest()
    except LanguageUnavailableError as error:
        return str(error)
    except Exception:
        return "LANGUAGE_UNAVAILABLE"
    return None if value == TREE_SITTER_CANARY_DIGEST else "GRAMMAR_CANARY_MISMATCH"
