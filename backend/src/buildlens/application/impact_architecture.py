"""Scoped comparison of owner-declared rules against verified architecture edges."""

from functools import lru_cache
from typing import Any

from buildlens.domain.architecture_rules import Rule, glob_expression
from buildlens.domain.source import digest

_compile = lru_cache(maxsize=512)(glob_expression)


def _matches(module: str, selectors: tuple[str, ...]) -> bool:
    locator = module.removeprefix("module:")
    return any(
        selector == module
        if selector.startswith("module:")
        else bool(_compile(selector).match(locator))
        for selector in selectors
    )


def compare_dependencies(
    rules: tuple[Rule, ...],
    before: dict[str, Any],
    after: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    changes, findings = [], []
    gaps = {"ARCHITECTURE_SCOPED_MEMBERSHIP_UNVERIFIED"}
    observations = (before, after)
    if len(rules) > 32:
        gaps.add("ARCHITECTURE_RULE_LIMIT")
    for rule in rules[:32]:
        selectors = (rule.sources, rule.targets) if rule.kind == "forbid" else rule.layers
        if any(
            item.startswith(("container:", "workspace:")) for group in selectors for item in group
        ):
            gaps.add("ARCHITECTURE_HIERARCHICAL_SELECTOR_UNSUPPORTED")
            continue
        violations = []
        for observation in observations:
            matched = {}
            for edge in observation["edges"]:
                if edge["import_class"] not in rule.classes:
                    continue
                source, target = edge["source"], edge["target"]
                if rule.kind == "forbid":
                    violates = _matches(source, rule.sources) and _matches(target, rule.targets)
                else:
                    start = [i for i, group in enumerate(rule.layers) if _matches(source, group)]
                    end = [i for i, group in enumerate(rule.layers) if _matches(target, group)]
                    if len(start) > 1 or len(end) > 1:
                        gaps.add("ARCHITECTURE_LAYER_SELECTOR_OVERLAP")
                    violates = len(start) == len(end) == 1 and end[0] < start[0]
                if violates:
                    matched[(source, target, edge["relation"], edge["import_class"])] = edge
            violations.append(matched)
        old, new = violations
        for identity in sorted(old.keys() ^ new.keys()):
            side = "new" if identity in new else "old"
            edge = (new if side == "new" else old)[identity]
            change = {
                "property": "architecture_rule_observation",
                "comparison": "one_sided_observation",
                "entity": rule.name,
                "locator": edge["locator"],
                "authority": "architecture_rule",
                "before": old.get(identity),
                "after": new.get(identity),
            }
            origin = digest(change)[:24]
            changes.append({**change, "id": origin})
            findings.append(
                {
                    "origin": origin,
                    "entity": edge["target"],
                    "locator": edge["locator"],
                    "side": side,
                    "mechanism": "architecture",
                    "distance": "direct",
                    "consequence": "rule_violation_observed_on_one_side",
                    "relationship_tier": edge["tier"],
                    "witnesses": [
                        {
                            "side": side,
                            "namespace": "architecture",
                            "conditions": edge["conditions"],
                            "steps": [edge],
                        }
                    ],
                }
            )
    return changes, findings, gaps
