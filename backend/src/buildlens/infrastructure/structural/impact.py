"""Screened supplemental syntax facts for the deliberately small Impact producer."""

import ast
import json
import re
import sys
import tomllib
from collections import Counter
from importlib.metadata import version
from itertools import islice
from pathlib import PurePosixPath
from typing import Any

import tree_sitter
import tree_sitter_javascript
import tree_sitter_typescript

from buildlens.domain.source import SCREENING_VERSION, decode_source, digest, screen_content

MAX_FUNCTIONS = 128
MAX_EVENTS = 512
_OPERATORS = {
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Eq: "==",
    ast.NotEq: "!=",
}


def syntax_contract() -> str:
    return str(
        digest(
            {
                "version": "impact-syntax.v8",
                "python": sys.version.split()[0],
                "parsers": [
                    version(name)
                    for name in (
                        "tree-sitter",
                        "tree-sitter-javascript",
                        "tree-sitter-typescript",
                    )
                ],
                "screening": SCREENING_VERSION,
            }
        )
    )


def _name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id[:128]
    if isinstance(node, ast.Attribute):
        parent = _name(node.value)
        if parent:
            return f"{parent}.{node.attr}"[:128]
    return None


def _literal(node: ast.AST | None) -> dict[str, Any] | None:
    if not isinstance(node, ast.Constant):
        return None
    value = node.value
    if value is None or type(value) is bool or (type(value) is int and abs(value) <= 2**31):
        return {"kind": type(value).__name__, "value": value}
    if isinstance(value, str):
        return {"kind": "str", "digest": digest(value)}
    return None


def _field(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value.isidentifier() and len(node.value) <= 64:
            return node.value
    return None


def _parameters(args: ast.arguments) -> list[dict[str, Any]]:
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    values = [
        {
            "name": param.arg,
            "kind": "posonly" if i < len(args.posonlyargs) else "positional",
            "required": default is None,
            "default_digest": _syntax_hash(default),
        }
        for i, (param, default) in enumerate(zip(positional, defaults, strict=True))
    ]
    values.extend(
        {
            "name": param.arg,
            "kind": "keyword",
            "required": default is None,
            "default_digest": _syntax_hash(default),
        }
        for param, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    )
    for kind, param in (("vararg", args.vararg), ("kwarg", args.kwarg)):
        if param:
            values.append({"name": param.arg, "kind": kind, "required": False})
    return values


def _syntax_hash(node: ast.AST | None) -> str | None:
    return digest(ast.dump(node, include_attributes=False)) if node is not None else None


def _integer_guard(test: ast.expr) -> str | None:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    call, right = test.left, test.comparators[0]
    if not isinstance(test.ops[0], ast.IsNot) or _name(right) != "int":
        return None
    if isinstance(call, ast.Call) and _name(call.func) == "type" and len(call.args) == 1:
        if not call.keywords and isinstance(call.args[0], ast.Name):
            return call.args[0].id
    return None


def _threshold(test: ast.expr, integers: set[str]) -> dict[str, Any] | None:
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.left, ast.Name):
        name = test.left.id
        literal = test.comparators[0]
        negative = isinstance(literal, ast.UnaryOp) and isinstance(literal.op, ast.USub)
        limit = _literal(
            literal.operand if negative and isinstance(literal, ast.UnaryOp) else literal
        )
        op = _OPERATORS.get(type(test.ops[0]))
        if name in integers and op and limit and limit["kind"] == "int":
            return {
                "binding": name,
                "operator": op,
                "limit": -limit["value"] if negative else limit["value"],
            }
    return None


