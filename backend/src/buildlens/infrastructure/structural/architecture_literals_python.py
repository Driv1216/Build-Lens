"""Additive ``python-architecture-literals.v1`` pass: sanitised syntactic literal rows.

It re-parses the same bytes with ``ast`` and never alters the frozen declaration parser or the
``python-facts.v1`` pass. Rows are allow-listed shapes only (Stage 14 plan F7, C-2):

* ``app_or_router``: ``X = FastAPI(...) | APIRouter(prefix=<lit>) | Flask(...) | Blueprint(...)``;
* ``router_mount``: ``<obj>.include_router(...)``, ``<obj>.register_blueprint(...)``,
  ``<obj>.mount(<lit>, <obj>)``;
* ``migration_table``: ``op.create_table(<lit>, ...)``;
* ``import_guard``: the syntactic guard of a module- or class-scope import nested in a compound
  statement (``type_checking`` only for the body of ``if TYPE_CHECKING`` /
  ``if typing.TYPE_CHECKING``; everything else is ``conditional``);
* ``opaque_dynamic_import``: ``importlib.import_module`` / ``__import__`` with a non-literal
  argument (position only, never the argument text).

No control flow, data flow or constant evaluation is performed.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from uuid import uuid4

from buildlens.domain.architecture_literals import (
    CLASSIFICATION_KINDS,
    CLASSIFICATION_ROWS_PER_ARTIFACT,
    ROWS_PER_ARTIFACT,
    Counters,
    LiteralKind,
    LiteralRejected,
    identifier_chain,
    path_value,
)
from buildlens.domain.extractors import PYTHON_LITERALS, extractor_contract
from buildlens.domain.source import ContentRejectedError, decode_source, digest, screen_content
from buildlens.domain.structural import (
    ArchitectureLiteral,
    ArtifactExtraction,
    ParseDiagnostic,
    SourceSpan,
)

_CONSTRUCTORS = {"FastAPI", "APIRouter", "Flask", "Blueprint"}
_PREFIX_KEYWORD = {"APIRouter": "prefix", "Blueprint": "url_prefix"}
_TYPE_CHECKING = {"TYPE_CHECKING", "typing.TYPE_CHECKING", "typing_extensions.TYPE_CHECKING"}
_DYNAMIC = {"importlib.import_module", "import_module", "__import__"}


def _dotted(node: ast.AST | None) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _span(node: ast.AST) -> SourceSpan:
    return SourceSpan(
        getattr(node, "lineno", None),
        getattr(node, "end_lineno", getattr(node, "lineno", None)),
        getattr(node, "col_offset", None),
        getattr(node, "end_col_offset", None),
    )


@dataclass(frozen=True, slots=True)
class _Guard:
    construct: str
    branch: str
    type_checking: bool


class _Walker:
    def __init__(self) -> None:
        self.rows: list[ArchitectureLiteral] = []
        self.counters = Counters()
        self.evidence_rows = 0
        self.classification_rows = 0
        self.classification_complete = True

    # -- rows ------------------------------------------------------------------------------
    def add(
        self, kind: LiteralKind, node: ast.AST, subject: str | None, attributes: dict[str, object]
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
            ArchitectureLiteral(len(self.rows), kind, _span(node), subject, attributes)
        )

    def literal(self, node: ast.AST | None) -> dict[str, object]:
        if node is None:
            return {"state": "absent"}
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                value, truncated = path_value(node.value)
            except LiteralRejected as error:
                self.counters.reject(str(error))
                return {"state": "sanitized_out"}
            if truncated:
                self.counters.truncated += 1
                return {"state": "truncated"}
            return {"state": "literal", "value": value}
        return {"state": "opaque"}

    # -- statements ------------------------------------------------------------------------
    def body(
        self, statements: list[ast.stmt], scope: str, owner: str | None, guards: tuple[_Guard, ...]
    ) -> None:
        for statement in statements:
            self.statement(statement, scope, owner, guards)

    def statement(
        self, node: ast.stmt, scope: str, owner: str | None, guards: tuple[_Guard, ...]
    ) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if scope != "function" and guards:
                checking = any(item.type_checking for item in guards)
                innermost = guards[-1]
                self.add(
                    "import_guard",
                    node,
                    None,
                    {
                        "guard": "type_checking" if checking else "conditional",
                        "construct": innermost.construct,
                        "branch": innermost.branch,
                    },
                )
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.expressions([*node.decorator_list, *node.args.defaults], scope, owner)
            name = node.name if owner is None else f"{owner}.{node.name}"
            self.body(node.body, "function", name, ())
            return
        if isinstance(node, ast.ClassDef):
            self.expressions([*node.decorator_list, *node.bases], scope, owner)
            name = node.name if owner is None else f"{owner}.{node.name}"
            self.body(node.body, "function" if scope == "function" else "class", name, guards)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            self.assignment(node, scope, owner)
        if isinstance(node, ast.If):
            self.expressions([node.test], scope, owner)
            checking = _dotted(node.test) in _TYPE_CHECKING
            self.body(node.body, scope, owner, (*guards, _Guard("if", "body", checking)))
            self.body(node.orelse, scope, owner, (*guards, _Guard("if", "else", False)))
            return
        if isinstance(node, (ast.Try, ast.TryStar)):
            self.body(node.body, scope, owner, (*guards, _Guard("try", "body", False)))
            for handler in node.handlers:
                self.body(handler.body, scope, owner, (*guards, _Guard("try", "handler", False)))
            self.body(node.orelse, scope, owner, (*guards, _Guard("try", "else", False)))
            self.body(node.finalbody, scope, owner, (*guards, _Guard("try", "finally", False)))
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            self.expressions([item.context_expr for item in node.items], scope, owner)
            self.body(node.body, scope, owner, (*guards, _Guard("with", "body", False)))
            return
        if isinstance(node, ast.Match):
            self.expressions([node.subject], scope, owner)
            for case in node.cases:
                self.body(case.body, scope, owner, (*guards, _Guard("match", "case", False)))
            return
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            header = [node.test] if isinstance(node, ast.While) else [node.iter]
            self.expressions(header, scope, owner)
            self.body(node.body, scope, owner, (*guards, _Guard("loop", "body", False)))
            self.body(node.orelse, scope, owner, (*guards, _Guard("loop", "else", False)))
            return
        self.expressions(
            [child for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)],
            scope,
            owner,
        )

    def assignment(self, node: ast.Assign | ast.AnnAssign, scope: str, owner: str | None) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            return
        if not isinstance(value, ast.Call):
            return
        callee = _dotted(value.func)
        constructor = callee.rsplit(".", 1)[-1] if callee else None
        if constructor not in _CONSTRUCTORS:
            return
        keyword = _PREFIX_KEYWORD.get(constructor or "")
        prefix = next((k.value for k in value.keywords if keyword and k.arg == keyword), None)
        spread = any(k.arg is None for k in value.keywords)
        self.add(
            "app_or_router",
            node,
            targets[0].id,
            {
                "constructor": constructor,
                "callee": identifier_chain(callee),
                "prefix": {"state": "opaque"}
                if spread and prefix is None
                else self.literal(prefix),
                "scope": scope,
                "owner": owner,
            },
        )

    # -- expressions -----------------------------------------------------------------------
    def expressions(self, roots: list[ast.expr], scope: str, owner: str | None) -> None:
        stack: list[ast.AST] = list(reversed(roots))
        while stack:
            node = stack.pop()
            if isinstance(node, ast.Call):
                self.call(node, scope, owner)
            stack.extend(reversed(list(ast.iter_child_nodes(node))))

    def call(self, node: ast.Call, scope: str, owner: str | None) -> None:
        callee = _dotted(node.func)
        if callee is None:
            return
        method = callee.rsplit(".", 1)[-1]
        if "." in callee and method in {"include_router", "register_blueprint"}:
            keyword = "prefix" if method == "include_router" else "url_prefix"
            router = node.args[0] if node.args else None
            prefix = next((k.value for k in node.keywords if k.arg == keyword), None)
            spread = any(k.arg is None for k in node.keywords)
            self.add(
                "router_mount",
                node,
                identifier_chain(callee.rsplit(".", 1)[0]),
                {
                    "method": method,
                    "router": identifier_chain(_dotted(router)),
                    "prefix": {"state": "opaque"}
                    if spread and prefix is None
                    else self.literal(prefix),
                    "scope": scope,
                    "owner": owner,
                },
            )
        elif "." in callee and method == "mount":
            path = (
                node.args[0]
                if node.args
                else next((k.value for k in node.keywords if k.arg == "path"), None)
            )
            application = (
                node.args[1]
                if len(node.args) > 1
                else next((k.value for k in node.keywords if k.arg == "app"), None)
            )
            self.add(
                "router_mount",
                node,
                identifier_chain(callee.rsplit(".", 1)[0]),
                {
                    "method": "mount",
                    "router": identifier_chain(_dotted(application)),
                    "prefix": self.literal(path) if path is not None else {"state": "opaque"},
                    "scope": scope,
                    "owner": owner,
                },
            )
        elif callee == "op.create_table" or callee.endswith(".op.create_table"):
            name = self.literal(node.args[0] if node.args else None)
            if name.get("state") == "literal":
                self.add(
                    "migration_table",
                    node,
                    str(name["value"]),
                    {"operation": "create_table", "scope": scope, "owner": owner},
                )
        elif callee in _DYNAMIC:
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                self.add(
                    "opaque_dynamic_import",
                    node,
                    None,
                    {"form": callee, "scope": scope, "owner": owner},
                )


def extract_python_literals(data: bytes) -> ArtifactExtraction:
    contract = extractor_contract(PYTHON_LITERALS)
    assert contract is not None
    input_fingerprint = hashlib.sha256(data).hexdigest()

    def failed(code: str) -> ArtifactExtraction:
        return ArtifactExtraction(
            uuid4(),
            PYTHON_LITERALS,
            contract,
            digest(contract),
            "failed",
            input_fingerprint,
            (ParseDiagnostic(code, "Architecture literals could not be extracted.", "error"),),
        )

    try:
        source, _ = decode_source(data, python=True)
        screen_content(source)
        tree = ast.parse(source, filename="<buildlens-artifact>")
    except ContentRejectedError as error:
        return failed(error.code)
    except (SyntaxError, UnicodeDecodeError, LookupError, ValueError, RecursionError):
        return failed("SYNTAX_ERROR")
    walker = _Walker()
    try:
        walker.body(tree.body, "module", None, ())
    except RecursionError:
        return failed("NESTING_LIMIT")
    counters = walker.counters
    diagnostics: tuple[ParseDiagnostic, ...] = ()
    if counters.dropped_rows:
        diagnostics = (
            ParseDiagnostic(
                "LITERAL_ROW_LIMIT",
                "Literal rows beyond the per-artifact cap were dropped.",
                "warning",
            ),
        )
    return ArtifactExtraction(
        uuid4(),
        PYTHON_LITERALS,
        contract,
        digest(contract),
        "partial" if counters.dropped_rows else "extracted",
        input_fingerprint,
        diagnostics,
        artifact_facts={
            "rows": len(walker.rows),
            "dropped_rows": counters.dropped_rows,
            "sanitized_out": counters.sanitized_out,
            "sanitized_codes": dict(sorted(counters.codes.items())),
            "truncated_values": counters.truncated,
            "classification_complete": walker.classification_complete,
        },
        literals=tuple(walker.rows),
    )
