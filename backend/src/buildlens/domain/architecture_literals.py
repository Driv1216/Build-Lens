"""Stage 14 literal evidence rows: vocabulary, limits and sanitisation (pure).

Literal rows are syntactic observations made by the additive ``*-architecture-literals.v1``
passes inside the supervised worker. Nothing is evaluated, resolved or executed. Every stored
value is sanitised here before it leaves the worker:

* URLs keep only scheme, host, port and path; userinfo, query and fragment are dropped entirely
  (keys included), and the host is lower-cased;
* expression placeholders are kept only when they are an identifier, a member chain or a short
  call of an identifier, otherwise ``<expr>``;
* static parts are bounded, values with control or bidirectional-format characters are dropped,
  and every value passes the existing secret screen.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from buildlens.domain.source import ContentRejectedError, screen_content

LiteralKind = Literal[
    "app_or_router",
    "router_mount",
    "migration_table",
    "http_call",
    "url_call",
    "import_guard",
    "type_import",
    "url_constant",
    "url_helper",
    "frontend_route",
    "opaque_dynamic_import",
]
LITERAL_KINDS: tuple[LiteralKind, ...] = (
    "app_or_router",
    "router_mount",
    "migration_table",
    "http_call",
    "url_call",
    "import_guard",
    "type_import",
    "url_constant",
    "url_helper",
    "frontend_route",
    "opaque_dynamic_import",
)
# Classification rows are bounded by the import count, not by the evidence-row cap.
CLASSIFICATION_KINDS = frozenset({"import_guard", "type_import"})

ROWS_PER_ARTIFACT = 256
CLASSIFICATION_ROWS_PER_ARTIFACT = 4096
ROWS_PER_RUN = 50_000
STATIC_LIMIT = 256
PLACEHOLDER_LIMIT = 128
PARTS_LIMIT = 16
SUBJECT_LIMIT = 256
OPAQUE = "<expr>"

_CHAIN = r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*"
_ARGUMENT = r"(?:" + _CHAIN + r"|-?\d+(?:\.\d+)?|'[^'\\]{0,32}'|\"[^\"\\]{0,32}\")"
# An identifier or member chain, or a call of one with at most four identifier/literal arguments.
_PLACEHOLDER = re.compile(
    r"^(?:"
    + _CHAIN
    + r"|"
    + _CHAIN
    + r"\((?:\s*"
    + _ARGUMENT
    + r"(?:\s*,\s*"
    + _ARGUMENT
    + r"){0,3}\s*)?\))$"
)
_IDENTIFIER_CHAIN = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
_ABSOLUTE = re.compile(r"^(https?):\/\/", re.IGNORECASE)


class LiteralRejected(Exception):
    """A value failed sanitisation; it is counted and never stored."""


def _unsafe(value: str) -> bool:
    return any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Co"} for c in value)


def clean(value: str, limit: int = STATIC_LIMIT) -> tuple[str, bool]:
    """Return a storable value and whether it was truncated; raise when it must be dropped."""
    if _unsafe(value):
        raise LiteralRejected("CONTROL_OR_FORMAT_CHARACTER")
    try:
        screen_content(value)
    except ContentRejectedError as error:
        raise LiteralRejected(error.code) from None
    if len(value) > limit:
        return value[:limit], True
    return value, False


def identifier_chain(value: str | None) -> str | None:
    """A dotted identifier chain, or ``None`` (never free text)."""
    if value is None or len(value) > SUBJECT_LIMIT or not _IDENTIFIER_CHAIN.match(value):
        return None
    return value


def placeholder(text: str | None) -> str:
    """An identifier, member chain or short identifier call; anything else is opaque."""
    if text is None:
        return OPAQUE
    compact = " ".join(text.split())
    if len(compact) > PLACEHOLDER_LIMIT or _unsafe(compact) or not _PLACEHOLDER.match(compact):
        return OPAQUE
    try:
        screen_content(compact)
    except ContentRejectedError:
        return OPAQUE
    return compact


Part = tuple[Literal["s", "e"], str]


@dataclass(slots=True)
class Counters:
    sanitized_out: int = 0
    truncated: int = 0
    dropped_rows: int = 0
    codes: dict[str, int] = field(default_factory=dict)

    def reject(self, code: str) -> None:
        self.sanitized_out += 1
        self.codes[code] = self.codes.get(code, 0) + 1


def url_template(parts: list[Part]) -> dict[str, object]:
    """Sanitise one string/template value into origin + path parts.

    Raises ``LiteralRejected`` when any static part is unsafe; the caller counts it.
    """
    merged: list[Part] = []
    for kind, value in parts:
        if kind == "s" and merged and merged[-1][0] == "s":
            merged[-1] = ("s", merged[-1][1] + value)
        elif kind == "s" and not value:
            continue
        else:
            merged.append((kind, value))
    truncated = False
    query_dropped = False
    kept: list[Part] = []
    for kind, value in merged:
        if kind == "s":
            cut = min((i for i in (value.find("?"), value.find("#")) if i >= 0), default=-1)
            if cut >= 0:
                value = value[:cut]
                query_dropped = True
            if value:
                text, cut_short = clean(value)
                truncated = truncated or cut_short
                kept.append(("s", text))
            if query_dropped:
                break
        else:
            kept.append(("e", placeholder(value)))
    if len(kept) > PARTS_LIMIT:
        kept = kept[:PARTS_LIMIT]
        truncated = True
    origin: dict[str, object] = {"kind": "relative_or_unknown"}
    if kept and kept[0][0] == "s":
        head = kept[0][1]
        match = _ABSOLUTE.match(head)
        protocol_relative = head.startswith("//")
        if match or protocol_relative:
            rest = head[match.end() :] if match else head[2:]
            slash = rest.find("/")
            authority = rest if slash < 0 else rest[:slash]
            path = "" if slash < 0 else rest[slash:]
            complete = slash >= 0 or len(kept) == 1
            authority = authority.rsplit("@", 1)[-1]
            host, port = authority, None
            if authority.startswith("["):
                end = authority.find("]")
                host = authority[: end + 1] if end >= 0 else authority
                tail = authority[end + 1 :] if end >= 0 else ""
                port = tail[1:] if tail.startswith(":") else None
            elif ":" in authority:
                host, port = authority.rsplit(":", 1)
            origin = {
                "kind": "absolute" if complete else "absolute_opaque_host",
                "scheme": match.group(1).lower() if match else None,
                "host": host.lower() if complete and host else None,
                "port": port if complete and port and port.isdigit() else None,
            }
            kept = [("s", path), *kept[1:]] if path else kept[1:]
            if not complete:
                kept = []
        elif head.startswith("/"):
            origin = {"kind": "path_absolute"}
    return {
        "origin": origin,
        "parts": [{kind: value} for kind, value in kept],
        "truncated": truncated,
        "query_dropped": query_dropped,
    }


def url_shaped(parts: list[Part]) -> bool:
    """A string/template whose leading static part looks like a path or http(s) URL."""
    statics = [value for kind, value in parts if kind == "s"]
    if not parts:
        return False
    if parts[0][0] == "s":
        head = parts[0][1]
    elif len(parts) > 1 and parts[1][0] == "s":
        head = parts[1][1]
    else:
        return False
    if any(c.isspace() for value in statics for c in value):
        return False
    return head.startswith("/") or bool(_ABSOLUTE.match(head))


def path_value(value: str) -> tuple[str, bool]:
    """A literal route path or prefix (no URL parsing)."""
    return clean(value)