class _Body:
    def __init__(self, local_functions: set[str], integer_inputs: set[str]) -> None:
        self.local_functions = local_functions
        self.integer_inputs = integer_inputs
        self.calls: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        self.returns: list[dict[str, Any]] = []
        self.raises: list[dict[str, Any]] = []
        self.conditional_calls: list[dict[str, Any]] = []
        self.assertions: list[dict[str, Any]] = []
        self.gaps: set[str] = set()

    def expression(
        self, expr: ast.expr | None, bindings: dict[str, int], *, guarded: bool = False
    ) -> int | None:
        if isinstance(expr, ast.Await):
            # Await is not treated as a synchronous direct call by this projection.
            self.gaps.add("AWAITED_SOURCE_TRANSFER_UNAVAILABLE")
            return None
        if isinstance(expr, ast.Name):
            return bindings.get(expr.id)
        if isinstance(expr, ast.Subscript):
            origin = self.expression(expr.value, bindings, guarded=guarded)
            key = _field(expr.slice)
            if origin is not None and key:
                self.reads.append(
                    {"call": origin, "field": key, "line": expr.lineno, "guarded": guarded}
                )
            elif key is None:
                self.expression(expr.slice, bindings, guarded=guarded)
                self.gaps.add("DYNAMIC_FIELD_ACCESS")
            return None
        if isinstance(expr, ast.Call):
            escaped = {
                origin
                for item in (*expr.args, *(kw.value for kw in expr.keywords))
                if (origin := self.expression(item, bindings, guarded=guarded)) is not None
            }
            if isinstance(expr.func, ast.Attribute):
                receiver = self.expression(expr.func.value, bindings, guarded=guarded)
                if receiver is not None:
                    escaped.add(receiver)
            if escaped:
                self.gaps.add("CALL_ARGUMENT_ALIAS_ESCAPE")
                for name in tuple(bindings):
                    if bindings[name] in escaped:
                        del bindings[name]
            target = _name(expr.func)
            if not target:
                self.gaps.add("DYNAMIC_CALL")
            recorded = {
                "target": target,
                "line": expr.lineno,
                "column": expr.col_offset,
                "end_line": expr.end_lineno,
                "end_column": expr.end_col_offset,
                "count": len(expr.args),
                "keywords": [kw.arg for kw in expr.keywords],
                "unpacking": any(isinstance(arg, ast.Starred) for arg in expr.args)
                or any(kw.arg is None for kw in expr.keywords),
                "local": target in self.local_functions,
                "guarded": guarded,
                "inputs": [_literal(item) for item in expr.args],
            }
            self.calls.append(recorded)
            return len(self.calls) - 1 if target and not recorded["unpacking"] else None
        if isinstance(expr, ast.BoolOp):
            for index, operand in enumerate(expr.values):
                self.expression(operand, bindings, guarded=guarded)
                literal = _literal(operand)
                if literal is not None and literal["kind"] == "bool":
                    if literal["value"] != isinstance(expr.op, ast.And):
                        break
                else:
                    guarded = True
                    if index < len(expr.values) - 1:
                        self.gaps.add("SHORT_CIRCUIT_CONDITION_COMPARISON_UNAVAILABLE")
            return None
        if isinstance(expr, ast.Compare) and len(expr.ops) == 1:
            compared = {
                origin
                for operand in (expr.left, expr.comparators[0])
                if (origin := self.expression(operand, bindings, guarded=guarded)) is not None
            }
            if compared:
                self.gaps.add("COMPARISON_ALIAS_ESCAPE")
                for name in tuple(bindings):
                    if bindings[name] in compared:
                        del bindings[name]
            return None
        if isinstance(expr, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
            elements = (
                [entry for pair in zip(expr.keys, expr.values, strict=True) for entry in pair]
                if isinstance(expr, ast.Dict)
                else expr.elts
            )
            escaped = {
                origin
                for item in elements
                if (origin := self.expression(item, bindings, guarded=guarded)) is not None
            }
            if escaped:
                self.gaps.add("CONTAINER_ALIAS_ESCAPE")
                for name in tuple(bindings):
                    if bindings[name] in escaped:
                        del bindings[name]
            return None
        if expr is not None and not isinstance(expr, ast.Constant):
            self.gaps.add("UNSUPPORTED_EXPRESSION_ORDER")
            bindings.clear()
        return None

    def block(
        self,
        statements: list[ast.stmt],
        bindings: dict[str, int],
        integers: set[str],
        *,
        guarded: bool = False,
    ) -> tuple[dict[str, int], set[str], bool]:
        for statement in statements:
            if isinstance(statement, ast.Return):
                value = statement.value
                forwarded = self.expression(value, bindings, guarded=guarded)
                fields = None
                if isinstance(value, ast.Dict):
                    names = [_field(key) for key in value.keys]
                    if all(name is not None for name in names) and len(set(names)) == len(names):
                        fields = {
                            name: _literal(item)
                            for name, item in zip(names, value.values, strict=True)
                        }
                self.returns.append(
                    {
                        "line": statement.lineno,
                        "fields": fields,
                        "value": _literal(value),
                        "forward": forwarded,
                    }
                )
                return bindings, integers, False
            if isinstance(statement, ast.Raise):
                exc = statement.exc
                target = _name(exc.func) if isinstance(exc, ast.Call) else _name(exc)
                status = None
                if isinstance(exc, ast.Call) and target and target.endswith("HTTPException"):
                    status = next(
                        (_literal(arg.value) for arg in exc.keywords if arg.arg == "status_code"),
                        None,
                    )
                self.raises.append({"line": statement.lineno, "type": target, "status": status})
                return bindings, integers, False
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                origin = self.expression(statement.value, bindings, guarded=guarded)
                if any(not isinstance(target, ast.Name) for target in targets):
                    self.gaps.add("ALIASED_WRITE")
                    bindings.clear()
                    integers.clear()
                for target_node in targets:
                    if isinstance(target_node, ast.Name):
                        name = target_node.id
                        bindings.pop(name, None)
                        integers.discard(name)
                        if origin is not None:
                            bindings[name] = origin
                continue
            if isinstance(statement, ast.Expr):
                self.expression(statement.value, bindings, guarded=guarded)
                continue
            if isinstance(statement, ast.If):
                self.expression(statement.test, bindings, guarded=guarded)
                integer = _integer_guard(statement.test)
                if (
                    integer is not None
                    and integer in self.integer_inputs
                    and len(statement.body) == 1
                    and isinstance(statement.body[0], ast.Raise)
                    and not statement.orelse
                ):
                    self.block(statement.body, dict(bindings), set(integers), guarded=True)
                    integers.add(integer)
                    continue
                guard = _threshold(statement.test, integers)
                if (
                    not guarded
                    and guard
                    and len(statement.body) == 1
                    and isinstance(statement.body[0], ast.Expr)
                ):
                    effect = statement.body[0].value
                    if isinstance(effect, ast.Call) and _name(effect.func) and not statement.orelse:
                        self.conditional_calls.append(
                            {
                                "line": statement.lineno,
                                "predicate": guard,
                                "target": _name(effect.func),
                                "arguments_digest": digest(
                                    [
                                        _syntax_hash(effect.func),
                                        [_syntax_hash(item) for item in effect.args],
                                        [
                                            (item.arg, _syntax_hash(item.value))
                                            for item in effect.keywords
                                        ],
                                    ]
                                ),
                            }
                        )
                left, left_ints, left_continues = self.block(
                    statement.body, dict(bindings), set(integers), guarded=True
                )
                right, right_ints, right_continues = self.block(
                    statement.orelse, dict(bindings), set(integers), guarded=True
                )
                if not (left_continues or right_continues):
                    return bindings, integers, False
                if not left_continues or not right_continues:
                    bindings, integers = (
                        (left, left_ints) if left_continues else (right, right_ints)
                    )
                    guarded = True
                    continue
                for name in bindings.keys() | left.keys() | right.keys():
                    if name in left and left.get(name) == right.get(name):
                        bindings[name] = left[name]
                    else:
                        bindings.pop(name, None)
                        if name in left or name in right:
                            self.gaps.add("CONDITIONAL_DEFINITION_JOIN")
                integers.intersection_update(left_ints & right_ints)
                continue
            if isinstance(statement, ast.Assert):
                test = statement.test
                previous = len(self.reads)
                if isinstance(test, ast.Compare) and len(test.ops) == 1:
                    self.expression(test.left, bindings, guarded=guarded)
                    self.expression(test.comparators[0], bindings, guarded=guarded)
                    expected = _literal(test.comparators[0])
                    if len(self.reads) == previous + 1 and expected:
                        self.assertions.append(
                            {
                                **self.reads[-1],
                                "assertion_line": statement.lineno,
                                "expected": expected,
                                "operator": type(test.ops[0]).__name__,
                            }
                        )
                else:
                    self.expression(test, bindings, guarded=guarded)
                continue
            if not isinstance(statement, ast.Pass):
                self.gaps.add("UNSUPPORTED_ORDERED_STATEMENT")
                bindings.clear()
                integers.clear()
        return bindings, integers, True


def _python(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    functions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    projection_gaps = set()
    if any(isinstance(item, (ast.AsyncFunctionDef, ast.ClassDef)) for item in tree.body):
        projection_gaps.add("PYTHON_DECLARATION_BODY_UNSUPPORTED")
    if any(
        not isinstance(item, (ast.FunctionDef, ast.Import, ast.ImportFrom))
        and not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant))
        for item in tree.body
    ):
        projection_gaps.add("MODULE_EXECUTION_NOT_ANALYZED")
    counts = Counter(item.name for item in functions)
    # Local target/builtin resolution requires an uncluttered module namespace.
    module_static = all(
        isinstance(item, (ast.FunctionDef, ast.Import, ast.ImportFrom))
        or (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant))
        for item in tree.body
    )
    imports = {
        alias.asname or alias.name.split(".")[0]
        for item in tree.body
        if isinstance(item, (ast.Import, ast.ImportFrom))
        for alias in item.names
    }
    local_names = {name for name, count in counts.items() if count == 1} - imports
    candidates = {
        item.name
        for item in functions
        if module_static
        and "*" not in imports
        and item.name in local_names
        and not item.decorator_list
    }
    result = []
    for item in functions[:MAX_FUNCTIONS]:
        nodes = list(islice(ast.walk(item), MAX_EVENTS + 1))
        if len(nodes) > MAX_EVENTS:
            result.append(
                {
                    "name": item.name,
                    "line": item.lineno,
                    "signature": _parameters(item.args),
                    "syntax": None,
                    "gaps": ["FUNCTION_BODY_SYNTAX_LIMIT"],
                    "eligible_binding": False,
                }
            )
            continue
        writes = {
            node.id
            for node in nodes
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
        }
        parameters = _parameters(item.args)
        local_writes = writes | {node.arg for node in nodes if isinstance(node, ast.arg)}
        nested = any(
            isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Lambda, ast.Global, ast.Nonlocal))
            for node in nodes[1:]
        )
        local_candidates = candidates - local_writes if not nested else set()
        static_builtins = (
            module_static
            and not nested
            and not ({"type", "int", "*"} & (local_writes | set(counts) | imports))
        )
        integer_inputs = (
            {
                param["name"]
                for param in parameters
                if param["kind"] in {"positional", "posonly", "keyword"}
            }
            - writes
            if static_builtins
            else set()
        )
        body = _Body(local_candidates, integer_inputs)
        if nested:
            body.gaps.add("NESTED_SCOPE")
        else:
            _, _, falls_through = body.block(item.body, {}, set())
            if falls_through and body.returns:
                body.gaps.add("IMPLICIT_RETURN_ALTERNATIVE")
                body.returns.append(
                    {"line": item.end_lineno, "fields": None, "value": None, "forward": None}
                )
        result.append(
            {
                "name": item.name,
                "line": item.lineno,
                "signature": parameters,
                "syntax": _syntax_hash(item),
                "body_syntax": _syntax_hash(ast.Module(body=item.body, type_ignores=[])),
                "calls": body.calls,
                "reads": body.reads,
                "returns": body.returns,
                "assertions": body.assertions,
                "raises": body.raises,
                "conditional_calls": body.conditional_calls,
                "gaps": sorted(body.gaps),
                "eligible_binding": item.name in candidates,
            }
        )
    module_properties, http_tests, database_accesses, transactions = _module_properties(tree)
    return {
        "language": "Python",
        "namespace_static": module_static,
        "module_bindings": sorted(set(counts) | imports),
        "functions": result,
        "complete": len(result) == len(functions) and not projection_gaps,
        "gaps": sorted(projection_gaps),
        "module_syntax": _syntax_hash(
            ast.Module(
                body=[node for node in tree.body if not isinstance(node, ast.FunctionDef)],
                type_ignores=[],
            )
        ),
        "module_properties": module_properties,
        "http_tests": http_tests,
        "database_accesses": database_accesses,
        "transactions": transactions,
    }


