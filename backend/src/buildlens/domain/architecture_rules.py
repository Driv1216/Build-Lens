"""Stage 14 Milestone C owner-declared architecture rules (``architecture-rules.v1``).

Declared intent is its own epistemic axis (C-4): a rule set carries no evidence tier, is
supplied only by the owner through the API or CLI (never read from repository files), is
stored as immutable, append-only versions, and never alters the graph. Two rule types exist:

* ``forbid``: no module matched by ``from`` may depend on a module matched by ``to``;
* ``layers``: an ordered list of layers; a module of layer *i* may depend only on modules of
  the same or a later layer.

Selectors are exact node keys (``module:``, ``container:``, ``workspace:``) or globs over
module locators with ``*`` (within one segment), ``**`` (any segments) and ``?`` (one
character). There are no character classes and no regular expressions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from buildlens.domain.architecture import CLASS_SETS, IMPORT_CLASSES
from buildlens.domain.source import digest

RULES_VERSION = "architecture-rules.v1"
MAX_RULES = 200
MAX_SELECTORS = 50
MAX_LAYERS = 12
MAX_WITNESSES = 50
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:-]{0,79}$")
_KEY = re.compile(r"^(module|container|workspace):[^\x00-\x1f]{0,1024}$")
_GLOB = re.compile(r"^[A-Za-z0-9_.@/*?-]{1,512}$")

ConformanceOutcome = Literal[
    "not_satisfied", "satisfied_in_observed_graph", "not_determinable", "selector_matches_nothing"
]


class RuleValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    kind: Literal["forbid", "layers"]
    classes: tuple[str, ...]
    sources: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    layers: tuple[tuple[str, ...], ...] = ()

    def as_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "classes": list(self.classes),
        }
        if self.kind == "forbid":
            value |= {"from": list(self.sources), "to": list(self.targets)}
        else:
            value["layers"] = [list(layer) for layer in self.layers]
        return value


@dataclass(frozen=True, slots=True)
class RuleSet:
    """One immutable, owner-declared version. Intent has no evidence tier."""

    id: UUID
    project_id: UUID
    version: int
    rules: tuple[Rule, ...]
    digest: str
    created_at: datetime
    contract: str = RULES_VERSION


def _selectors(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_SELECTORS:
        raise RuleValidationError(f"'{field}' must be a non-empty list of at most {MAX_SELECTORS}.")
    values = []
    for item in value:
        if not isinstance(item, str) or not (_KEY.match(item) or _GLOB.match(item)):
            raise RuleValidationError(
                f"Selector {item!r} must be a node key or a glob using only *, ** and ?."
            )
        if ".." in item.split("/"):
            raise RuleValidationError(f"Selector {item!r} must not contain '..'.")
        values.append(item)
    return tuple(values)


def _classes(value: Any) -> tuple[str, ...]:
    if value is None:
        return tuple(sorted(CLASS_SETS["S2"]))
    if isinstance(value, str) and value in CLASS_SETS:
        return tuple(sorted(CLASS_SETS[value]))
    if isinstance(value, list) and value and all(item in IMPORT_CLASSES for item in value):
        return tuple(sorted(set(value)))
    raise RuleValidationError("'classes' must be a class set (S0-S4) or a list of import classes.")


def validate_rules(payload: Any) -> tuple[Rule, ...]:
    """Strict validation: unknown fields, duplicate names and regex-like selectors fail."""
    if not isinstance(payload, list) or len(payload) > MAX_RULES:
        raise RuleValidationError(f"Rules must be a list of at most {MAX_RULES} items.")
    rules: list[Rule] = []
    names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise RuleValidationError("Each rule must be an object.")
        kind = item.get("kind")
        allowed = {"name", "kind", "classes"} | (
            {"from", "to"} if kind == "forbid" else {"layers"} if kind == "layers" else set()
        )
        extra = set(item) - allowed
        if kind not in {"forbid", "layers"}:
            raise RuleValidationError("'kind' must be 'forbid' or 'layers'.")
        if extra:
            raise RuleValidationError(f"Unknown rule fields: {sorted(extra)}.")
        name = item.get("name")
        if not isinstance(name, str) or not _NAME.match(name) or name in names:
            raise RuleValidationError("Each rule needs a unique, plain 'name'.")
        names.add(name)
        classes = _classes(item.get("classes"))
        if kind == "forbid":
            rules.append(
                Rule(
                    name,
                    "forbid",
                    classes,
                    _selectors(item.get("from"), "from"),
                    _selectors(item.get("to"), "to"),
                )
            )
        else:
            layers = item.get("layers")
            if not isinstance(layers, list) or not 2 <= len(layers) <= MAX_LAYERS:
                raise RuleValidationError(f"'layers' needs 2 to {MAX_LAYERS} layers.")
            rules.append(
                Rule(
                    name,
                    "layers",
                    classes,
                    layers=tuple(_selectors(layer, "layers[]") for layer in layers),
                )
            )
    return tuple(rules)


def rules_digest(rules: tuple[Rule, ...]) -> str:
    return digest({"contract": RULES_VERSION, "rules": [rule.as_json() for rule in rules]})


def glob_expression(pattern: str) -> re.Pattern[str]:
    """Translate the restricted glob into an anchored expression (no user regex)."""
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(parts) + "$")
