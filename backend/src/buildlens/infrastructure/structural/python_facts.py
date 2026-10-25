"""Additive ``python-facts.v1`` pass: syntax-only facts beside the frozen declaration parser.

It re-parses the same bytes and never alters ``PythonAstParser`` output (invariant M3). Facts are
attached to the parser's symbols by exact declaration position. Comment, string and docstring
ranges use decoded Unicode half-open offsets (the chunk convention), unlike the ``ast`` UTF-8
byte columns stored on declarations.
"""

from __future__ import annotations

import ast
import hashlib
import io
import tokenize
from uuid import UUID, uuid4

from buildlens.domain.extractors import PYTHON_FACTS, extractor_contract
from buildlens.domain.source import (
    ContentRejectedError,
    decode_source,
    digest,
    line_offsets,
    screen_content,
)
from buildlens.domain.structural import (
    ArtifactExtraction,
    ExportReference,
    ParseDiagnostic,
    SourceSpan,
    Symbol,
    SymbolFacts,
    SymbolReference,
)

TARGET_LIMIT = 512
VALUE_LIMIT = 256
REFERENCES_PER_SYMBOL = 256
REFERENCES_PER_ARTIFACT = 4096
RANGE_LIMIT = 10_000
_DYNAMIC = {"eval", "exec", "getattr", "setattr", "delattr", "__import__", "compile"}
_DYNAMIC_DOTTED = {"importlib.import_module"}


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _bounded(value: str, limit: int) -> str:
    data = value.encode("utf-8")
    return value if len(data) <= limit else data[:limit].decode("utf-8", errors="ignore")


def _unparse(node: ast.AST | None, limit: int = VALUE_LIMIT) -> str | None:
    if node is None:
        return None
    if any(isinstance(item, ast.Lambda) for item in ast.walk(node)):
        return None
    try:
        return _bounded(ast.unparse(node), limit)
    except (TypeError, ValueError):
        return None


def _span(node: ast.AST) -> SourceSpan:
    return SourceSpan(
        getattr(node, "lineno", None),
        getattr(node, "end_lineno", getattr(node, "lineno", None)),
        getattr(node, "col_offset", None),
        getattr(node, "end_col_offset", None),
    )