class _JsBody:
    """Ordered uses in a small JS/TS function subset, without evaluating JavaScript."""

    def __init__(self, data: bytes, targets: set[str]) -> None:
        self.data = data
        self.targets = targets
        self.calls: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        self.returns: list[dict[str, Any]] = []
        self.gaps: set[str] = set()

    def name(self, node: tree_sitter.Node | None) -> str | None:
        if node is not None and node.type in {"identifier", "property_identifier"}:
            if node.end_byte - node.start_byte <= 128:
                return self.data[node.start_byte : node.end_byte].decode()
        return None

    def literal(self, node: tree_sitter.Node | None) -> dict[str, Any] | None:
        if node is None:
            return None
        if node.type in {"true", "false"}:
            return {"kind": "bool", "value": node.type == "true"}
        if node.type == "null":
            return {"kind": "NoneType", "value": None}
        if node.type == "number":
            raw = self.data[node.start_byte : node.end_byte]
            if raw.isdigit() and len(raw) <= 10 and int(raw) < 2**31:
                return {"kind": "int", "value": int(raw)}
        return None

    def expression(
        self, node: tree_sitter.Node | None, env: dict[str, int], *, guarded: bool = False
    ) -> int | None:
        if node is None:
            return None
        if name := self.name(node):
            return env.get(name)
        if node.type == "parenthesized_expression":
            child = node.named_child(0)
            return self.expression(child, env, guarded=guarded)
        if node.type in {"object", "array", "await_expression"}:
            # Container aliases and Promise unwrapping need their own value domain.
            # Do not retain a precise binding across these unsupported operations.
            env.clear()
            self.gaps.add(
                "ASYNC_RESULT_TRANSFER_UNAVAILABLE"
                if node.type == "await_expression"
                else "CONTAINER_ALIAS_ESCAPE"
            )
            return None
        if node.type == "member_expression":
            origin = self.expression(node.child_by_field_name("object"), env, guarded=guarded)
            field = self.name(node.child_by_field_name("property"))
            if origin is not None and field and node.child_by_field_name("optional_chain") is None:
                self.reads.append(
                    {
                        "call": origin,
                        "field": field,
                        "line": node.start_point.row + 1,
                        "guarded": guarded,
                    }
                )
            return None
        if node.type == "call_expression":
            callable_ = node.child_by_field_name("function")
            target = self.name(callable_)
            args = node.child_by_field_name("arguments")
            arguments = args.named_children if args else []
            escapes = {
                origin
                for arg in arguments
                if (origin := self.expression(arg, env, guarded=guarded)) is not None
            }
            if callable_ and callable_.type == "member_expression":
                receiver = self.expression(callable_.child_by_field_name("object"), env)
                if receiver is not None:
                    escapes.add(receiver)
            if escapes:
                self.gaps.add("CALL_ARGUMENT_ALIAS_ESCAPE")
                for name in tuple(env):
                    if env[name] in escapes:
                        del env[name]
            local = target in self.targets and node.child_by_field_name("optional_chain") is None
            self.calls.append(
                {
                    "target": target,
                    "line": node.start_point.row + 1,
                    "column": node.start_point.column,
                    "end_line": node.end_point.row + 1,
                    "end_column": node.end_point.column,
                    "count": len(arguments),
                    "keywords": [],
                    "local": local,
                    "unpacking": any(arg.type == "spread_element" for arg in arguments),
                    "inputs": [self.literal(arg) for arg in arguments],
                }
            )
            return (
                len(self.calls) - 1
                if target and node.child_by_field_name("optional_chain") is None
                else None
            )
        if node.type == "assignment_expression":
            origin = self.expression(node.child_by_field_name("right"), env, guarded=guarded)
            name = self.name(node.child_by_field_name("left"))
            if name:
                env.pop(name, None)
                if origin is not None:
                    env[name] = origin
            else:
                env.clear()
                self.gaps.add("ALIASED_WRITE")
            return None
        if node.type not in {"number", "true", "false", "null", "object", "string"}:
            env.clear()
            self.gaps.add("UNSUPPORTED_JS_ORDERED_EXPRESSION")
        return None

    def statement(self, node: tree_sitter.Node, env: dict[str, int]) -> bool:
        if node.type == "statement_block":
            for item in node.named_children:
                if not self.statement(item, env):
                    return False
            return True
        if node.type in {"lexical_declaration", "variable_declaration"}:
            for item in node.named_children:
                name = self.name(item.child_by_field_name("name"))
                origin = self.expression(item.child_by_field_name("value"), env)
                if name:
                    env.pop(name, None)
                    if origin is not None:
                        env[name] = origin
                else:
                    env.clear()
                    self.gaps.add("UNSUPPORTED_JS_DESTRUCTURING")
            return True
        if node.type == "expression_statement":
            self.expression(node.named_child(0), env)
            return True
        if node.type == "if_statement":
            self.expression(node.child_by_field_name("condition"), env)
            outcomes = []
            for kind in ("consequence", "alternative"):
                child = node.child_by_field_name(kind)
                if child and child.type == "else_clause":
                    child = child.named_child(0)
                branch = dict(env)
                if child is None or self.statement(child, branch):
                    if child is not None and child.type == "statement_block":
                        for statement in child.named_children:
                            if statement.type == "lexical_declaration":
                                for declarator in statement.named_children:
                                    local = self.name(declarator.child_by_field_name("name"))
                                    if local is not None:
                                        branch.pop(local, None)
                    outcomes.append(branch)
            proofs = dict(outcomes[0]) if outcomes else {}
            for other in outcomes[1:]:
                proofs = {
                    name: origin for name, origin in proofs.items() if other.get(name) == origin
                }
            env.clear()
            env.update(proofs)
            return bool(outcomes)
        if node.type == "return_statement":
            value = node.named_child(0)
            origin = self.expression(value, env)
            fields: dict[str, Any] | None = None
            if value and value.type == "object":
                fields = {}
                for leaf in value.named_children:
                    name = self.name(leaf.child_by_field_name("key"))
                    if name is None or name in fields or name == "__proto__":
                        fields = None
                        break
                    fields[name] = self.literal(leaf.child_by_field_name("value"))
            self.returns.append(
                {"line": node.start_point.row + 1, "fields": fields, "forward": origin}
            )
            return False
        if node.type == "throw_statement":
            return False
        if node.type not in {"comment", "empty_statement"}:
            env.clear()
            self.gaps.add("UNSUPPORTED_JS_ORDERED_STATEMENT")
        return True


