"""Standard-library Python AST extraction with restricted expression storage."""

from __future__ import annotations

import ast
import hashlib
import sys
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from buildlens.domain.source import PARSER_VERSION as PARSER_VERSION
from buildlens.domain.source import ContentRejectedError, decode_source, screen_content
from buildlens.domain.structural import (
    ArtifactAnalysis,
    DecoratorObservation,
    ImportReference,
    ObservedExpression,
    Parameter,
    ParseDiagnostic,
    Signature,
    SourceSpan,
    Symbol,
)

FRAGMENT_LIMIT = 2048


def _now() -> datetime:
    return datetime.now(UTC)


def _span(node: ast.AST) -> SourceSpan:
    return SourceSpan(
        getattr(node, "lineno", None),
        getattr(node, "end_lineno", getattr(node, "lineno", None)),
        getattr(node, "col_offset", None),
        getattr(node, "end_col_offset", None),
    )


def _has_lambda(node: ast.AST) -> bool:
    return any(isinstance(value, ast.Lambda) for value in ast.walk(node))


def _fragment(node: ast.AST | None) -> ObservedExpression | None:
    if node is None:
        return None
    if _has_lambda(node):
        return ObservedExpression(None, _span(node), omitted=True)
    # ``unparse`` reconstructs only the AST expression, inherently excluding comments
    # and surrounding declaration text while retaining string literals safely.
    try:
        text = ast.unparse(node)
    except (TypeError, ValueError):
        return ObservedExpression(None, _span(node), omitted=True)
    data = text.encode("utf-8")
    if len(data) <= FRAGMENT_LIMIT:
        return ObservedExpression(text, _span(node))
    clipped = data[:FRAGMENT_LIMIT]
    while clipped:
        try:
            safe = clipped.decode("utf-8")
            return ObservedExpression(safe, _span(node), truncated=True)
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ObservedExpression(None, _span(node), omitted=True)


