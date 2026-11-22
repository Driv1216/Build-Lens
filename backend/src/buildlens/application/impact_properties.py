"""Property comparators shared by snapshot, hypothesis and patch inputs."""

import keyword
import re
from collections import Counter, deque
from copy import deepcopy
from typing import Any

from buildlens.domain.impact import MAX_FRAMES, Threshold, integer_difference
from buildlens.domain.source import digest


def argument_problem(call: dict[str, Any], params: list[dict[str, Any]]) -> str | None:
    if call["unpacking"]:
        return None
    positional = [item for item in params if item["kind"] in {"posonly", "positional"}]
    keywords = {item["name"] for item in params if item["kind"] in {"positional", "keyword"}}
    variadic = {item["kind"] for item in params}
    count = call["count"]
    passed = {item["name"] for item in positional[:count]}
    if count > len(positional) and "vararg" not in variadic:
        return "too_many_positional_arguments"
    for name in call["keywords"]:
        if name in passed and name in keywords:
            return "duplicate_argument"
        if name not in keywords and "kwarg" not in variadic:
            return "unexpected_keyword"
        if name in keywords:
            passed.add(name)
    if any(item["required"] and item["name"] not in passed for item in params):
        return "missing_required_argument"
    return None


def _by_name(projection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    functions = projection.get("functions", [])
    counts = Counter(function["name"] for function in functions)
    return {function["name"]: function for function in functions if counts[function["name"]] == 1}


def _input_binding(function: dict[str, Any], name: str) -> tuple[str, int | None] | None:
    for index, entry in enumerate(function.get("signature", [])):
        if entry["name"] == name and entry["kind"] in {"positional", "posonly", "keyword"}:
            return entry["kind"], None if entry["kind"] == "keyword" else index
    return None


def _http_test_findings(
    change: dict[str, Any],
    tests_before: list[dict[str, Any]],
    tests_after: list[dict[str, Any]],
    old_app: str | None,
    new_app: str | None,
) -> list[dict[str, Any]]:
    if old_app is None or old_app != new_app or change["old_route"] != change["new_route"]:
        return []
    route = change["old_route"]
    first = [
        probe
        for probe in tests_before
        if (probe["app"], probe["method"], probe["path"])
        == (old_app, route["method"], route["path"])
    ]
    second = [
        probe
        for probe in tests_after
        if (probe["app"], probe["method"], probe["path"])
        == (new_app, route["method"], route["path"])
    ]
    old_counts = Counter((item["test"], item["fixture"]) for item in first)
    new_counts = Counter((item["test"], item["fixture"]) for item in second)
    findings = []
    for probe in second:
        key = probe["test"], probe["fixture"]
        if old_counts[key] != 1 or new_counts[key] != 1:
            continue
        assertions = [
            item
            for item in probe["assertions"]
            if item["field"] == change["field"] and change["property"] == "api_response_contract"
        ]
        missing_body = (
            change["property"] == "api_body_contract"
            and change["after"]
            and change["after"]["required"]
            and (not change["before"] or not change["before"]["required"])
            and probe["body_fields"] is not None
            and change["field"] not in probe["body_fields"]
        )
        consequence = (
            "test_asserts_affected_response_field"
            if assertions
            else (
                "test_supplies_body_without_new_required_field"
                if missing_body
                else "test_client_execution_exposure"
            )
        )
        witness = {
            "line": probe["line"],
            "fixture": probe["fixture"],
            "route": route,
            "field": change["field"],
            "override_declared": probe["override_declared"],
            "conditions": ["TEST_EXECUTION_UNMEASURED", "EFFECTIVE_DEPENDENCY_OVERRIDES_UNKNOWN"],
        }
        findings.append(
            {
                "origin": change["id"],
                "entity": probe["test"],
                "locator": change["locator"],
                "mechanism": "data"
                if assertions
                else "compatibility"
                if missing_body
                else "structural",
                "consequence": consequence,
                "relationship_tier": "derived",
                "distance": "direct",
                "witness": witness,
                "test_candidate": True,
                "test_assertions": assertions,
            }
        )
    return findings


def _column_findings(
    change: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[dict[str, Any]]:
    findings = []
    for side, projection in (("old", before), ("new", after)):
        if (side == "old" and change["comparison"] == "introduced") or (
            side == "new" and change["comparison"] == "removed"
        ):
            continue
        for access in projection.get("database_accesses", []):
            if access["field"] != change["entity"]:
                continue
            findings.append(
                {
                    "origin": change["id"],
                    "entity": access["function"],
                    "locator": change["locator"],
                    "side": side,
                    "mechanism": "shared_state",
                    "relationship_tier": "derived",
                    "distance": "direct",
                    "consequence": "mapped_column_read"
                    if access["direction"] == "read"
                    else "mapped_column_write_input",
                    "witness": {
                        **access,
                        "conditions": [
                            "ANNOTATED_SESSION_BINDING",
                            "DATABASE_EXECUTION_AND_DURABILITY_UNMEASURED",
                        ],
                    },
                }
            )
    return findings


def _integer_test_inputs(
    change: dict[str, Any],
    functions: dict[str, dict[str, Any]],
    provider: dict[str, Any],
    position: int,
) -> list[dict[str, Any]]:
    results = []
    for function in functions.values():
        if not function["name"].startswith("test_"):
            continue
        for call in function.get("calls", []):
            values = call.get("inputs", [])
            if (
                not call["local"]
                or call["target"] != provider["name"]
                or call["unpacking"]
                or argument_problem(call, provider["signature"]) is not None
                or position >= len(values)
                or not (example := values[position])
                or example["kind"] != "int"
            ):
                continue
            value = example["value"]
            if any(
                (region["min"] is None or region["min"] <= value)
                and (region["max"] is None or value <= region["max"])
                for region in change["regions"]
            ):
                results.append(
                    {
                        "origin": change["id"],
                        "entity": function["name"],
                        "locator": change["locator"],
                        "mechanism": "control",
                        "distance": "direct",
                        "relationship_tier": "derived",
                        "consequence": "test_literal_input_in_changed_region",
                        "test_candidate": True,
                        "matching_input": value,
                        "witness": {
                            "line": call["line"],
                            "conditions": ["TEST_EXECUTION_UNMEASURED"],
                        },
                    }
                )
    return results


def _findings(
    change: dict[str, Any],
    new_functions: dict[str, dict[str, Any]],
    symbol: dict[str, Any],
) -> list[dict[str, Any]]:
    if not symbol.get("eligible_binding") or symbol.get("result_kind", "value") != "value":
        return []
    if change["property"] == "return_field":
        return _field_consumers(change, new_functions, symbol["name"])
    found = []
    for caller in new_functions.values():
        for call in caller.get("calls", []):
            if not call["local"] or call["target"] != symbol["name"]:
                continue
            evidence = {
                "line": call["line"],
                "column": call["column"],
                "caller": caller["name"],
                "target": symbol["name"],
                "conditional_call": call.get("guarded", False),
            }
            consequence: str | None = None
            if change["property"] == "signature" and not call["unpacking"]:
                old = argument_problem(call, change["before"])
                new = argument_problem(call, change["after"])
                if old is None and new:
                    consequence = new
            if consequence:
                found.append(
                    {
                        "origin": change["id"],
                        "entity": caller["name"],
                        "mechanism": "compatibility",
                        "consequence": consequence,
                        "relationship_tier": "derived",
                        "witness": evidence,
                        "distance": "direct",
                        "locator": change["locator"],
                    }
                )
    return found


def _field_consumers(
    change: dict[str, Any], functions: dict[str, dict[str, Any]], source: str
) -> list[dict[str, Any]]:
    """Follow direct calls and transparent forwarding returns, with matched callsite indexes."""
    by_target: dict[str, list[tuple[dict[str, Any], int, dict[str, Any]]]] = {}
    for function in functions.values():
        for index, call in enumerate(function.get("calls", [])):
            if call["local"] and not call["unpacking"]:
                by_target.setdefault(call["target"], []).append((function, index, call))
    found: list[dict[str, Any]] = []
    queue: deque[tuple[str, tuple[dict[str, Any], ...], frozenset[str]]] = deque(
        [(source, (), frozenset({source}))]
    )
    expansions = 0
    while queue:
        target, context, seen = queue.popleft()
        for function, index, call in by_target.get(target, []):
            if expansions >= 2000:
                return found
            expansions += 1
            target_function = functions.get(target, {})
            if target_function.get("result_kind", "value") != "value":
                continue
            if "signature" in target_function and argument_problem(
                call, target_function["signature"]
            ):
                continue
            step = {
                "line": call["line"],
                "column": call["column"],
                "caller": function["name"],
                "target": target,
            }
            for read in function.get("reads", []):
                if read["call"] != index or read["field"] != change["field"]:
                    continue
                checks = (
                    [
                        assertion
                        for assertion in function.get("assertions", [])
                        if assertion["call"] == index
                        and assertion["field"] == change["field"]
                        and assertion["line"] == read["line"]
                    ]
                    if function["name"].startswith("test_")
                    else []
                )
                found.append(
                    {
                        "origin": change["id"],
                        "entity": function["name"],
                        "mechanism": "data",
                        "consequence": "matched_field_consumer",
                        "relationship_tier": "derived",
                        "witness": {
                            **step,
                            "read_line": read["line"],
                            "conditional_read": read["guarded"],
                            "forwarding": list(context),
                        },
                        "distance": "indirect" if context else "direct",
                        "locator": change["locator"],
                        "test_assertions": checks,
                    }
                )
            returns = function.get("returns", [])
            name = function["name"]
            if (
                len(returns) == 1
                and returns[0].get("forward") == index
                and name not in seen
                and len(context) + 1 < MAX_FRAMES
                and function.get("eligible_binding")
            ):
                queue.append(
                    (name, (*context, {**step, "return_line": returns[0]["line"]}), seen | {name})
                )
    return found


def compare_syntax(
    before: dict[str, Any],
    after: dict[str, Any],
    locator: str,
    authority: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    old, new = _by_name(before), _by_name(after)
    changes: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    gaps = set() if before["complete"] and after["complete"] else {"PARTIAL_SYNTAX_PROJECTION"}
    for side in (before, after):
        gaps.update(side.get("gaps", []))
        for function in side.get("functions", []):
            gaps.update(function.get("gaps", []))
        unaligned = len(side.get("functions", [])) != len(_by_name(side))
        if unaligned:
            gaps.add("AMBIGUOUS_DECLARATION_MATCH")

    def add(name: str, kind: str, left: Any, right: Any, **specific: Any) -> dict[str, Any]:
        record = {
            "entity": name,
            "locator": locator,
            "property": kind,
            "before": left,
            "after": right,
            "authority": authority,
            **specific,
        }
        change = {**record, "id": digest(record)[:24]}
        changes.append(change)
        return change

    for name in sorted(old.keys() | new.keys()):
        left, right = old.get(name), new.get(name)
        if left is None or right is None:
            if (
                before["complete"]
                and after["complete"]
                and "AMBIGUOUS_DECLARATION_MATCH" not in gaps
            ):
                change = add(
                    name,
                    "declaration",
                    left and left["line"],
                    right and right["line"],
                    comparison="introduced" if left is None else "removed",
                )
                if (
                    left is not None
                    and left.get("eligible_binding")
                    and after.get("namespace_static")
                ):
                    for caller in new.values():
                        for call in caller.get("calls", []):
                            if call["target"] == name and not call["unpacking"]:
                                findings.append(
                                    {
                                        "origin": change["id"],
                                        "entity": caller["name"],
                                        "locator": locator,
                                        "mechanism": "compatibility",
                                        "consequence": "call_target_declaration_removed",
                                        "relationship_tier": "derived",
                                        "distance": "direct",
                                        "witness": {
                                            "line": call["line"],
                                            "target": name,
                                            "conditions": [
                                                "LOCAL_DECLARATION_REMOVED",
                                                "REPLACEMENT_OR_EXTERNAL_BINDING_UNRESOLVED",
                                            ],
                                        },
                                    }
                                )
                    gaps.add("REMOVED_SYMBOL_REPLACEMENT_BINDING_UNKNOWN")
            continue
        if left.get("syntax") == right.get("syntax") and left == right:
            continue
        if left.get("body_syntax") != right.get("body_syntax"):
            gaps.add("BODY_BEHAVIOR_COMPARISON_PARTIAL")
        for side in (left, right):
            gaps.update(side.get("gaps", []))
        if (
            left.get("signature") != right.get("signature")
            and "signature" in left
            and "signature" in right
        ):
            change = add(
                name,
                "signature",
                left["signature"],
                right["signature"],
                old_line=left["line"],
                new_line=right["line"],
                comparison="modified",
            )
            # Compare only uniquely aligned continuing invocation shapes. An added or repaired
            # caller is not evaluated as if it still used the old callsite.
            continuing = {}
            for caller_name in sorted(old.keys() & new.keys()):
                old_calls = Counter(
                    (call["target"], call["count"], tuple(call["keywords"]), call["unpacking"])
                    for call in old[caller_name].get("calls", [])
                    if call["local"]
                )
                new_calls = Counter(
                    (call["target"], call["count"], tuple(call["keywords"]), call["unpacking"])
                    for call in new[caller_name].get("calls", [])
                    if call["local"]
                )
                selected = [
                    call
                    for call in new[caller_name].get("calls", [])
                    if (
                        shape := (
                            call["target"],
                            call["count"],
                            tuple(call["keywords"]),
                            call["unpacking"],
                        )
                    )
                    in old_calls
                    and old_calls[shape] == new_calls[shape] == 1
                ]
                continuing[caller_name] = {**new[caller_name], "calls": selected}
            findings.extend(_findings(change, continuing, right))
        old_returns, new_returns = left.get("returns", []), right.get("returns", [])
        if len(old_returns) == len(new_returns) == 1:
            fields_before, fields_after = old_returns[0]["fields"], new_returns[0]["fields"]
            if isinstance(fields_before, dict) and isinstance(fields_after, dict):
                for field in sorted(fields_before.keys() | fields_after.keys()):
                    first, second = fields_before.get(field), fields_after.get(field)
                    introduced, removed = field not in fields_before, field not in fields_after
                    if (
                        introduced
                        or removed
                        or (first is not None and second is not None and first != second)
                    ):
                        change = add(
                            name,
                            "return_field",
                            first,
                            second,
                            field=field,
                            comparison="introduced"
                            if introduced
                            else "removed"
                            if removed
                            else "modified",
                            old_line=old_returns[0]["line"],
                            new_line=new_returns[0]["line"],
                        )
                        if not introduced:
                            findings.extend(_findings(change, new, right))
                            gaps.add("FIELD_DEPENDENCE_CONTEXT_BOUNDED")
        for key, prop in (("calls", "invocations"), ("raises", "explicit_exceptions")):
            if key not in left or key not in right:
                continue

            def shapes(value: dict[str, Any], key: str) -> list[str]:
                return sorted(
                    digest(
                        {
                            k: v
                            for k, v in row.items()
                            if k not in {"line", "column", "end_line", "end_column"}
                        }
                    )
                    for row in value[key]
                )

            if shapes(left, key) != shapes(right, key):
                add(name, prop, left[key], right[key], comparison="modified")
        previous, current = left.get("conditional_calls", []), right.get("conditional_calls", [])
        if len(previous) == len(current) == 1:
            a, b = previous[0], current[0]
            if (a["target"], a["arguments_digest"], a["predicate"]["binding"]) == (
                b["target"],
                b["arguments_digest"],
                b["predicate"]["binding"],
            ) and a["predicate"] != b["predicate"]:
                p, q = a["predicate"], b["predicate"]
                binding = _input_binding(left, p["binding"])
                if binding is None or binding != _input_binding(right, q["binding"]):
                    gaps.add("INTEGER_INPUT_BINDING_INCOMPARABLE")
                else:
                    regions = integer_difference(
                        Threshold(p["operator"], p["limit"]), Threshold(q["operator"], q["limit"])
                    )
                    change = add(
                        name,
                        "integer_guard",
                        p,
                        q,
                        comparison="modified",
                        regions=regions,
                        old_line=a["line"],
                        new_line=b["line"],
                    )
                    if regions:
                        findings.append(
                            {
                                "origin": change["id"],
                                "entity": b["target"],
                                "locator": locator,
                                "mechanism": "control",
                                "consequence": "conditional_operation_changed",
                                "relationship_tier": "derived",
                                "distance": "direct",
                                "regions": regions,
                                "witness": {
                                    "old_line": a["line"],
                                    "new_line": b["line"],
                                    "condition": "same_exact_integer_input_reaches_comparison",
                                },
                            }
                        )
                        if binding[1] is not None:
                            findings.extend(_integer_test_inputs(change, new, right, binding[1]))
        if (
            left["syntax"] is not None
            and right["syntax"] is not None
            and left["syntax"] != right["syntax"]
        ):
            # Annotation, decorator, and other declaration semantics are wider
            # than the supported property comparators, even with equal bodies.
            gaps.add("DECLARATION_SEMANTICS_NOT_FULLY_COMPARED")
            add(
                name,
                "declaration_syntax",
                left["syntax"],
                right["syntax"],
                comparison="modified",
                old_line=left["line"],
                new_line=right["line"],
            )
    if before.get("module_syntax") != after.get("module_syntax"):
        gaps.add("MODULE_BEHAVIOR_COMPARISON_PARTIAL")
    # Compare static local declarations. Runtime mounting, migrations and effective environment
    # values are outside these source records.
    first = {row["id"]: row for row in before.get("module_properties", [])}
    second = {row["id"]: row for row in after.get("module_properties", [])}
    for key in sorted(first.keys() & second.keys()):
        left, right = first[key], second[key]
        prior, current = left["value"], right["value"]
        if right["property"] == "orm_model_inventory":
            continue
        if right["property"] == "api_route_declaration":
            # These properties have different comparison domains. A schema field must
            # never masquerade as a change to the method, path, or declared status.
            for family in ("query", "body", "response"):
                field_key = f"{family}_fields"
                earlier, later = prior.get(field_key), current.get(field_key)
                if earlier is None or later is None:
                    if earlier is not None or later is not None:
                        gaps.add(f"API_{family.upper()}_CONTRACT_INCOMPARABLE")
                    continue
                for field in sorted(earlier.keys() | later.keys()):
                    old_field, new_field = earlier.get(field), later.get(field)
                    if (old_field and (old_field["type"], old_field["required"])) == (
                        new_field and (new_field["type"], new_field["required"])
                    ):
                        continue
                    change = add(
                        right["entity"],
                        f"api_{family}_contract",
                        old_field,
                        new_field,
                        field=field,
                        comparison="introduced"
                        if old_field is None
                        else "removed"
                        if new_field is None
                        else "modified",
                        old_line=old_field["line"] if old_field else left["line"],
                        new_line=new_field["line"] if new_field else right["line"],
                        old_route={key: prior[key] for key in ("method", "path")},
                        new_route={key: current[key] for key in ("method", "path")},
                    )
                    findings.extend(
                        _http_test_findings(
                            change,
                            before.get("http_tests", []),
                            after.get("http_tests", []),
                            prior.get("same_file_app"),
                            current.get("same_file_app"),
                        )
                    )
            prior = {name: prior[name] for name in ("path", "method", "status", "mount")}
            current = {name: current[name] for name in ("path", "method", "status", "mount")}
        if prior != current:
            family = right["property"]
            if family == "environment_default" and prior["key"] != current["key"]:
                family = "environment_key"
            change = add(
                right["entity"],
                family,
                prior,
                current,
                comparison="modified",
                old_line=left["line"],
                new_line=right["line"],
                **(
                    {"model": key.split(":", 2)[1], "old_entity": left["entity"]}
                    if family == "orm_column_declaration"
                    else {}
                ),
            )
            if change["property"] == "orm_column_declaration" and left["entity"] == right["entity"]:
                findings.extend(_column_findings(change, before, after))
            if family in {
                "environment_default",
                "environment_key",
                "framework_version_declaration",
            }:
                gaps.add("EFFECTIVE_CONFIGURATION_UNAVAILABLE")
                if family != "framework_version_declaration":
                    findings.append(
                        {
                            "origin": change["id"],
                            "entity": current["scope"],
                            "locator": locator,
                            "mechanism": "configuration",
                            "distance": "direct",
                            "consequence": "conditional_source_default_changed"
                            if family == "environment_default"
                            else "environment_lookup_key_changed",
                            "relationship_tier": "derived",
                            "witness": {
                                "old_line": left["line"],
                                "new_line": right["line"],
                                "conditions": ["EFFECTIVE_OVERRIDES_UNKNOWN", current["condition"]]
                                if family == "environment_default"
                                else ["EFFECTIVE_OVERRIDES_UNKNOWN"],
                            },
                        }
                    )
    qualified_members = set()
    for key in sorted(first.keys() ^ second.keys()):
        left, right = first.get(key), second.get(key)
        row = left or right
        if row is None or row["property"] != "orm_column_declaration":
            continue
        _, model, field = key.split(":", 2)
        model_key = f"model:{model}"
        a, b = first.get(model_key), second.get(model_key)
        absent_inventory = a if left is None else b
        if (
            a is None
            or b is None
            or a["entity"] != b["entity"]
            or absent_inventory is None
            or field in absent_inventory["value"]["fields"]
        ):
            continue
        change = add(
            row["entity"],
            row["property"],
            left["value"] if left else None,
            right["value"] if right else None,
            comparison="removed" if right is None else "introduced",
            old_line=left["line"] if left else a["line"],
            new_line=right["line"] if right else b["line"],
            model=model,
            old_entity=left["entity"] if left else row["entity"],
        )
        qualified_members.add(key)
        findings.extend(_column_findings(change, before, after))
        gaps.add("ORM_INHERITED_AND_RUNTIME_SCHEMA_UNKNOWN")
    if (first.keys() ^ second.keys()) - qualified_members:
        gaps.add("MODULE_PROPERTY_MEMBERSHIP_UNVERIFIED")

    old_tx = {(tx["function"], tx["session"]): tx["steps"] for tx in before.get("transactions", [])}
    new_tx = {(tx["function"], tx["session"]): tx["steps"] for tx in after.get("transactions", [])}
    for name, session in sorted(old_tx.keys() & new_tx.keys()):
        earlier, later = old_tx[(name, session)], new_tx[(name, session)]
        prior_ops = [step["operation"] for step in earlier]
        current_ops = [step["operation"] for step in later]
        if prior_ops == current_ops or not (
            {"commit", "flush", "rollback"} & set((*prior_ops, *current_ops))
        ):
            continue
        change = add(
            name, "transaction_sequence", earlier, later, comparison="modified", session=session
        )
        findings.append(
            {
                "origin": change["id"],
                "entity": name,
                "locator": locator,
                "mechanism": "shared_state",
                "distance": "direct",
                "consequence": "declared_session_operation_order_changed",
                "relationship_tier": "derived",
                "witness": {
                    "before": earlier,
                    "after": later,
                    "conditions": [
                        "ANNOTATED_SESSION_BINDING",
                        "NORMAL_EXECUTION_REACHES_STATEMENTS",
                        "DURABILITY_UNMEASURED",
                    ],
                },
            }
        )

    return changes, findings, gaps


def _scalar(value: Any) -> dict[str, Any] | None:
    if value is None or type(value) is bool or (type(value) is int and abs(value) <= 2**31):
        return {"kind": type(value).__name__, "value": value}
    if isinstance(value, str) and len(value) <= 2048:
        return {"kind": "str", "digest": digest(value)}
    return None


def _module_operation(candidate: dict[str, Any], operation: dict[str, Any]) -> bool:
    kind, target, member = operation["kind"], operation["target"], operation.get("member", "")
    records = candidate.get("module_properties", [])
    if kind in {
        "remove_api_field",
        "require_api_field",
        "rename_api_field",
        "set_endpoint_path",
        "set_endpoint_method",
    }:
        routes = [
            row
            for row in records
            if row["property"] == "api_route_declaration" and row["entity"] == target
        ]
        if len(routes) != 1:
            return False
        route = routes[0]["value"]
        if kind == "set_endpoint_path":
            value = operation.get("value")
            if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9/_{}:.-]{0,127}", value):
                return False
            if value == route["path"]:
                return False
            route["path"] = value
        elif kind == "set_endpoint_method":
            value = operation.get("value")
            if value not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                return False
            if value == route["method"]:
                return False
            route["method"] = value
        else:
            family = operation.get("family")
            fields = route.get(f"{family}_fields")
            if not isinstance(fields, dict) or member not in fields:
                return False
            if kind == "require_api_field":
                if fields[member]["required"]:
                    return False
                fields[member]["required"] = True
            elif kind == "rename_api_field":
                name = operation.get("new_name", "")
                if not name.isidentifier() or keyword.iskeyword(name) or name in fields:
                    return False
                fields[name] = fields.pop(member)
            else:
                del fields[member]
        candidate.setdefault("gaps", []).append("HYPOTHESIS_API_CONTRACT_LOCAL_ONLY")
        return True
    if kind in {"remove_database_column", "set_database_constraint"}:
        columns = [row for row in records if row["id"] == f"column:{target}:{member}"]
        models = [row for row in records if row["id"] == f"model:{target}"]
        if len(columns) != 1 or len(models) != 1:
            return False
        column = columns[0]
        if kind == "remove_database_column":
            if member not in models[0]["value"]["fields"]:
                return False
            models[0]["value"]["fields"].remove(member)
            records.remove(column)
        else:
            constraint, value = operation.get("constraint"), operation.get("value")
            if constraint not in {"nullable", "unique", "primary_key"} or type(value) is not bool:
                return False
            prior = column["value"][constraint]
            if prior is None or prior.get("kind") != "bool" or prior["value"] == value:
                return False
            column["value"][constraint] = _scalar(value)
        candidate.setdefault("gaps", []).append("ORM_INHERITED_AND_RUNTIME_SCHEMA_UNKNOWN")
        return True
    if kind == "set_configuration_default":
        scope = "<module>" if target == "__module__" else target
        matches = [
            row
            for row in records
            if row["property"] == "environment_default"
            and row["value"]["scope"] == scope
            and row["entity"] == member
        ]
        shape = _scalar(operation.get("value"))
        if len(matches) != 1 or shape is None or matches[0]["value"]["literal_kind"] is None:
            return False
        value = matches[0]["value"]
        if value["default_digest"] == digest(shape):
            return False
        value.update(declared=True, literal_kind=shape["kind"], default_digest=digest(shape))
        return True
    return False


def hypothetical(before: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any] | None:
    after = deepcopy(before)
    old, new = _by_name(before), _by_name(after)
    seen: set[tuple[str, str, str]] = set()
    symbol_targets = {
        op["target"] for op in operations if op["kind"] in {"rename_symbol", "remove_symbol"}
    }
    if any(sum(op["target"] == target for op in operations) != 1 for target in symbol_targets):
        return None
    for operation in operations:
        target, kind, member = operation["target"], operation["kind"], operation.get("member", "")
        if kind in {
            "remove_api_field",
            "require_api_field",
            "rename_api_field",
            "set_endpoint_path",
            "set_endpoint_method",
            "remove_database_column",
            "set_database_constraint",
            "set_configuration_default",
        }:
            family = "api_field:" + str(operation.get("family")) if "api_field" in kind else kind
            if kind == "set_database_constraint":
                family += ":" + str(operation.get("constraint"))
            coordinate = (target, family, member)
            baseline = {"module_properties": deepcopy(before.get("module_properties", []))}
            if (
                coordinate in seen
                or not _module_operation(baseline, operation)
                or not _module_operation(after, operation)
            ):
                return None
            seen.add(coordinate)
            continue
        prior, next_ = old.get(target), new.get(target)
        if kind in {"rename_symbol", "remove_symbol"}:
            if (
                prior is None
                or next_ is None
                or not prior.get("eligible_binding")
                or not before["complete"]
            ):
                return None
            if kind == "rename_symbol":
                name = operation.get("new_name", "")
                if (
                    not name.isidentifier()
                    or keyword.iskeyword(name)
                    or name in before.get("module_bindings", [])
                    or name in _by_name(after)
                ):
                    return None
                next_["name"] = name
                after.setdefault("gaps", []).append("RENAME_HYPOTHESIS_DOES_NOT_REWRITE_REFERENCES")
            else:
                after["functions"].remove(next_)
            continue
        property_family = (
            "signature"
            if kind in {"require_parameter", "remove_parameter"}
            else "return"
            if kind in {"remove_return_field", "set_return_boolean"}
            else kind
        )
        coordinate = (target, property_family, member)
        if prior is None or next_ is None or coordinate in seen:
            return None
        seen.add(coordinate)
        if kind in {"require_parameter", "remove_parameter"}:
            matches = [item for item in next_.get("signature", []) if item["name"] == member]
            if len(matches) != 1 or matches[0]["kind"] in {"vararg", "kwarg"}:
                return None
            if kind == "require_parameter":
                if matches[0]["required"]:
                    return None
                matches[0]["required"] = True
                matches[0]["default_digest"] = None
            else:
                next_["signature"].remove(matches[0])
        elif kind == "remove_return_field":
            returns = next_.get("returns", [])
            if (
                len(returns) != 1
                or not isinstance(returns[0]["fields"], dict)
                or member not in returns[0]["fields"]
            ):
                return None
            del returns[0]["fields"][member]
        elif kind == "set_return_boolean":
            returns = next_.get("returns", [])
            value = operation.get("value")
            if (
                len(returns) != 1
                or not isinstance(returns[0]["fields"], dict)
                or member not in returns[0]["fields"]
                or type(value) is not bool
            ):
                return None
            returns[0]["fields"][member] = {"kind": "bool", "value": value}
        elif kind == "set_integer_threshold":
            guards = next_.get("conditional_calls", [])
            value = operation.get("value")
            if len(guards) != 1 or type(value) is not int or abs(value) > 2**31:
                return None
            guards[0]["predicate"]["limit"] = value
        else:
            return None
        if prior["syntax"] is not None:
            next_["syntax"] = digest({"baseline": prior["syntax"], "candidate": next_})
    for candidate in new.values():
        default_seen = False
        for parameter in candidate.get("signature", []):
            if parameter["kind"] not in {"posonly", "positional"}:
                continue
            if parameter["required"] and default_seen:
                return None
            default_seen |= not parameter["required"]
    return after