def _javascript(locator: str, source: str) -> dict[str, Any]:
    suffix = PurePosixPath(locator).suffix
    if suffix == ".ts":
        grammar = tree_sitter_typescript.language_typescript()
    elif suffix == ".tsx":
        grammar = tree_sitter_typescript.language_tsx()
    else:
        grammar = tree_sitter_javascript.language()
    data = source.encode("utf-8")
    root = tree_sitter.Parser(tree_sitter.Language(grammar)).parse(data).root_node
    if root.has_error:
        raise SyntaxError("Partial syntax")
    declarations = []
    for node in root.named_children:
        declaration = (
            node.child_by_field_name("declaration") if node.type == "export_statement" else node
        )
        if declaration is None or declaration.type != "function_declaration":
            continue
        identifier = declaration.child_by_field_name("name")
        if identifier is not None and identifier.end_byte - identifier.start_byte <= 128:
            declarations.append(
                (declaration, data[identifier.start_byte : identifier.end_byte].decode())
            )
    names = Counter(name for _, name in declarations)
    # Other top-level statements can mutate local declarations or install dynamic bindings.
    plain_module = all(
        node.type in {"import_statement", "comment", "function_declaration"}
        or (
            node.type == "export_statement"
            and (child := node.child_by_field_name("declaration")) is not None
            and child.type == "function_declaration"
        )
        for node in root.named_children
    )
    functions = []
    for declaration, name in declarations[:MAX_FUNCTIONS]:
        scope = declaration.child_by_field_name("body")
        targets = set()
        if plain_module and scope:
            body_nodes = [scope]
            seen = 0
            blocked: set[str] = set()
            if parameters := declaration.child_by_field_name("parameters"):
                body_nodes.append(parameters)
            while body_nodes and seen <= MAX_EVENTS:
                item = body_nodes.pop()
                seen += 1
                if item.type in {"identifier", "property_identifier"}:
                    if item.parent and item.parent.type not in {
                        "call_expression",
                        "member_expression",
                        "pair",
                    }:
                        blocked.add(data[item.start_byte : item.end_byte].decode())
                if item.type in {
                    "function_declaration",
                    "arrow_function",
                    "function_expression",
                    "with_statement",
                    "class_declaration",
                }:
                    seen = MAX_EVENTS + 1
                body_nodes.extend(item.named_children)
            if seen <= MAX_EVENTS:
                targets = {target for target, count in names.items() if count == 1} - blocked
            else:
                scope = None
        body = _JsBody(data, targets)
        if scope:
            body.statement(scope, {})
        functions.append(
            {
                "name": name,
                "result_kind": "promise"
                if any(child.type == "async" for child in declaration.children)
                else "value",
                "line": declaration.start_point.row + 1,
                "syntax": digest(data[declaration.start_byte : declaration.end_byte].decode()),
                "calls": body.calls,
                "reads": body.reads,
                "returns": body.returns,
                "eligible_binding": plain_module and names[name] == 1,
                "gaps": sorted(
                    body.gaps
                    | {"JAVASCRIPT_SIGNATURE_COMPARISON_UNAVAILABLE"}
                    | (
                        {"ASYNC_RESULT_TRANSFER_UNAVAILABLE"}
                        if any(child.type == "async" for child in declaration.children)
                        else set()
                    )
                ),
            }
        )
    return {
        "language": "JavaScript/TypeScript",
        "functions": functions[:MAX_FUNCTIONS],
        "complete": False,
    }