class PythonAstParser:
    def parse(
        self, *, analysis_id: UUID, version_id: UUID, artifact_id: UUID, locator: str, data: bytes
    ) -> ArtifactAnalysis:
        started = _now()
        try:
            source, encoding = decode_source(data, python=True)
            screen_content(source)
        except ContentRejectedError as error:
            return self._failure(
                analysis_id, version_id, artifact_id, locator, started, error.code, error
            )
        except (SyntaxError, UnicodeDecodeError, LookupError) as error:
            return self._failure(
                analysis_id, version_id, artifact_id, locator, started, "ENCODING_ERROR", error
            )
        try:
            tree = ast.parse(source, filename="<buildlens-artifact>", type_comments=True)
        except (SyntaxError, ValueError) as error:
            return self._failure(
                analysis_id, version_id, artifact_id, locator, started, "SYNTAX_ERROR", error
            )
        symbols: list[Symbol] = []
        imports: list[ImportReference] = []
        owner_stack: list[Symbol] = []
        statement_ordinal = 0

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._function(node, False)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._function(node, True)

            def _function(
                self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool
            ) -> None:
                parent = owner_stack[-1] if owner_stack else None
                kind: Literal["function", "method"] = (
                    "method" if parent and parent.kind == "class" else "function"
                )
                symbol = self._symbol(node, kind, is_async)
                symbols.append(symbol)
                owner_stack.append(symbol)
                self.generic_visit(node)
                owner_stack.pop()

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                symbol = self._symbol(node, "class", False)
                symbols.append(symbol)
                owner_stack.append(symbol)
                self.generic_visit(node)
                owner_stack.pop()

            def _symbol(
                self,
                node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
                kind: Literal["function", "class", "method"],
                is_async: bool,
            ) -> Symbol:
                name = node.name
                parent = owner_stack[-1] if owner_stack else None
                qualified = f"{parent.qualified_name}.{name}" if parent else name
                decorators = tuple(
                    DecoratorObservation(
                        index,
                        type(value).__name__.lower(),
                        _fragment(value) or ObservedExpression(None, _span(value), True),
                        _span(value),
                    )
                    for index, value in enumerate(getattr(node, "decorator_list", []))
                )
                signature = (
                    self._signature(node.args, node.returns)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else None
                )
                type_params = tuple(
                    _fragment(item) or ObservedExpression(None, _span(item), True)
                    for item in getattr(node, "type_params", [])
                )
                return Symbol(
                    uuid4(),
                    UUID(int=0),
                    parent.id if parent else None,
                    len(symbols),
                    kind,
                    name,
                    qualified,
                    is_async,
                    _span(node),
                    min(
                        (
                            item.span.start_line
                            for item in decorators
                            if item.span and item.span.start_line
                        ),
                        default=getattr(node, "lineno", None),
                    ),
                    signature,
                    decorators,
                    type_params,
                    state_fingerprint=hashlib.sha256(
                        (
                            "python-declaration-ast.v1\0"
                            + PARSER_VERSION
                            + "\0"
                            + sys.version.split()[0]
                            + "\0"
                            + ast.dump(node, annotate_fields=True, include_attributes=False)
                        ).encode("utf-8")
                    ).hexdigest(),
                )

            def _signature(self, args: ast.arguments, returns: ast.expr | None) -> Signature:
                positional = [*args.posonlyargs, *args.args]
                defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
                params: list[Parameter] = []
                for index, arg in enumerate(args.posonlyargs):
                    params.append(self._parameter(arg, "positional_only", defaults[index]))
                for index, arg in enumerate(args.args, start=len(args.posonlyargs)):
                    params.append(self._parameter(arg, "positional_or_keyword", defaults[index]))
                if args.vararg:
                    params.append(self._parameter(args.vararg, "variadic_positional", None))
                for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
                    params.append(self._parameter(arg, "keyword_only", default))
                if args.kwarg:
                    params.append(self._parameter(args.kwarg, "variadic_keyword", None))
                return Signature(tuple(params), _fragment(returns))

            def _parameter(
                self,
                arg: ast.arg,
                kind: Literal[
                    "positional_only",
                    "positional_or_keyword",
                    "variadic_positional",
                    "keyword_only",
                    "variadic_keyword",
                ],
                default: ast.expr | None,
            ) -> Parameter:
                return Parameter(
                    arg.arg,
                    kind,
                    default is not None,
                    _fragment(default),
                    _fragment(arg.annotation),
                    _span(arg),
                )

            def visit_Import(self, node: ast.Import) -> None:
                self._imports(node, "import", None, 0)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                self._imports(node, "from", node.module, node.level)

            def _imports(
                self,
                node: ast.Import | ast.ImportFrom,
                form: Literal["import", "from"],
                module: str | None,
                level: int,
            ) -> None:
                nonlocal statement_ordinal
                for item_ordinal, alias in enumerate(node.names):
                    imports.append(
                        ImportReference(
                            uuid4(),
                            UUID(int=0),
                            owner_stack[-1].id if owner_stack else None,
                            statement_ordinal,
                            item_ordinal,
                            form,
                            module,
                            alias.name,
                            alias.asname,
                            level,
                            _span(alias),
                        )
                    )
                statement_ordinal += 1

        visitor = Visitor()
        visitor.visit(tree)
        # Parser-generated IDs are replaced by the application after the aggregate ID is known.
        completed = _now()
        return ArtifactAnalysis(
            uuid4(),
            analysis_id,
            version_id,
            artifact_id,
            locator,
            "parsed",
            started,
            completed,
            encoding,
            len(source.splitlines()),
            locator.rsplit("/", 1)[-1] == "__init__.py",
            (),
            tuple(symbols),
            tuple(imports),
        )

    @staticmethod
    def _failure(
        analysis_id: UUID,
        version_id: UUID,
        artifact_id: UUID,
        locator: str,
        started: datetime,
        code: str,
        error: Exception,
    ) -> ArtifactAnalysis:
        line = getattr(error, "lineno", None)
        return ArtifactAnalysis(
            uuid4(),
            analysis_id,
            version_id,
            artifact_id,
            locator,
            "failed",
            started,
            _now(),
            None,
            None,
            None,
            (
                ParseDiagnostic(
                    code,
                    "Python source could not be parsed.",
                    "error",
                    SourceSpan(line, line) if line else None,
                ),
            ),
        )

    @property
    def runtime_version(self) -> str:
        return sys.version.split()[0]
