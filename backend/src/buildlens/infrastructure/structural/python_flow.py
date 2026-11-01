"""Stage 15 ``python-flow.v2``: per-scope CFGs, callsites, bindings and summaries.

It re-parses the bytes with ``ast`` (never imports or executes them) and attaches scopes to the
frozen parser's symbols by exact declaration position, as ``python-facts.v1`` does. Nothing
here resolves a callee: callsites carry analysis-safe *shapes* that Milestone B resolves.

What a CFG node is: a statement, or a part of one that is only conditionally evaluated
(short-circuit operands, conditional-expression arms, comprehension clauses), plus ``await``
and ``yield`` suspension points. Every callsite is attached to the node that evaluates it;
callsites within one node are in evaluation order (their ordinals).
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from buildlens.domain import flow_cfg as C
from buildlens.domain.flow_evidence import (
    BINDINGS_PER_SCOPE,
    CALLSITES_PER_ARTIFACT,
    CALLSITES_PER_SCOPE,
    CFG_NODES_PER_SCOPE,
    PYTHON_FLOW,
    SCOPES_PER_ARTIFACT,
    SHAPE_DEPTH,
    SHAPE_ITEMS,
    SUMMARY_SHAPES,
    DisplayText,
    FlowCallsite,
    FlowExtraction,
    FlowScope,
    display,
    flow_pass_contract,
    literal,
)
from buildlens.domain.source import ContentRejectedError, decode_source, digest, screen_content
from buildlens.domain.structural import ParseDiagnostic, SourceSpan

# (start_line, start_column, name) -> symbol id: the frozen parser's declaration position.
SymbolPositions = dict[tuple[int, int, str], UUID]

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, *_COMPREHENSIONS)
_DYNAMIC = {"exec", "eval", "locals", "vars", "globals", "__import__"}
_CATCH_ALL = {"BaseException", "builtins.BaseException"}


class _Truncated(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _span(node: ast.AST) -> list[int]:
    return [
        getattr(node, "lineno", 0),
        getattr(node, "col_offset", 0),
        getattr(node, "end_lineno", getattr(node, "lineno", 0)),
        getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
    ]


def _source_span(node: ast.AST) -> SourceSpan:
    s = _span(node)
    return SourceSpan(s[0], s[2], s[1], s[3])


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (TypeError, ValueError, RecursionError):
        return f"<{type(node).__name__}>"


@dataclass(slots=True)
class _ScopePlan:
    key: str
    parent: str | None
    kind: str
    node: ast.AST
    name: str | None
    owner: UUID | None


class _Planner(ast.NodeVisitor):
    """Discovers every nested scope (in source order) and gives it a stable key."""

    def __init__(self, positions: SymbolPositions) -> None:
        self.positions = positions
        self.plans: list[_ScopePlan] = []
        self.keys: dict[int, str] = {}
        self.unmatched = 0

    def plan(self, tree: ast.Module) -> None:
        self.plans.append(_ScopePlan("m", None, "module", tree, None, None))
        self._children(tree, "m", in_class=False)

    def _children(self, node: ast.AST, parent: str, in_class: bool) -> None:
        count = 0
        stack: list[ast.AST] = list(reversed(list(ast.iter_child_nodes(node))))
        while stack:
            current = stack.pop()
            if isinstance(current, _NESTED):
                key = f"{parent}.{count}"
                count += 1
                kind, name, owner = self._describe(current, in_class)
                self.keys[id(current)] = key
                self.plans.append(_ScopePlan(key, parent, kind, current, name, owner))
                # Definition-time parts of a nested scope belong to the parent:
                # decorators, defaults, bases, keywords and a comprehension's first iterable.
                for part in self._definition_time(current):
                    stack.append(part)
                if len(self.plans) > SCOPES_PER_ARTIFACT:
                    raise _Truncated("FLOW_SCOPE_LIMIT")
                self._children_of_scope(current, key)
                continue
            stack.extend(reversed(list(ast.iter_child_nodes(current))))

    def _children_of_scope(self, node: ast.AST, key: str) -> None:
        if isinstance(node, _FUNCTIONS):
            body = ast.Module(body=node.body, type_ignores=[])
            self._children(body, key, in_class=False)
        elif isinstance(node, ast.ClassDef):
            body = ast.Module(body=node.body, type_ignores=[])
            self._children(body, key, in_class=True)
        elif isinstance(node, ast.Lambda):
            self._children(ast.Expr(value=node.body), key, in_class=False)
        else:  # comprehension
            assert isinstance(node, _COMPREHENSIONS)
            parts: list[ast.AST] = []
            for index, generator in enumerate(node.generators):
                if index:
                    parts.append(generator.iter)
                parts.append(generator.target)
                parts.extend(generator.ifs)
            if isinstance(node, ast.DictComp):
                parts.extend([node.key, node.value])
            else:
                parts.append(node.elt)
            wrapped: list[ast.stmt] = [ast.Expr(value=p) for p in parts if isinstance(p, ast.expr)]
            self._children(ast.Module(body=wrapped, type_ignores=[]), key, in_class=False)

    @staticmethod
    def _definition_time(node: ast.AST) -> list[ast.AST]:
        values: list[ast.AST] = []
        if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            values.extend(node.decorator_list)
        if isinstance(node, _FUNCTIONS) or isinstance(node, ast.Lambda):
            args = node.args
            values.extend([d for d in args.defaults])
            values.extend([d for d in args.kw_defaults if d is not None])
        if isinstance(node, ast.ClassDef):
            values.extend(node.bases)
            values.extend(k.value for k in node.keywords)
        if isinstance(node, _COMPREHENSIONS):
            values.append(node.generators[0].iter)
        return values

    def _describe(self, node: ast.AST, in_class: bool) -> tuple[str, str | None, UUID | None]:
        if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            owner = self.positions.get((node.lineno, node.col_offset, node.name))
            if owner is None:
                self.unmatched += 1
            if isinstance(node, ast.ClassDef):
                return "class_body", node.name, owner
            return ("method" if in_class else "function"), node.name, owner
        if isinstance(node, ast.Lambda):
            return "lambda", None, None
        if isinstance(node, ast.GeneratorExp):
            return "generator_expression", None, None
        return "comprehension", None, None


@dataclass(slots=True)
class _ArtifactBudget:
    callsites: int = 0


@dataclass(slots=True)
class _Scope:
    plan: _ScopePlan
    keys: dict[int, str]
    budget: _ArtifactBudget
    builder: C.CfgBuilder = field(default_factory=lambda: C.CfgBuilder(CFG_NODES_PER_SCOPE))
    callsites: list[FlowCallsite] = field(default_factory=list)
    call_ordinal: dict[int, int] = field(default_factory=dict)
    bindings: list[dict[str, Any]] = field(default_factory=list)
    texts: list[DisplayText] = field(default_factory=list)
    returns: list[dict[str, Any]] = field(default_factory=list)
    raises: list[dict[str, Any]] = field(default_factory=list)
    yields: int = 0
    awaits: int = 0
    nonlocal_names: set[str] = field(default_factory=set)
    global_names: set[str] = field(default_factory=set)
    star_imports: list[dict[str, Any]] = field(default_factory=list)
    dynamic: set[str] = field(default_factory=set)
    called_names: set[str] = field(default_factory=set)
    executes: list[list[Any]] = field(default_factory=list)
    copies: dict[int, list[int]] = field(default_factory=dict)

    # -- shapes ---------------------------------------------------------------------------
    def shape(self, node: ast.AST | None, depth: int = 0) -> dict[str, Any]:
        if node is None:
            return {"k": "none"}
        if depth >= SHAPE_DEPTH:
            return {"k": "deep"}
        d = depth + 1
        if isinstance(node, ast.Name):
            return {"k": "name", "id": node.id}
        if isinstance(node, ast.Attribute):
            path: list[str] = []
            root: ast.AST = node
            while isinstance(root, ast.Attribute):
                path.append(root.attr)
                root = root.value
            return {"k": "attr", "root": self.shape(root, d), "path": list(reversed(path))}
        if isinstance(node, ast.Constant):
            return literal(node.value)
        if isinstance(node, ast.Call):
            value: dict[str, Any] = {
                "k": "call",
                "callee": self.shape(node.func, d),
                "args": [self.shape(a, d) for a in node.args[:SHAPE_ITEMS]],
                "kwargs": [
                    {"kw": k.arg, "shape": self.shape(k.value, d)}
                    for k in node.keywords[:SHAPE_ITEMS]
                ],
            }
            ordinal = self.call_ordinal.get(id(node))
            if ordinal is not None:
                value["callsite"] = ordinal
            return value
        if isinstance(node, ast.Await):
            return {"k": "await", "value": self.shape(node.value, d)}
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return {"k": "yield", "from": isinstance(node, ast.YieldFrom)}
        if isinstance(node, _NESTED):
            key = self.keys.get(id(node))
            return {"k": "scope", "key": key}
        if isinstance(node, ast.Subscript):
            index = node.slice
            items = index.elts if isinstance(index, ast.Tuple) else [index]
            return {
                "k": "subscript",
                "value": self.shape(node.value, d),
                "index": [self.shape(i, d) for i in items[:SHAPE_ITEMS]],
            }
        if isinstance(node, ast.Starred):
            return {"k": "starred", "value": self.shape(node.value, d)}
        if isinstance(node, ast.NamedExpr):
            return self.shape(node.value, d)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return {
                "k": "collection",
                "t": type(node).__name__.lower(),
                "items": [self.shape(e, d) for e in node.elts[:SHAPE_ITEMS]],
                "size": len(node.elts),
            }
        if isinstance(node, ast.Dict):
            return {"k": "collection", "t": "dict", "size": len(node.keys)}
        if isinstance(node, ast.JoinedStr):
            return {"k": "fstring"}
        return {"k": "other", "node": type(node).__name__}

    def text(self, key: str, node: ast.AST) -> str:
        item = display(f"{self.plan.key}#{key}", _unparse(node))
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

    def bind_target(self, target: ast.AST, node: int, kind: str, value: dict[str, Any]) -> None:
        if isinstance(target, ast.Name):
            self.bind(target.id, node, kind, value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for index, item in enumerate(target.elts):
                self.bind_target(item, node, kind, {"k": "unpack", "from": value, "index": index})
        elif isinstance(target, ast.Starred):
            self.bind_target(target.value, node, kind, {"k": "unpack_rest", "from": value})
        elif isinstance(target, ast.Attribute):
            root = target.value
            if isinstance(root, ast.Name):
                self.bind(f"{root.id}.{target.attr}", node, "attribute_store", value)

    # -- atoms --------------------------------------------------------------------------
    @staticmethod
    def atom(test: ast.AST) -> dict[str, Any]:
        if isinstance(test, ast.Name):
            return {"op": "truthy", "var": test.id}
        if isinstance(test, ast.Constant):
            return {"op": "const", "lit": literal(test.value)}
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            left, right, op = test.left, test.comparators[0], test.ops[0]
            if isinstance(op, (ast.Is, ast.IsNot)):
                negated = isinstance(op, ast.IsNot)
                for var, other in ((left, right), (right, left)):
                    if isinstance(var, ast.Name) and isinstance(other, ast.Constant):
                        if other.value is None:
                            return {"op": "is_none", "var": var.id, "neg": negated}
                        if isinstance(other.value, bool):
                            return {
                                "op": "is_lit",
                                "var": var.id,
                                "lit": literal(other.value),
                                "neg": negated,
                            }
            if isinstance(op, (ast.Eq, ast.NotEq)):
                negated = isinstance(op, ast.NotEq)
                for var, other in ((left, right), (right, left)):
                    if isinstance(var, ast.Name) and isinstance(other, ast.Constant):
                        return {
                            "op": "eq",
                            "var": var.id,
                            "lit": literal(other.value),
                            "neg": negated,
                        }
            return {"op": "opaque", "reason": "COMPARISON"}
        if (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Name)
            and test.func.id == "isinstance"
            and len(test.args) == 2
            and not test.keywords
            and isinstance(test.args[0], ast.Name)
        ):
            target = test.args[1]
            classes = target.elts if isinstance(target, ast.Tuple) else [target]
            names = [_dotted(c) for c in classes]
            if all(names):
                return {"op": "isinstance", "var": test.args[0].id, "classes": names}
        if isinstance(test, ast.Attribute):
            return {"op": "opaque", "reason": "ATTRIBUTE"}
        if isinstance(test, ast.Call):
            return {"op": "opaque", "reason": "CALL"}
        return {"op": "opaque", "reason": type(test).__name__.upper()}


class _Lowerer:
    """Lowers one scope's body into its CFG."""

    def __init__(self, scope: _Scope) -> None:
        self.s = scope
        self.b = scope.builder

    # -- expressions --------------------------------------------------------------------
    def expr(self, node: ast.AST | None, cur: int | None) -> int | None:
        """Evaluate ``node`` starting at ``cur``; return the node evaluation continues at."""
        if node is None or cur is None:
            return cur
        if isinstance(node, _NESTED):
            key = self.s.keys.get(id(node))
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
                # Comprehensions run immediately; their first iterable is evaluated here.
                cur = self.expr(node.generators[0].iter, cur)
                assert cur is not None
                self.s.executes.append([cur, key])
                self.b.may_raise(cur)
            elif isinstance(node, ast.GeneratorExp):
                cur = self.expr(node.generators[0].iter, cur)
            elif isinstance(node, ast.Lambda):
                for default in [*node.args.defaults, *[d for d in node.args.kw_defaults if d]]:
                    cur = self.expr(default, cur)
            return cur
        if isinstance(node, ast.BoolOp):
            cur = self.expr(node.values[0], cur)
            join = self.b.node("join")
            for value in node.values[1:]:
                assert cur is not None
                branch = self.b.follow(
                    cur,
                    "branch",
                    _span(node),
                    atom=self.s.atom(node.values[0]),
                    short_circuit=type(node.op).__name__.lower(),
                )
                rest = self.b.node("operand", _span(value))
                go_on = C.TRUE if isinstance(node.op, ast.And) else C.FALSE
                stop = C.FALSE if go_on == C.TRUE else C.TRUE
                self.b.edge(branch, rest, go_on)
                self.b.edge(branch, join, stop)
                cur = self.expr(value, rest)
            if cur is not None:
                self.b.edge(cur, join)
            return join
        if isinstance(node, ast.IfExp):
            cur = self.expr(node.test, cur)
            assert cur is not None
            branch = self.b.follow(
                cur,
                "branch",
                _span(node.test),
                atom=self.s.atom(node.test),
                text=self.s.text(f"b{len(self.b.nodes)}", node.test),
            )
            join = self.b.node("join")
            for arm, kind in ((node.body, C.TRUE), (node.orelse, C.FALSE)):
                start = self.b.node("operand", _span(arm))
                self.b.edge(branch, start, kind)
                end = self.expr(arm, start)
                if end is not None:
                    self.b.edge(end, join)
            return join
        if isinstance(node, ast.Await):
            cur = self.expr(node.value, cur)
            assert cur is not None
            self.s.awaits += 1
            wait = self.b.follow(cur, "await", _span(node))
            self.b.may_raise(wait)
            resume = self.b.node("resume")
            self.b.edge(wait, resume, C.RESUME_NORMAL)
            return resume
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            cur = self.expr(node.value, cur)
            assert cur is not None
            self.s.yields += 1
            point = self.b.follow(
                cur, "yield", _span(node), delegate=isinstance(node, ast.YieldFrom) or None
            )
            resume = self.b.node("resume")
            self.b.edge(point, resume, C.RESUME_NORMAL)
            thrown = self.b.node("thrown")
            self.b.edge(point, thrown, C.RESUME_THROW)
            self.b.jump(thrown, C.RAISE)
            return resume
        if isinstance(node, ast.Call):
            cur = self.expr(node.func, cur)
            for argument in node.args:
                cur = self.expr(argument, cur)
            for keyword in node.keywords:
                cur = self.expr(keyword.value, cur)
            assert cur is not None
            self.callsite(node, cur)
            return cur
        if isinstance(node, ast.NamedExpr):
            cur = self.expr(node.value, cur)
            assert cur is not None
            self.s.bind_target(node.target, cur, "walrus", self.s.shape(node.value))
            return cur
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr_context):
                continue
            cur = self.expr(child, cur)
        return cur

    def callsite(self, node: ast.Call, cur: int) -> None:
        s = self.s
        existing = s.call_ordinal.get(id(node))
        if existing is not None:  # the same call in another finally copy
            s.copies.setdefault(existing, []).append(cur)
            self.b.may_raise(cur)
            return
        if len(s.callsites) >= CALLSITES_PER_SCOPE:
            raise _Truncated("FLOW_CALLSITE_LIMIT")
        if s.budget.callsites >= CALLSITES_PER_ARTIFACT:
            raise _Truncated("FLOW_ARTIFACT_CALLSITE_LIMIT")
        s.budget.callsites += 1
        ordinal = len(s.callsites)
        s.call_ordinal[id(node)] = ordinal
        name = _dotted(node.func)
        if name in _DYNAMIC:
            s.dynamic.add(name)
        if isinstance(node.func, ast.Name):
            s.called_names.add(node.func.id)
        arguments: list[dict[str, Any]] = []
        for index, argument in enumerate(node.args[: SHAPE_ITEMS * 4]):
            if isinstance(argument, ast.Starred):
                arguments.append({"star": True, "shape": s.shape(argument.value)})
            else:
                arguments.append({"pos": index, "shape": s.shape(argument)})
        for keyword in node.keywords[: SHAPE_ITEMS * 4]:
            arguments.append({"kw": keyword.arg, "shape": s.shape(keyword.value)})
        s.callsites.append(
            FlowCallsite(
                ordinal,
                cur,
                s.shape(node.func),
                tuple(arguments),
                isinstance(getattr(node, "_flow_parent", None), ast.Await),
                _source_span(node),
                text_key=s.text(f"c{ordinal}", node),
            )
        )
        self.b.may_raise(cur)

    # -- conditions ---------------------------------------------------------------------
    def cond(self, test: ast.AST, cur: int) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        """Lower a test; return pending (node, edge kind) pairs for the true and false targets."""
        if isinstance(test, ast.BoolOp):
            true_out: list[tuple[int, str]] = []
            false_out: list[tuple[int, str]] = []
            start: int = cur
            for index, value in enumerate(test.values):
                t, f = self.cond(value, start)
                last = index == len(test.values) - 1
                if isinstance(test.op, ast.And):
                    false_out += f
                    if last:
                        true_out += t
                    else:
                        start = self.b.node("operand", _span(test.values[index + 1]))
                        self.attach(t, start)
                else:
                    true_out += t
                    if last:
                        false_out += f
                    else:
                        start = self.b.node("operand", _span(test.values[index + 1]))
                        self.attach(f, start)
            return true_out, false_out
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            t, f = self.cond(test.operand, cur)
            return f, t
        evaluated = self.expr(test, cur)
        assert evaluated is not None
        if isinstance(test, ast.Constant):
            # A constant test has one outcome; the other edge does not exist.
            truthy = bool(test.value)
            branch = self.b.follow(evaluated, "branch", _span(test), atom=self.s.atom(test))
            return ([(branch, C.TRUE)], []) if truthy else ([], [(branch, C.FALSE)])
        branch = self.b.follow(
            evaluated,
            "branch",
            _span(test),
            atom=self.s.atom(test),
            text=self.s.text(f"b{len(self.b.nodes)}", test),
        )
        return [(branch, C.TRUE)], [(branch, C.FALSE)]

    def attach(
        self, pending: Iterable[tuple[int, str]], target: int, kind: str | None = None
    ) -> None:
        for source, edge in pending:
            self.b.edge(source, target, kind or edge, None if kind is None else edge)

    # -- statements -----------------------------------------------------------------------
    def block(self, statements: list[ast.stmt], cur: int | None) -> int | None:
        for statement in statements:
            if cur is None:
                # Unreachable code still gets nodes and callsites (marked unreachable later).
                cur = self.b.node("unreachable", _span(statement))
            cur = self.statement(statement, cur)
        return cur

    def statement(self, s: ast.stmt, cur: int) -> int | None:
        b, scope = self.b, self.s
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node = b.follow(cur, "def", _span(s))
            end = self.expr_list([*s.decorator_list], node)
            if isinstance(s, ast.ClassDef):
                end = self.expr_list([*s.bases, *(k.value for k in s.keywords)], end)
            else:
                end = self.expr_list([*s.args.defaults, *(d for d in s.args.kw_defaults if d)], end)
            assert end is not None
            key = scope.keys.get(id(s))
            if isinstance(s, ast.ClassDef):
                scope.executes.append([end, key])
                b.may_raise(end)
            if s.decorator_list:
                b.may_raise(end)
            scope.bind(
                s.name,
                end,
                "class" if isinstance(s, ast.ClassDef) else "def",
                {"k": "scope", "key": key},
            )
            return end
        if isinstance(s, ast.Return):
            node = b.follow(cur, "return", _span(s))
            end = self.expr(s.value, node)
            assert end is not None
            if s.value is not None and len(scope.returns) < SUMMARY_SHAPES:
                scope.returns.append(scope.shape(s.value))
            b.jump(end, C.RETURN)
            return None
        if isinstance(s, ast.Raise):
            node = b.follow(cur, "raise", _span(s))
            end = self.expr(s.exc, node)
            end = self.expr(s.cause, end)
            assert end is not None
            if len(scope.raises) < SUMMARY_SHAPES:
                scope.raises.append(scope.shape(s.exc) if s.exc is not None else {"k": "reraise"})
            b.jump(end, C.RAISE)
            return None
        if isinstance(s, ast.Assert):
            node = b.follow(cur, "assert", _span(s), condition="ASSERTIONS_ENABLED_ASSUMED")
            t, f = self.cond(s.test, node)
            after = b.node("join")
            self.attach(t, after)
            if f:
                failed = b.node("assert_failed", _span(s))
                self.attach(f, failed)
                end = self.expr(s.msg, failed)
                assert end is not None
                b.jump(end, C.RAISE)
            return after
        if isinstance(s, ast.If):
            node = b.follow(cur, "if", _span(s))
            t, f = self.cond(s.test, node)
            ends: list[int | None] = []
            if t:
                start = b.node("block", _span(s.body[0]))
                self.attach(t, start)
                ends.append(self.block(s.body, start))
            if f:
                if s.orelse:
                    start = b.node("block", _span(s.orelse[0]))
                    self.attach(f, start)
                    ends.append(self.block(s.orelse, start))
                else:
                    join = b.node("join")
                    self.attach(f, join)
                    ends.append(join)
            return self.join(ends)
        if isinstance(s, ast.While):
            head = b.follow(cur, "loop_head", _span(s), loop="while")
            after = b.node("after_loop")
            t, f = self.cond(s.test, head)
            frame = b.loop(None, after, head)
            if t:
                start = b.node("block", _span(s.body[0]))
                self.attach(t, start)
                end = self.block(s.body, start)
                if end is not None:
                    b.edge(end, head, C.LOOP_BACK)
            b.pop(frame)
            if f:
                exhausted = b.node("loop_exhausted")
                self.attach(f, exhausted, C.LOOP_EXIT)
                end = self.block(s.orelse, exhausted)
                if end is not None:
                    b.edge(end, after)
            return after if self.reached(after) else None
        if isinstance(s, (ast.For, ast.AsyncFor)):
            node = b.follow(cur, "for", _span(s))
            end = self.expr(s.iter, node)
            assert end is not None
            head = b.follow(
                end,
                "loop_head",
                _span(s.target),
                loop="async_for" if isinstance(s, ast.AsyncFor) else "for",
            )
            b.may_raise(head)  # iteration runs __next__/__anext__ (or a generator body)
            after = b.node("after_loop")
            frame = b.loop(None, after, head)
            start = b.node("block", _span(s.body[0]))
            b.edge(head, start, C.TRUE, "next")
            scope.bind_target(
                s.target,
                start,
                "for_target",
                {
                    "k": "iter_item",
                    "from": scope.shape(s.iter),
                    "async": isinstance(s, ast.AsyncFor),
                },
            )
            body_end = self.block(s.body, start)
            if body_end is not None:
                b.edge(body_end, head, C.LOOP_BACK)
            b.pop(frame)
            exhausted = b.node("loop_exhausted")
            b.edge(head, exhausted, C.LOOP_EXIT)
            end = self.block(s.orelse, exhausted)
            if end is not None:
                b.edge(end, after)
            return after if self.reached(after) else None
        if isinstance(s, (ast.Break, ast.Continue)):
            node = b.follow(cur, "break" if isinstance(s, ast.Break) else "continue", _span(s))
            b.jump(node, C.BREAK if isinstance(s, ast.Break) else C.CONTINUE)
            return None
        if isinstance(s, (ast.Try, ast.TryStar)):
            return self.try_(s, cur)
        if isinstance(s, (ast.With, ast.AsyncWith)):
            return self.with_(s, cur)
        if isinstance(s, ast.Match):
            return self.match(s, cur)
        if isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            node = b.follow(cur, "assign", _span(s))
            end = self.expr(s.value, node) if s.value is not None else node
            assert end is not None
            targets = s.targets if isinstance(s, ast.Assign) else [s.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    end = self.expr(getattr(target, "value", None), end)
                    assert end is not None
                    if isinstance(target, ast.Subscript):
                        end = self.expr(target.slice, end)
            assert end is not None
            kind = {ast.Assign: "assign", ast.AnnAssign: "annassign", ast.AugAssign: "augassign"}[
                type(s)
            ]
            if s.value is not None or isinstance(s, ast.AugAssign):
                value = (
                    scope.shape(s.value)
                    if not isinstance(s, ast.AugAssign)
                    else {"k": "other", "node": "AugAssign"}
                )
                for target in targets:
                    scope.bind_target(target, end, kind, value)
            if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
                scope.bind(
                    s.target.id,
                    end,
                    "annotation",
                    {"k": "annotation", "shape": scope.shape(s.annotation)},
                )
            return end
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            node = b.follow(cur, "import", _span(s))
            b.may_raise(node)
            for alias in s.names:
                bound = alias.asname or alias.name.split(".")[0]
                value = {
                    "k": "import",
                    "module": s.module if isinstance(s, ast.ImportFrom) else alias.name,
                    "name": alias.name if isinstance(s, ast.ImportFrom) else None,
                    "level": s.level if isinstance(s, ast.ImportFrom) else 0,
                }
                if bound != "*":
                    scope.bind(bound, node, "import", value)
                elif len(scope.star_imports) < SUMMARY_SHAPES:
                    scope.star_imports.append({"module": value["module"], "level": value["level"]})
            return node
        if isinstance(s, ast.Global):
            scope.global_names.update(s.names)
            return cur
        if isinstance(s, ast.Nonlocal):
            scope.nonlocal_names.update(s.names)
            return cur
        if isinstance(s, ast.Delete):
            node = b.follow(cur, "delete", _span(s))
            for target in s.targets:
                if isinstance(target, ast.Name):
                    scope.bind(target.id, node, "delete", {"k": "deleted"})
                else:
                    end = self.expr(target, node)
                    assert end is not None
                    node = end
            return node
        if isinstance(s, ast.Pass):
            return cur
        node = b.follow(cur, "stmt", _span(s))
        return self.expr(s, node) if not isinstance(s, ast.Expr) else self.expr(s.value, node)

    def expr_list(self, values: Iterable[ast.AST], cur: int | None) -> int | None:
        for value in values:
            cur = self.expr(value, cur)
        return cur

    def reached(self, node: int) -> bool:
        return any(edge[1] == node for edge in self.b.edges)

    def join(self, ends: list[int | None]) -> int | None:
        live = [end for end in ends if end is not None]
        if not live:
            return None
        if len(live) == 1:
            return live[0]
        join = self.b.node("join")
        for end in live:
            self.b.edge(end, join)
        return join

    def try_(self, s: ast.Try | ast.TryStar, cur: int) -> int | None:
        b, scope = self.b, self.s
        start = b.follow(cur, "try", _span(s), star=isinstance(s, ast.TryStar) or None)
        final_frame = (
            b.finally_(lambda entry: self.block(s.finalbody, entry)) if s.finalbody else None
        )
        dispatch = b.node("dispatch", _span(s)) if s.handlers else None
        handler_frame = b.handlers(dispatch) if dispatch is not None else None
        body_end = self.block(s.body, start)
        if handler_frame is not None:
            b.pop(handler_frame)
        ends: list[int | None] = []
        if s.orelse and body_end is not None:
            ends.append(self.block(s.orelse, b.follow(body_end, "else", _span(s.orelse[0]))))
        else:
            ends.append(body_end)
        if dispatch is not None:
            catch_all = False
            for index, handler in enumerate(s.handlers):
                types = (
                    handler.type.elts
                    if isinstance(handler.type, ast.Tuple)
                    else ([handler.type] if handler.type else [])
                )
                names = [_dotted(t) for t in types]
                node = b.node(
                    "handler",
                    _span(handler),
                    types=[n if n else None for n in names] or None,
                    text=scope.text(f"h{index}", handler.type)
                    if handler.type is not None
                    else None,
                )
                b.edge(dispatch, node, C.EXCEPTION, f"h{index}")
                if handler.type is None or any(n in _CATCH_ALL for n in names if n):
                    catch_all = True
                if handler.name:
                    scope.bind(handler.name, node, "except_as", {"k": "exception", "types": names})
                ends.append(self.block(handler.body, node))
            if not catch_all:
                unmatched = b.node("unmatched", _span(s))
                b.edge(dispatch, unmatched, C.EXCEPTION, "unmatched")
                b.jump(unmatched, C.RAISE)
        if final_frame is not None:
            exits = [b.normal_completion_through_finally(end, final_frame) for end in ends]
            b.pop(final_frame)
            return self.join(list(dict.fromkeys(exits)))
        return self.join(ends)

    def with_(self, s: ast.With | ast.AsyncWith, cur: int) -> int | None:
        b, scope = self.b, self.s
        node = b.follow(cur, "with", _span(s), asynchronous=isinstance(s, ast.AsyncWith) or None)
        for item in s.items:
            end = self.expr(item.context_expr, node)
            assert end is not None
            enter = b.follow(
                end,
                "with_enter",
                _span(item.context_expr),
                text=scope.text(f"w{len(b.nodes)}", item.context_expr),
            )
            b.may_raise(enter)
            node = enter
            if item.optional_vars is not None:
                scope.bind_target(
                    item.optional_vars,
                    enter,
                    "with_as",
                    {
                        "k": "enter",
                        "from": scope.shape(item.context_expr),
                        "async": isinstance(s, ast.AsyncWith),
                    },
                )
        # ``__exit__`` runs on every way out of the body: normal completion and return/break/
        # continue go through a with_exit copy (lowered like a finally body); exceptions go to
        # the exceptional exit, which may suppress (edge to after) or propagate. Milestone B
        # keeps or drops the suppression edge by manager kind.
        span = _span(s)

        def leave(entry: int) -> int:
            node = b.follow(entry, "with_exit", span)
            b.may_raise(node)
            return node

        exit_frame = b.finally_(leave)
        exit_exceptional = b.node("with_exit_exceptional", span)
        handler_frame = b.handlers(exit_exceptional)
        body_end = self.block(s.body, node)
        b.pop(handler_frame)
        normal_end = b.normal_completion_through_finally(body_end, exit_frame)
        b.pop(exit_frame)
        after = b.node("after_with")
        b.edge(exit_exceptional, after, C.EXCEPTION, "suppressed")
        b.jump(exit_exceptional, C.RAISE)
        if normal_end is not None:
            b.edge(normal_end, after)
        return after

    def match(self, s: ast.Match, cur: int) -> int | None:
        b, scope = self.b, self.s
        node = b.follow(cur, "match", _span(s))
        source = self.expr(s.subject, node)
        assert source is not None
        after = b.node("after_match")
        # Ways to reach the next case test: a pattern that did not match (``no_match``) or a
        # guard that evaluated false (normal edge from the guard's false branch).
        pending: list[tuple[int, str, str | None]] = [(source, C.NORMAL, None)]
        for index, case in enumerate(s.cases):
            test = b.node(
                "case_test", _span(case.pattern), text=scope.text(f"p{index}", case.pattern)
            )
            for source_node, kind, label in pending:
                b.edge(source_node, test, kind, label)
            for name in _pattern_names(case.pattern):
                scope.bind(name, test, "match_capture", {"k": "pattern", "case": index})
            matched = b.node("case_body", _span(case.body[0]))
            pending = [] if _irrefutable(case.pattern) else [(test, C.NO_MATCH, f"c{index}")]
            if case.guard is not None:
                guard = b.node("case_guard", _span(case.guard))
                b.edge(test, guard, C.CASE, f"c{index}")
                t, f = self.cond(case.guard, guard)
                self.attach(t, matched)
                if f:
                    failed = b.node("case_failed")
                    self.attach(f, failed)
                    pending.append((failed, C.NORMAL, None))
            else:
                b.edge(test, matched, C.CASE, f"c{index}")
            end = self.block(case.body, matched)
            if end is not None:
                b.edge(end, after)
        for source_node, kind, label in pending:
            b.edge(source_node, after, kind, label)
        return after if self.reached(after) else None


def _pattern_names(pattern: ast.pattern) -> list[str]:
    names: list[str] = []
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchAs) and node.name:
            names.append(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            names.append(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.append(node.rest)
    return names


def _irrefutable(pattern: ast.pattern) -> bool:
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or _irrefutable(pattern.pattern)
    if isinstance(pattern, ast.MatchOr):
        return any(_irrefutable(p) for p in pattern.patterns)
    return False


def _annotate_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._flow_parent = parent  # type: ignore[attr-defined]


def _parameters(scope: _Scope, node: ast.AST) -> list[dict[str, Any]]:
    if not isinstance(node, (*_FUNCTIONS, ast.Lambda)):
        return []
    args = node.args
    values: list[dict[str, Any]] = []
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for index, (arg, default) in enumerate(zip(positional, defaults, strict=True)):
        values.append(
            {
                "name": arg.arg,
                "index": index,
                "kind": "positional_only"
                if index < len(args.posonlyargs)
                else "positional_or_keyword",
                "annotation": scope.shape(arg.annotation) if arg.annotation else None,
                "default": scope.shape(default) if default is not None else None,
            }
        )
    if args.vararg:
        values.append(
            {
                "name": args.vararg.arg,
                "index": len(values),
                "kind": "variadic_positional",
                "annotation": scope.shape(args.vararg.annotation)
                if args.vararg.annotation
                else None,
                "default": None,
            }
        )
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        values.append(
            {
                "name": arg.arg,
                "index": len(values),
                "kind": "keyword_only",
                "annotation": scope.shape(arg.annotation) if arg.annotation else None,
                "default": scope.shape(default) if default is not None else None,
            }
        )
    if args.kwarg:
        values.append(
            {
                "name": args.kwarg.arg,
                "index": len(values),
                "kind": "variadic_keyword",
                "annotation": scope.shape(args.kwarg.annotation) if args.kwarg.annotation else None,
                "default": None,
            }
        )
    return values


def _callable_kind(plan: _ScopePlan, yields: int) -> str:
    node = plan.node
    if plan.kind == "module":
        return "module_code"
    if plan.kind == "class_body":
        return "class_code"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async_generator_function" if yields else "coroutine_function"
    if isinstance(node, ast.GeneratorExp):
        return "generator_function"
    if isinstance(node, ast.FunctionDef) and yields:
        return "generator_function"
    return "sync_function"


def _lower(
    plan: _ScopePlan, keys: dict[int, str], budget: _ArtifactBudget
) -> tuple[FlowScope, list[DisplayText]]:
    scope = _Scope(plan, keys, budget)
    node = plan.node
    lowerer = _Lowerer(scope)
    name_span = node if not isinstance(node, ast.Module) else None
    span = _source_span(name_span) if name_span is not None else SourceSpan(1, None, 0, None)
    truncated: str | None = None
    try:
        params = _parameters(scope, node)
        for param in params:
            scope.bind(param["name"], C.ENTRY, "param", {"k": "param", "index": param["index"]})
        if isinstance(node, ast.Module):
            end = lowerer.block(node.body, C.ENTRY)
        elif isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            end = lowerer.block(node.body, C.ENTRY)
        elif isinstance(node, ast.Lambda):
            body = lowerer.b.follow(C.ENTRY, "return", _span(node.body))
            end = lowerer.expr(node.body, body)
            if end is not None:
                if len(scope.returns) < SUMMARY_SHAPES:
                    scope.returns.append(scope.shape(node.body))
                lowerer.b.jump(end, C.RETURN)
            end = None
        else:
            end = _lower_comprehension(lowerer, node)
        if end is not None:
            lowerer.b.edge(end, C.EXIT, C.FALLTHROUGH)
        cfg = lowerer.b.graph()
    except C.CfgLimitError:
        truncated, cfg = "FLOW_CFG_NODE_LIMIT", {}
    except _Truncated as error:
        truncated, cfg = error.code, {}
    except RecursionError:
        truncated, cfg = "FLOW_NESTING_LIMIT", {}
    if truncated:
        scope.callsites.clear()
        scope.texts.clear()
        summary: dict[str, Any] = {"truncated": truncated}
    else:
        graph = C.Graph.of(cfg)
        reach = graph.reachable(C.ENTRY, normal_only=False)
        params = _parameters(scope, node)
        rebound = {b["name"] for b in scope.bindings if b["kind"] != "param"}
        summary = {
            "parameters": params,
            "decorators": [
                scope.shape(d) for d in getattr(node, "decorator_list", [])[:SUMMARY_SHAPES]
            ],
            "bases": [scope.shape(b) for b in getattr(node, "bases", [])[:SUMMARY_SHAPES]],
            "keywords": [
                {"kw": k.arg, "shape": scope.shape(k.value)}
                for k in getattr(node, "keywords", [])[:SUMMARY_SHAPES]
            ],
            "returns": scope.returns,
            "return_annotation": scope.shape(node.returns)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns
            else None,
            "star_imports": scope.star_imports,
            "raises": scope.raises,
            "invokes_parameter": sorted(
                p["index"]
                for p in params
                if p["name"] in scope.called_names and p["name"] not in rebound
            ),
            "normal_exit_reachable": C.summary_flags(graph)["normal_exit_reachable"],
            "yield_points": scope.yields,
            "await_points": scope.awaits,
            "nonlocal": sorted(scope.nonlocal_names),
            "global": sorted(scope.global_names),
            "dynamic": sorted(scope.dynamic),
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
            _callable_kind(plan, scope.yields),
            plan.name,
            span,
            None,
            None,
            truncated,
            cfg,
            summary,
            tuple(
                replace(c, copies=tuple(scope.copies.get(c.ordinal, ()))) for c in scope.callsites
            ),
            (),
            plan.owner,
        ),
        scope.texts,
    )


def _lower_comprehension(lowerer: _Lowerer, node: ast.AST) -> int | None:
    assert isinstance(node, _COMPREHENSIONS)
    b, scope = lowerer.b, lowerer.s
    cur: int | None = C.ENTRY
    heads: list[int] = []
    for index, generator in enumerate(node.generators):
        if index:
            cur = lowerer.expr(generator.iter, cur)
        assert cur is not None
        head = b.follow(cur, "loop_head", _span(generator.target), loop="comprehension")
        b.may_raise(head)
        heads.append(head)
        start = b.node("block", _span(generator.target))
        b.edge(head, start, C.TRUE, "next")
        scope.bind_target(
            generator.target,
            start,
            "for_target",
            {
                "k": "iter_item",
                "from": scope.shape(generator.iter),
                "async": bool(generator.is_async),
            },
        )
        cur = start
        for condition in generator.ifs:
            t, f = lowerer.cond(condition, cur)
            passed = b.node("operand", _span(condition))
            lowerer.attach(t, passed)
            lowerer.attach(f, head, C.LOOP_BACK)
            cur = passed
    parts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
    for part in parts:
        cur = lowerer.expr(part, cur)
    if isinstance(node, ast.GeneratorExp) and cur is not None:
        point = b.follow(cur, "yield", _span(node.elt))
        scope.yields += 1
        resume = b.node("resume")
        b.edge(point, resume, C.RESUME_NORMAL)
        thrown = b.node("thrown")
        b.edge(point, thrown, C.RESUME_THROW)
        b.jump(thrown, C.RAISE)
        cur = resume
    if cur is not None:
        b.edge(cur, heads[-1], C.LOOP_BACK)
    for inner, outer in zip(reversed(heads[1:]), reversed(heads[:-1]), strict=True):
        b.edge(inner, outer, C.LOOP_EXIT)
    done = b.node("loop_exhausted")
    b.edge(heads[0], done, C.LOOP_EXIT)
    return done


def extract_python_flow(data: bytes, positions: SymbolPositions) -> FlowExtraction:
    contract = flow_pass_contract(PYTHON_FLOW)
    assert contract is not None
    fingerprint = hashlib.sha256(data).hexdigest()

    def failed(code: str) -> FlowExtraction:
        return FlowExtraction(
            PYTHON_FLOW,
            contract,
            digest(contract),
            "failed",
            fingerprint,
            (ParseDiagnostic(code, "Python flow evidence could not be extracted.", "error"),),
        )

    try:
        source, _ = decode_source(data, python=True)
        screen_content(source)
        tree = ast.parse(source, filename="<buildlens-artifact>")
    except ContentRejectedError as error:
        return failed(error.code)
    except (SyntaxError, UnicodeDecodeError, LookupError, ValueError, RecursionError):
        return failed("SYNTAX_ERROR")
    _annotate_parents(tree)
    planner = _Planner(positions)
    diagnostics: list[ParseDiagnostic] = []
    try:
        planner.plan(tree)
    except _Truncated as error:
        diagnostics.append(
            ParseDiagnostic(error.code, "Too many scopes; the rest were not planned.", "warning")
        )
    except RecursionError:
        return failed("FLOW_NESTING_LIMIT")
    budget = _ArtifactBudget()
    scopes: list[FlowScope] = []
    texts: list[DisplayText] = []
    for plan in planner.plans:
        scope, scope_texts = _lower(plan, planner.keys, budget)
        scopes.append(scope)
        texts.extend(scope_texts)
    truncated = sorted({s.truncated for s in scopes if s.truncated})
    for code in truncated:
        diagnostics.append(
            ParseDiagnostic(code, "A scope exceeded a flow cap and was truncated.", "warning")
        )
    if planner.unmatched:
        diagnostics.append(
            ParseDiagnostic(
                "FLOW_DECLARATION_UNMATCHED", "A declaration had no matching symbol.", "warning"
            )
        )
    return FlowExtraction(
        PYTHON_FLOW,
        contract,
        digest(contract),
        "partial" if diagnostics else "extracted",
        fingerprint,
        tuple(diagnostics),
        tuple(scopes),
        tuple(texts),
        planner.unmatched,
    )