def extract_impact(locator: str, raw: bytes) -> dict[str, Any]:
    source, _ = decode_source(raw, python=locator.endswith(".py"))
    screen_content(source)
    if PurePosixPath(locator).name in {"pyproject.toml", "package.json"}:
        return _framework_manifest(locator, source)
    return _python(source) if locator.endswith(".py") else _javascript(locator, source)


_FRAMEWORK_PACKAGES = {
    "fastapi",
    "pydantic",
    "sqlalchemy",
    "starlette",
    "react",
    "react-dom",
    "axios",
    "@tanstack/react-query",
}
_EXACT_VERSION = re.compile(r"[0-9]{1,5}(?:\.[0-9]{1,5}){1,3}")


def _framework_manifest(locator: str, source: str) -> dict[str, Any]:
    name = PurePosixPath(locator).name
    declarations: list[tuple[str, str, str]] = []
    try:
        if name == "pyproject.toml":
            project = tomllib.loads(source).get("project")
            dependencies = project.get("dependencies") if isinstance(project, dict) else None
            if isinstance(dependencies, list):
                for raw in dependencies:
                    if isinstance(raw, str) and (
                        match := re.fullmatch(r"([A-Za-z0-9_.-]+)\s*==\s*([0-9.]+)", raw.strip())
                    ):
                        declarations.append(("dependencies", match[1].lower(), match[2]))
        else:
            manifest = json.loads(source, object_pairs_hook=_unique_json_keys)
            if isinstance(manifest, dict):
                for group in ("dependencies", "devDependencies"):
                    values = manifest.get(group)
                    if isinstance(values, dict):
                        declarations.extend(
                            (group, key, item)
                            for key, item in values.items()
                            if isinstance(item, str)
                        )
    except ValueError:
        raise SyntaxError("Unsupported framework manifest") from None
    identities = Counter((group, name) for group, name, _ in declarations)
    properties = [
        {
            "id": f"framework:{group}:{package}",
            "line": None,
            "property": "framework_version_declaration",
            "entity": package,
            "value": {"declared_version": value, "group": group, "effective_version": "unresolved"},
        }
        for group, package, value in declarations
        if package in _FRAMEWORK_PACKAGES
        and identities[(group, package)] == 1
        and _EXACT_VERSION.fullmatch(value)
    ]
    return {
        "language": "Framework manifest",
        "functions": [],
        "complete": True,
        "module_properties": properties,
    }


def _unique_json_keys(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("Duplicate manifest key")
    return result


def _import_names(tree: ast.Module) -> dict[str, str]:
    """Only unique, direct top-level imports in an unambiguous lexical slot."""
    imports: dict[str, str] = {}
    repeats: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module and statement.level == 0:
            pairs = [
                (alias.asname or alias.name, f"{statement.module}.{alias.name}")
                for alias in statement.names
            ]
        elif isinstance(statement, ast.Import):
            pairs = [
                (alias.asname or alias.name.split(".")[0], alias.name) for alias in statement.names
            ]
        else:
            continue
        for key, target in pairs:
            if key in imports:
                repeats.add(key)
            imports[key] = target
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            repeats.add(node.id)
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            repeats.add(node.name)
        if isinstance(node, ast.arg):
            repeats.add(node.arg)
    return {key: value for key, value in imports.items() if key not in repeats and key != "*"}


def _qualified(node: ast.AST | None, imports: dict[str, str]) -> str | None:
    value = _name(node)
    if not value:
        return None
    root, dot, rest = value.partition(".")
    prefix = imports.get(root)
    return prefix + dot + rest if prefix else None


def _safe_route(node: ast.AST | None) -> str | None:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    value = node.value
    if not value:
        return value
    if (
        len(value) <= 128
        and value.startswith("/")
        and all(char.isascii() and (char.isalnum() or char in "/_-{}:.") for char in value)
    ):
        return value
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


_WIRE_TYPES = {"bool", "int", "float", "str"}


def _query_fields(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    imports: dict[str, str],
    scalar_types: set[str],
    body_parameter: str | None = None,
) -> dict[str, dict[str, Any]] | None:
    args = function.args
    if args.posonlyargs or args.vararg or args.kwarg:
        return None
    defaults = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults)
    fields = {}
    for arg, marker in zip(
        [*args.args, *args.kwonlyargs], [*defaults, *args.kw_defaults], strict=True
    ):
        if arg.arg == body_parameter:
            continue
        annotation = _name(arg.annotation)
        if (
            annotation not in scalar_types
            or not isinstance(marker, ast.Call)
            or _qualified(marker.func, imports) != "fastapi.Query"
            or len(marker.args) > 1
            or any(item.arg not in {"default", "alias"} for item in marker.keywords)
            or len({item.arg for item in marker.keywords}) != len(marker.keywords)
        ):
            return None
        default = _keyword(marker, "default")
        if marker.args:
            if default is not None:
                return None
            default = marker.args[0]
        required = isinstance(default, ast.Constant) and default.value is Ellipsis
        if default is not None and not required and _literal(default) is None:
            return None
        alias = _keyword(marker, "alias")
        name = _field(alias) if alias is not None else arg.arg
        if name is None or name in fields:
            return None
        fields[name] = {
            "type": annotation,
            "required": default is None or required,
            "line": arg.lineno,
        }
    return fields