class _Coordinates:
    """Convert ``ast`` (line, UTF-8 byte column) positions to decoded offsets."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.starts = line_offsets(source)

    def offset(self, line: int, byte_column: int) -> int:
        start = self.starts[line - 1]
        end = self.starts[line] if line < len(self.starts) else len(self.source)
        text = self.source[start:end]
        prefix = text.encode("utf-8")[:byte_column].decode("utf-8", errors="ignore")
        return start + len(prefix)

    def node(self, node: ast.AST) -> list[int]:
        return [
            self.offset(node.lineno, node.col_offset),  # type: ignore[attr-defined]
            self.offset(node.end_lineno, node.end_col_offset),  # type: ignore[attr-defined]
        ]


_Owner = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


class _Visitor(ast.NodeVisitor):
    def __init__(self, coordinates: _Coordinates, positions: dict[tuple[int, int, str], UUID]):
        self.coordinates = coordinates
        self.positions = positions
        self.owner: list[UUID | None] = [None]
        self.facts: dict[UUID, dict[str, object]] = {}
        self.references: list[SymbolReference] = []
        self.counts: dict[UUID | None, int] = {}
        self.truncated = 0
        self.docstrings: list[list[int]] = []
        self.unmatched = 0

    def _reference(self, kind: str, target: str | None, node: ast.AST) -> None:
        if not target:
            return
        owner = self.owner[-1]
        count = self.counts.get(owner, 0)
        if count >= REFERENCES_PER_SYMBOL or len(self.references) >= REFERENCES_PER_ARTIFACT:
            self.truncated += 1
            return
        self.counts[owner] = count + 1
        self.references.append(
            SymbolReference(
                len(self.references), owner, kind, _bounded(target, TARGET_LIMIT), _span(node)
            )
        )

    def _docstring(self, node: ast.Module | _Owner) -> list[int] | None:
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            value = self.coordinates.node(body[0].value)
            self.docstrings.append(value)
            return value
        return None

    def _own_nodes(self, node: _Owner) -> list[ast.AST]:
        """Descendants that belong to this declaration, excluding nested declarations."""
        values: list[ast.AST] = []
        stack: list[ast.AST] = [*node.body]
        while stack:
            current = stack.pop()
            values.append(current)
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(current, ast.Lambda):
                continue
            stack.extend(ast.iter_child_nodes(current))
        return values

    def _declaration(self, node: _Owner) -> None:
        identity = self.positions.get((node.lineno, node.col_offset, node.name))
        if identity is None:
            self.unmatched += 1
            self.generic_visit(node)
            return
        own = self._own_nodes(node)
        facts: dict[str, object] = {"docstring_range": self._docstring(node)}
        if isinstance(node, ast.ClassDef):
            facts["bases"] = [value for base in node.bases if (value := _unparse(base))]
            facts["keywords"] = [
                f"{item.arg}={value}" if item.arg else f"**{value}"
                for item in node.keywords
                if (value := _unparse(item.value)) is not None
            ]
            assignments: list[dict[str, object]] = []
            for statement in node.body:
                targets: list[ast.expr] = []
                value_node: ast.expr | None = None
                if isinstance(statement, ast.Assign):
                    targets, value_node = list(statement.targets), statement.value
                elif isinstance(statement, ast.AnnAssign):
                    targets, value_node = [statement.target], statement.value
                for target in targets:
                    if isinstance(target, ast.Name):
                        assignments.append(
                            {
                                "name": target.id,
                                "value": _unparse(value_node),
                                "line": target.lineno,
                            }
                        )
            facts["class_assignments"] = assignments[:256]
        else:
            facts["returns_value"] = any(
                isinstance(item, ast.Return) and item.value is not None for item in own
            )
            facts["yields"] = any(isinstance(item, (ast.Yield, ast.YieldFrom)) for item in own)
            facts["awaits"] = any(
                isinstance(item, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for item in own
            )
        dynamic = sorted(
            {
                name
                for item in own
                if isinstance(item, ast.Call)
                and (name := _dotted(item.func)) is not None
                and (name in _DYNAMIC or name in _DYNAMIC_DOTTED)
            }
        )
        facts["dynamic_features"] = dynamic
        self.facts[identity] = facts
        self.owner.append(identity)
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            self._reference("decorator", _dotted(target), decorator)
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                subscript = base.value if isinstance(base, ast.Subscript) else base
                self._reference("base", _dotted(subscript), base)
        for statement in node.body:
            self.visit(statement)
        self.owner.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._declaration(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._declaration(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._declaration(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._reference("call", _dotted(node.func), node.func)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.exc is not None:
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            self._reference("raise", _dotted(target), node.exc)
        self.generic_visit(node)


def _token_ranges(
    source: str, coordinates: _Coordinates
) -> tuple[list[list[int]], list[list[int]]]:
    comments: list[list[int]] = []
    strings: list[list[int]] = []
    starts = coordinates.starts
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in {tokenize.COMMENT, tokenize.STRING}:
            continue
        # tokenize columns are decoded character columns.
        value = [
            starts[token.start[0] - 1] + token.start[1],
            starts[token.end[0] - 1] + token.end[1],
        ]
        target = comments if token.type == tokenize.COMMENT else strings
        if len(target) < RANGE_LIMIT:
            target.append(value)
    return comments, strings


def extract_python_facts(data: bytes, symbols: tuple[Symbol, ...]) -> ArtifactExtraction:
    contract = extractor_contract(PYTHON_FACTS)
    assert contract is not None
    input_fingerprint = hashlib.sha256(data).hexdigest()

    def failed(code: str) -> ArtifactExtraction:
        return ArtifactExtraction(
            uuid4(),
            PYTHON_FACTS,
            contract,
            digest(contract),
            "failed",
            input_fingerprint,
            (ParseDiagnostic(code, "Python facts could not be extracted.", "error"),),
        )

    try:
        source, _ = decode_source(data, python=True)
        screen_content(source)
        tree = ast.parse(source, filename="<buildlens-artifact>", type_comments=True)
    except ContentRejectedError as error:
        return failed(error.code)
    except (SyntaxError, UnicodeDecodeError, LookupError, ValueError):
        return failed("SYNTAX_ERROR")
    coordinates = _Coordinates(source)
    positions = {
        (
            symbol.declaration_span.start_line or 0,
            symbol.declaration_span.start_column or 0,
            symbol.name,
        ): symbol.id
        for symbol in symbols
    }
    visitor = _Visitor(coordinates, positions)
    module_docstring = visitor._docstring(tree)
    for statement in tree.body:
        visitor.visit(statement)
    try:
        comments, strings = _token_ranges(source, coordinates)
    except (tokenize.TokenError, SyntaxError):
        comments, strings = [], []
    exports: list[ExportReference] = []
    main_guard = False
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets) and isinstance(
                statement.value, (ast.List, ast.Tuple)
            ):
                for item in statement.value.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        exports.append(
                            ExportReference(
                                len(exports),
                                None,
                                "dunder_all",
                                item.value,
                                None,
                                None,
                                _span(item),
                            )
                        )
        if (
            isinstance(statement, ast.If)
            and isinstance(statement.test, ast.Compare)
            and _dotted(statement.test.left) == "__name__"
        ):
            main_guard = True
    return ArtifactExtraction(
        uuid4(),
        PYTHON_FACTS,
        contract,
        digest(contract),
        "extracted",
        input_fingerprint,
        (),
        artifact_facts={
            "module_docstring_range": module_docstring,
            "docstring_ranges": visitor.docstrings[:RANGE_LIMIT],
            "comment_ranges": comments,
            "string_ranges": strings,
            "main_guard": main_guard,
            "truncated_references": visitor.truncated,
            "unmatched_declarations": visitor.unmatched,
        },
        symbol_facts=tuple(
            SymbolFacts(symbol.id, visitor.facts[symbol.id])
            for symbol in symbols
            if symbol.id in visitor.facts
        ),
        references=tuple(visitor.references),
        exports=tuple(exports),
    )