def _body_model(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    models: dict[str, tuple[int, dict[str, dict[str, Any]]]],
) -> tuple[str, dict[str, dict[str, Any]]] | None:
    args = function.args
    required = [
        *[*args.posonlyargs, *args.args][
            : (len(args.posonlyargs) + len(args.args) - len(args.defaults))
        ],
        *(
            arg
            for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
            if default is None
        ),
    ]
    candidates = []
    for arg in required:
        if (
            isinstance(arg.annotation, ast.Name)
            and (model := models.get(arg.annotation.id))
            and model[0] < function.lineno
        ):
            candidates.append((arg.arg, model[1]))
    return candidates[0] if len(candidates) == 1 else None


def _http_tests(
    tree: ast.Module, applications: set[str], imports: dict[str, str]
) -> list[dict[str, Any]]:
    def application(value: ast.expr | None) -> str | None:
        if (
            isinstance(value, ast.Call)
            and len(value.args) == 1
            and not value.keywords
            and _qualified(value.func, imports)
            in {"fastapi.testclient.TestClient", "starlette.testclient.TestClient"}
        ):
            name = _name(value.args[0])
            return name if name in applications else None
        return None

    def json_receiver(value: ast.expr | None) -> str | None:
        if (
            isinstance(value, ast.Call)
            and not value.args
            and not value.keywords
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "json"
        ):
            return _name(value.func.value)
        return None

    globals_: dict[str, str] = {}
    fixtures: dict[str, str] = {}
    duplicates = Counter(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    )
    overrides = set()
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                name = _name(target)
                app = application(statement.value)
                if app and isinstance(target, ast.Name) and duplicates[target.id] == 1:
                    globals_[target.id] = app
                if isinstance(target, ast.Subscript):
                    name = _name(target.value)
                if name and name.endswith(".dependency_overrides"):
                    overrides.add(name.removesuffix(".dependency_overrides"))
        if (
            isinstance(statement, ast.FunctionDef)
            and not _parameters(statement.args)
            and len(statement.body) == 1
            and isinstance(statement.body[0], ast.Return)
            and len(statement.decorator_list) == 1
            and _qualified(statement.decorator_list[0], imports) == "pytest.fixture"
            and (app := application(statement.body[0].value))
        ):
            fixtures[statement.name] = app

    tests = []
    for function in tree.body[:MAX_FUNCTIONS]:
        if not isinstance(function, ast.FunctionDef) or not function.name.startswith("test_"):
            continue
        nodes = list(islice(ast.walk(function), MAX_EVENTS + 1))
        if len(nodes) > MAX_EVENTS or any(
            isinstance(node, (ast.Global, ast.Nonlocal)) for node in nodes
        ):
            continue
        params = {param["name"] for param in _parameters(function.args)}
        local = {
            node.id
            for node in nodes
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
        }
        clients = {name: app for name, app in globals_.items() if name not in (local | params)}
        clients.update((name, fixtures[name]) for name in params - local if name in fixtures)
        responses: dict[str, dict[str, Any]] = {}
        decoded: dict[str, dict[str, Any]] = {}
        for statement in function.body:
            if isinstance(statement, ast.Assert):
                expression = statement.test
                left = expression.left if isinstance(expression, ast.Compare) else expression
                if isinstance(left, ast.Subscript) and (field := _field(left.slice)):
                    probe = responses.get(json_receiver(left.value) or "") or decoded.get(
                        _name(left.value) or ""
                    )
                    if probe is not None:
                        probe["assertions"].append({"field": field, "line": statement.lineno})
                continue
            if isinstance(statement, (ast.Return, ast.Raise)):
                break
            if isinstance(statement, ast.Pass):
                continue
            if not isinstance(statement, (ast.Assign, ast.Expr)):
                break
            name = (
                statement.targets[0].id
                if isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                else ""
            )
            if isinstance(statement, ast.Assign) and not name:
                break
            concern = statement.value
            from_response = responses.get(json_receiver(concern) or "")
            responses.pop(name, None)
            decoded.pop(name, None)
            if from_response is not None and name:
                decoded[name] = from_response
                continue
            if not (
                isinstance(concern, ast.Call)
                and isinstance(concern.func, ast.Attribute)
                and concern.args
                and (receiver := _name(concern.func.value)) in clients
                and concern.func.attr in {"get", "post", "put", "patch", "delete"}
                and (path := _safe_route(concern.args[0]))
                and "{" not in path
                and "}" not in path
            ):
                # An opaque expression can mutate previously returned objects.
                responses.clear()
                decoded.clear()
                continue
            body = _keyword(concern, "json")
            body_fields = [_field(key) for key in body.keys] if isinstance(body, ast.Dict) else []
            app = clients[receiver]
            probe = {
                "test": function.name,
                "app": app,
                "method": concern.func.attr.upper(),
                "path": path,
                "line": concern.lineno,
                "fixture": receiver if receiver in params else None,
                "override_declared": app in overrides,
                "body_fields": sorted(field for field in body_fields if field is not None)
                if isinstance(body, ast.Dict)
                and all(field is not None for field in body_fields)
                and len(concern.args) == 1
                and all(kw.arg == "json" for kw in concern.keywords)
                else None,
                "assertions": [],
            }
            tests.append(probe)
            if name:
                responses[name] = probe
            if len(tests) >= MAX_EVENTS:
                return tests
    return tests


def _response_models(
    tree: ast.Module, imports: dict[str, str], scalar_types: set[str], bindings: Counter[str]
) -> dict[str, tuple[int, dict[str, dict[str, Any]]]]:
    models = {}
    for declaration in tree.body:
        if (
            not isinstance(declaration, ast.ClassDef)
            or bindings[declaration.name] != 1
            or declaration.decorator_list
            or declaration.keywords
            or len(declaration.bases) != 1
            or _qualified(declaration.bases[0], imports) != "pydantic.BaseModel"
        ):
            continue
        fields: dict[str, dict[str, Any]] = {}
        for field in declaration.body:
            if isinstance(field, ast.Pass):
                continue
            if (
                isinstance(field, ast.Expr)
                and isinstance(field.value, ast.Constant)
                and isinstance(field.value.value, str)
            ):
                continue
            if (
                not isinstance(field, ast.AnnAssign)
                or not field.simple
                or not isinstance(field.target, ast.Name)
                or field.target.id.startswith("_")
                or field.target.id in fields
                or _name(field.annotation) not in scalar_types
                or (field.value is not None and _literal(field.value) is None)
            ):
                break
            fields[field.target.id] = {
                "type": _name(field.annotation),
                "required": field.value is None,
                "line": field.lineno,
            }
        else:
            models[declaration.name] = declaration.lineno, fields
    return models


def _database_accesses(
    tree: ast.Module,
    imports: dict[str, str],
    models: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    columns, orders = [], []
    for function in tree.body[:MAX_FUNCTIONS]:
        if not isinstance(function, ast.FunctionDef):
            continue
        nodes = list(islice(ast.walk(function), MAX_EVENTS + 1))
        if len(nodes) > MAX_EVENTS or any(
            isinstance(node, (ast.Global, ast.Nonlocal)) for node in nodes
        ):
            continue
        parameter_names = {param["name"] for param in _parameters(function.args)}
        rebound = {
            node.id
            for node in nodes
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
        }
        sessions = {
            arg.arg
            for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
            if _qualified(arg.annotation, imports)
            in {"sqlalchemy.orm.Session", "sqlalchemy.orm.session.Session"}
        } - rebound
        if not sessions:
            continue
        known_models = models.keys() - parameter_names - rebound
        operations: dict[str, list[dict[str, Any]]] = {name: [] for name in sessions}
        for statement in function.body:
            if isinstance(statement, ast.Raise):
                break
            value = (
                statement.value
                if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign, ast.Return))
                else None
            )
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                receiver = _name(value.func.value)
                method = value.func.attr
                if receiver is not None and receiver in sessions:
                    columns.extend(
                        _column_access(
                            value,
                            method,
                            function.name,
                            known_models,
                            models,
                            imports,
                            rebound | parameter_names,
                        )
                    )
                    if (
                        method in {"add", "flush", "commit", "rollback"}
                        and not value.keywords
                        and (len(value.args) == 1 if method == "add" else not value.args)
                    ):
                        operations[receiver].append({"operation": method, "line": value.lineno})
                if method == "with_entities" and isinstance(query := value.func.value, ast.Call):
                    if (
                        isinstance(query.func, ast.Attribute)
                        and query.func.attr == "query"
                        and _name(query.func.value) in sessions
                        and len(query.args) == 1
                        and not query.keywords
                        and not value.keywords
                        and (model := _name(query.args[0]))
                    ):
                        fields = [
                            arg.attr
                            for arg in value.args
                            if isinstance(arg, ast.Attribute) and _name(arg.value) == model
                        ]
                        columns.extend(
                            _column_records(
                                query,
                                function.name,
                                model,
                                fields,
                                "read",
                                known_models,
                                models,
                                imports,
                                rebound | parameter_names,
                            )
                        )
            if isinstance(statement, ast.Return):
                for session in operations.values():
                    session.append({"operation": "return", "line": statement.lineno})
                break
            if not isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign, ast.Pass)):
                break
            if any(
                isinstance(node, ast.Call)
                and any(
                    _name(arg) in sessions
                    for arg in (*node.args, *(kw.value for kw in node.keywords))
                )
                for node in ast.walk(statement)
            ) or (isinstance(statement, (ast.Assign, ast.AnnAssign)) and _name(value) in sessions):
                break
        for receiver, sequence in sorted(operations.items()):
            if len(sequence) >= 2 and sequence[-1]["operation"] == "return":
                orders.append({"function": function.name, "session": receiver, "steps": sequence})
    return columns[:MAX_EVENTS], orders[:MAX_EVENTS]


def _column_access(
    call: ast.Call,
    method: str,
    function: str,
    known_models: set[str],
    models: dict[str, str],
    imports: dict[str, str],
    shadowed: set[str],
) -> list[dict[str, Any]]:
    if len(call.args) != 1 or call.keywords or not isinstance(value := call.args[0], ast.Call):
        return []
    model = _name(value.func)
    if (
        method == "add"
        and model is not None
        and not value.args
        and all(item.arg is not None for item in value.keywords)
    ):
        return _column_records(
            call,
            function,
            model,
            [item.arg for item in value.keywords if item.arg],
            "write",
            known_models,
            models,
            imports,
            shadowed,
        )
    if (
        method in {"execute", "scalar", "scalars"}
        and _qualified(value.func, imports) == "sqlalchemy.select"
        and not value.keywords
    ):
        records = []
        for arg in value.args:
            if (
                isinstance(arg, ast.Attribute)
                and (owner := _name(arg.value)) is not None
                and owner in known_models
            ):
                records.extend(
                    _column_records(
                        call,
                        function,
                        owner,
                        [arg.attr],
                        "read",
                        known_models,
                        models,
                        imports,
                        shadowed,
                    )
                )
        return records
    return []


def _column_records(
    call: ast.Call,
    function: str,
    model: str,
    fields: list[str],
    direction: str,
    known_models: set[str],
    models: dict[str, str],
    imports: dict[str, str],
    shadowed: set[str],
) -> list[dict[str, Any]]:
    first, dot, rest = model.partition(".")
    table = models.get(model) if model in known_models else None
    imported = imports[first] + dot + rest if first in imports and first not in shadowed else None
    if table is None and imported is None:
        return []
    span = [call.lineno, call.col_offset, call.end_lineno, call.end_col_offset]
    target = _name(call.func)
    return [
        {
            "field": f"{table}.{field}" if table else None,
            "member": field,
            "model_reference": imported,
            "direction": direction,
            "line": call.lineno,
            "span": span,
            "function": function,
            "method": target.rsplit(".", 1)[-1] if target else None,
        }
        for field in sorted(set(fields))
    ]


def _module_properties(
    tree: ast.Module,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    imports = _import_names(tree)
    records: list[dict[str, Any]] = []
    routers: dict[str, str] = {}
    router_lines: dict[str, int] = {}
    simple_routers = set()
    applications = set()
    assignments: Counter[str] = Counter(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    )
    bindings = assignments.copy()
    bindings.update(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )
    # Include all imports for shadowing, even those excluded as ambiguous by _import_names.
    bindings.update(
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    )
    scalar_types = _WIRE_TYPES - bindings.keys() if "*" not in bindings else set()
    models = _response_models(tree, imports, scalar_types, bindings)
    bases: dict[str, int] = {}
    orm_models: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            name = _name(statement.targets[0])
            call: ast.expr | None = statement.value
            if (
                name
                and assignments[name] == 1
                and isinstance(call, ast.Call)
                and _qualified(call.func, imports) in {"fastapi.FastAPI", "fastapi.APIRouter"}
            ):
                prefix = _keyword(call, "prefix")
                route = _safe_route(prefix) if prefix is not None else ""
                if route is not None:
                    routers[name] = route
                    router_lines[name] = statement.lineno
                    if not call.args and all(item.arg == "prefix" for item in call.keywords):
                        simple_routers.add(name)
                        if (
                            _qualified(call.func, imports) == "fastapi.FastAPI"
                            and not call.keywords
                        ):
                            applications.add(name)
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for index, dec in enumerate(statement.decorator_list):
                if (
                    not isinstance(dec, ast.Call)
                    or not isinstance(dec.func, ast.Attribute)
                    or not dec.args
                ):
                    continue
                router, method = _name(dec.func.value), dec.func.attr
                route = _safe_route(dec.args[0])
                if (
                    router in routers
                    and method in {"get", "post", "put", "delete", "patch", "head", "options"}
                    and route is not None
                ):
                    path = routers[router] + route
                    simple_route = (
                        router in simple_routers
                        and router_lines[router] < dec.lineno
                        and len(statement.decorator_list) == 1
                        and len(dec.args) == 1
                        and all(
                            item.arg in {"status_code", "response_model"} for item in dec.keywords
                        )
                        and "{" not in path
                        and "}" not in path
                    )
                    result_model = _keyword(dec, "response_model")
                    model = (
                        models.get(result_model.id) if isinstance(result_model, ast.Name) else None
                    )
                    response = (
                        model[1] if simple_route and model and model[0] < dec.lineno else None
                    )
                    body_model = _body_model(statement, models) if simple_route else None
                    query = (
                        _query_fields(
                            statement, imports, scalar_types, body_model[0] if body_model else None
                        )
                        if simple_route
                        else None
                    )
                    records.append(
                        {
                            "id": f"route:{statement.name}:{index}",
                            "line": dec.lineno,
                            "property": "api_route_declaration",
                            "entity": statement.name,
                            "value": {
                                "path": path,
                                "method": method.upper(),
                                "status": _literal(_keyword(dec, "status_code")),
                                "mount": "unverified_local_declaration",
                                "query_fields": query,
                                "body_fields": body_model[1]
                                if body_model and query is not None
                                else None,
                                "response_fields": response,
                                "same_file_app": router
                                if router in applications and simple_route
                                else None,
                            },
                        }
                    )
        if isinstance(statement, ast.ClassDef):
            if (
                bindings[statement.name] == 1
                and len(statement.bases) == 1
                and _qualified(statement.bases[0], imports) == "sqlalchemy.orm.DeclarativeBase"
                and not statement.keywords
                and not statement.decorator_list
            ):
                bases[statement.name] = statement.lineno
            body = [
                node for node in statement.body if isinstance(node, (ast.Assign, ast.AnnAssign))
            ]
            table: str | None = None
            for node in body:
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and _name(node.targets[0]) == "__tablename__"
                ):
                    table = _field(node.value)
            if table:
                if (
                    bindings[statement.name] == 1
                    and len(statement.bases) == 1
                    and (base := _name(statement.bases[0])) in bases
                    and bases[base] < statement.lineno
                    and not statement.keywords
                    and not statement.decorator_list
                ):
                    orm_models[statement.name] = table
                    records.append(
                        {
                            "id": f"model:{statement.name}",
                            "line": statement.lineno,
                            "property": "orm_model_inventory",
                            "entity": table,
                            "value": {
                                "fields": sorted(
                                    {
                                        name
                                        for node in body
                                        for target in (
                                            node.targets
                                            if isinstance(node, ast.Assign)
                                            else [node.target]
                                        )
                                        if (name := _name(target)) and "." not in name
                                    }
                                )
                            },
                        }
                    )
                for node in body:
                    column = (
                        _name(node.target)
                        if isinstance(node, ast.AnnAssign)
                        else (_name(node.targets[0]) if len(node.targets) == 1 else None)
                    )
                    call = node.value
                    if not column or not isinstance(call, ast.Call):
                        continue
                    if _qualified(call.func, imports) not in {
                        "sqlalchemy.orm.mapped_column",
                        "sqlalchemy.Column",
                    }:
                        continue
                    records.append(
                        {
                            "id": f"column:{statement.name}:{column}",
                            "line": node.lineno,
                            "property": "orm_column_declaration",
                            "entity": f"{table}.{column}",
                            "value": {
                                "name": _field(call.args[0]) if call.args else None,
                                "nullable": _literal(_keyword(call, "nullable")),
                                "unique": _literal(_keyword(call, "unique")),
                                "primary_key": _literal(_keyword(call, "primary_key")),
                                "default_declared": any(
                                    item.arg in {"default", "server_default"}
                                    for item in call.keywords
                                ),
                            },
                        }
                    )
    direct_functions = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    scope_types = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    scopes = {"<module>": [node for node in tree.body if not isinstance(node, scope_types)]}
    scopes.update((node.name, node.body) for node in direct_functions if bindings[node.name] == 1)
    for scope, statements in scopes.items():
        nodes = list(islice(ast.walk(ast.Module(body=statements, type_ignores=[])), MAX_EVENTS + 1))
        if len(nodes) > MAX_EVENTS or any(isinstance(node, scope_types) for node in nodes):
            continue
        calls = [
            node
            for node in nodes
            if isinstance(node, ast.Call) and _qualified(node.func, imports) == "os.getenv"
        ]
        if len(calls) != 1:
            continue
        env_node = calls[0]
        if (
            not env_node.args
            or len(env_node.args) > 2
            or any(item.arg != "default" for item in env_node.keywords)
        ):
            continue
        key = _field(env_node.args[0])
        if key is None or not key.isascii() or key.upper() != key:
            continue
        default = env_node.args[1] if len(env_node.args) == 2 else _keyword(env_node, "default")
        shape = _literal(default)
        # Retain literal type and a one-way digest. Effective or secret values are never loaded.
        records.append(
            {
                "id": f"getenv:{scope}",
                "line": env_node.lineno,
                "property": "environment_default",
                "entity": key,
                "value": {
                    "declared": default is not None,
                    "key": key,
                    "scope": scope,
                    "literal_kind": shape["kind"] if shape else None,
                    "default_digest": digest(shape) if shape else None,
                    "condition": "no_environment_override",
                },
            }
        )
    tables = Counter(orm_models.values())
    accesses, transactions = _database_accesses(
        tree, imports, {model: table for model, table in orm_models.items() if tables[table] == 1}
    )
    return (
        records[:MAX_EVENTS],
        _http_tests(tree, applications, imports) if applications else [],
        accesses,
        transactions,
    )
