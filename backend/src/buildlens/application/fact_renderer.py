"""Versioned deterministic renderer for catalog facts (``fact-renderer.v1``).

``render`` is a pure function of the cited structural rows (in ``source_refs`` order), their
reference descriptors and the fact's recorded derivation. The catalog build and read-time
``F#`` verification call it with rows loaded by the same normaliser, so a changed or tampered
row changes the digest. The text never contains database ids, keeping it portable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from buildlens.domain.semantic import RENDERER_VERSION, Row, fact_digest

TEXT_LIMIT = 2048
_LIMITATIONS = {
    "PARTIAL_PARSE_ARTIFACT": "The file had syntax errors; erroneous top-level statements were "
    "not extracted",
    "FACTS_NOT_EXTRACTED": "Syntax facts (references, bases, docstrings) were not extracted for "
    "this artifact",
    "FACTS_EXTRACTION_FAILED": "The syntax-facts pass failed for this artifact",
    "FACTS_CONTRACT_CHANGED": "Syntax facts were produced by a different extractor contract and "
    "are not used",
    "EXTRACTOR_CONTRACT_CHANGED": "Declarations were produced by a different extractor contract "
    "and are not current",
    "STRUCTURE_FAILED": "The file could not be parsed",
    "REFERENCES_TRUNCATED": "Reference extraction reached its cap; some references are missing",
    "DYNAMIC_FEATURES": "The declaration uses dynamic features that static facts cannot follow",
    "AMBIENT_DECLARATION": "The declaration is ambient (type-level only; no runtime body)",
    "MIXED_SCRIPT_IDENTIFIER": "The name mixes letters from different scripts (possible homoglyph)",
    "UNRESOLVED_ALIAS_IMPORTS": "Some imports use path aliases that are not resolved",
}


def _clip(value: str) -> str:
    return value if len(value) <= TEXT_LIMIT else value[: TEXT_LIMIT - 1] + "…"


def _lines(row: Row) -> str:
    start, end = row.get("start_line"), row.get("end_line")
    if start is None:
        return ""
    return f"line {start}" if start == end or end is None else f"lines {start}–{end}"


def _expression(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    if text is None:
        return "…" if value.get("omitted") else None
    return f"{text}…" if value.get("truncated") else str(text)


def _python_parameters(signature: Any) -> str:
    if not isinstance(signature, dict):
        return "…"
    parts: list[str] = []
    marker_done = False
    previous = None
    for item in signature.get("parameters", []):
        kind = item.get("kind")
        if previous == "positional_only" and kind != "positional_only":
            parts.append("/")
        if kind == "keyword_only" and not marker_done:
            parts.append("*")
            marker_done = True
        prefix = (
            "*" if kind == "variadic_positional" else "**" if kind == "variadic_keyword" else ""
        )
        if kind == "variadic_positional":
            marker_done = True
        text = f"{prefix}{item.get('name')}"
        annotation = _expression(item.get("annotation"))
        if annotation:
            text += f": {annotation}"
        if item.get("has_default"):
            default = _expression(item.get("default")) or "…"
            text += f" = {default}"
        parts.append(text)
        previous = kind
    if previous == "positional_only":
        parts.append("/")
    return ", ".join(parts)


def _decorators(symbol: Row) -> str:
    values = [
        text
        for item in symbol.get("decorators") or []
        if (text := _expression(item.get("expression")))
    ]
    return " ".join(f"@{value}" for value in values)


def _python_signature(symbol: Row, facts: Row | None) -> str:
    kind = symbol["kind"]
    name = symbol["name"]
    if kind == "class":
        bases = list((facts or {}).get("bases") or []) + list((facts or {}).get("keywords") or [])
        head = f"class {name}({', '.join(bases)})" if bases else f"class {name}"
    else:
        signature = symbol.get("signature")
        keyword = "async def" if symbol.get("is_async") else "def"
        head = f"{keyword} {name}({_python_parameters(signature)})"
        returns = _expression(signature.get("return_annotation")) if signature else None
        if returns:
            head += f" -> {returns}"
    decorators = _decorators(symbol)
    return f"{decorators} {head}" if decorators else head


def _ts_signature(symbol: Row, facts: Row | None) -> str:
    kind, name, form = symbol["kind"], symbol["name"], symbol.get("declaration_form")
    facts = facts or {}
    generics = [str(item.get("text")) for item in symbol.get("type_parameters") or []]
    type_parameters = f"<{', '.join(generics)}>" if generics else ""
    keywords: list[str] = []
    if symbol.get("exported") == "default":
        keywords.append("export default")
    elif symbol.get("exported"):
        keywords.append("export")
    if facts.get("ambient"):
        keywords.append("declare")
    for flag in ("abstract", "static"):
        if facts.get(flag):
            keywords.append(flag)
    if symbol.get("is_async"):
        keywords.append("async")
    if kind in {"function", "method"}:
        parameters = ", ".join(
            f"{item.get('name')}{'?' if item.get('optional') else ''}"
            + (f": {item.get('type')}" if item.get("type") else "")
            for item in facts.get("parameters") or []
        )
        returns = f": {facts['return_type']}" if facts.get("return_type") else ""
        star = "*" if facts.get("generator") else ""
        accessor = f"{facts['accessor']} " if facts.get("accessor") else ""
        if form in {"variable_function", "field_function"}:
            wrapper = facts.get("wrapper")
            arrow = f"({parameters}){returns} => …"
            value = f"{wrapper}({arrow})" if wrapper else arrow
            head = f"{'const ' if form == 'variable_function' else ''}{name} = {value}"
        elif kind == "method":
            head = f"{accessor}{name}{star}{type_parameters}({parameters}){returns}"
        else:
            head = f"function{star} {name}{type_parameters}({parameters}){returns}"
    else:
        word = {
            "class": "class",
            "interface": "interface",
            "type_alias": "type",
            "enum": "enum",
            "namespace": "namespace",
        }[kind]
        head = f"{word} {name}{type_parameters}"
    extra: list[str] = []
    if facts.get("signature_count"):
        extra.append(f"{facts['signature_count']} overload signatures")
    if facts.get("merge_count"):
        extra.append(f"{facts['merge_count']} merged declarations")
    if facts.get("merges_with"):
        extra.append("also declared as " + ", ".join(facts["merges_with"]))
    decorators = _decorators(symbol)
    text = " ".join([*keywords, head])
    if decorators:
        text = f"{decorators} {text}"
    return text + (f" ({'; '.join(extra)})" if extra else "")


def _signature(refs: Sequence[Row], rows: Sequence[Row], derived: Row) -> str:
    symbol = rows[0]
    facts = rows[1].get("facts") if len(rows) > 1 else None
    python = derived.get("language") in {None, "Python"}
    code = _python_signature(symbol, facts) if python else _ts_signature(symbol, facts)
    label = str(symbol["kind"]).replace("_", " ")
    return f"{label} {symbol['qualified_name']} at {refs[0]['locator']} {_lines(symbol)}: {code}"


def _import(refs: Sequence[Row], rows: Sequence[Row], derived: Row) -> str:
    row = rows[0]
    form, name, alias, module = row["form"], row["name"], row.get("alias"), row.get("from_module")
    as_alias = f" as {alias}" if alias else ""
    if form == "import":
        code = f"import {name}{as_alias}"
    elif form == "from":
        code = f"from {'.' * int(row.get('level') or 0)}{module or ''} import {name}{as_alias}"
    elif form == "dynamic_import":
        code = f'import("{module}")'
    elif form == "require":
        code = f'require("{module}")'
    else:
        keyword = "import type" if form == "es_import_type" else "import"
        if not name:
            code = f'{keyword} "{module}"'
        elif name == "*":
            code = f'{keyword} * as {alias} from "{module}"'
        elif name == "default":
            code = f'{keyword} {alias} from "{module}"'
        else:
            code = f'{keyword} {{ {name}{as_alias} }} from "{module}"'
    return f"{refs[0]['locator']} {_lines(row)}: {code}"


def _export(refs: Sequence[Row], rows: Sequence[Row], derived: Row) -> str:
    row = rows[0]
    kind = row["kind"]
    local = row.get("local_name")
    if kind == "dunder_all":
        code = f"__all__ lists {row['name']!r}"
    elif kind == "default":
        code = f"export default {local or '(expression)'}"
    elif kind == "reexport":
        code = f'export {{ {local} as {row["name"]} }} from "{row.get("source_module")}"'
    elif kind == "reexport_all":
        alias = row["name"]
        target = f"* as {alias}" if alias != "*" else "*"
        code = f'export {target} from "{row.get("source_module")}"'
    elif kind == "commonjs":
        code = f"CommonJS export {row['name']}"
    else:
        code = (
            f"export {{ {local} as {row['name']} }}"
            if local and local != row["name"]
            else f"export {row['name']}"
        )
    return f"{refs[0]['locator']} {_lines(row)}: {code}"


def _reference(refs: Sequence[Row], rows: Sequence[Row], derived: Row) -> str:
    owner = rows[0]
    groups: dict[str, list[str]] = {}
    for row in rows[1:]:
        values = groups.setdefault(str(row["kind"]), [])
        if row["target"] not in values:
            values.append(str(row["target"]))
    labels = {
        "call": "calls",
        "raise": "raises",
        "jsx_element": "renders",
        "hook_call": "uses hooks",
        "type_ref": "type references",
        "decorator": "decorated by",
        "base": "bases",
    }
    parts = [f"{labels.get(kind, kind)} {', '.join(values)}" for kind, values in groups.items()]
    omitted = int(derived.get("omitted") or 0)
    suffix = f"; {omitted} further references omitted" if omitted else ""
    return f"{owner['qualified_name']} (syntax only, unresolved): {'; '.join(parts)}{suffix}"


def _base(refs: Sequence[Row], rows: Sequence[Row], derived: Row) -> str:
    owner = rows[0]
    bases = [str(row["target"]) for row in rows[1:]]
    return f"{owner['qualified_name']} inherits from {', '.join(bases)}"


def _name(row: Row, ref: Row) -> str:
    if ref["table"] == "symbols":
        return f"{row['qualified_name']} ({ref['locator']})"
    return str(ref["locator"])


def _relation(refs: Sequence[Row], rows: Sequence[Row], derived: Row) -> str:
    layout = derived["layout"]
    source = _name(rows[layout["source"]], refs[layout["source"]])
    target_index = layout.get("target")
    target = (
        _name(rows[target_index], refs[target_index])
        if target_index is not None
        else str(derived.get("target_text"))
    )
    verbs = {
        "contains": "contains",
        "imports": "imports",
        "exports": "exports",
        "references": "references",
        "renders": "renders",
        "uses_hook": "uses hook",
        "inherits": "inherits from",
        "defines_route": "defines route",
        "maps_table": "maps table",
    }
    evidence = [_lines(rows[index]) for index in layout.get("evidence", []) if _lines(rows[index])][
        :4
    ]
    text = f"{source} {verbs[derived['relation']]} {target}"
    if derived.get("resolution"):
        text += f" [{derived['resolution']}]"
    if derived.get("count", 1) > 1:
        text += f" ×{derived['count']}"
    return text + (f" (evidence at {', '.join(evidence)})" if evidence else "")


def _recognizer(refs: Sequence[Row], rows: Sequence[Row], derived: Row) -> str:
    strength = {
        "pattern_with_local_evidence": "pattern with local evidence",
        "pattern_only": "pattern only",
    }[derived["strength"]]
    subject = rows[0]
    label = subject.get("qualified_name") or refs[0]["locator"]
    attributes = derived.get("attributes") or {}
    detail = ", ".join(f"{key}={value}" for key, value in sorted(attributes.items()) if value)
    met = ", ".join(derived.get("met") or []) or "none"
    unmet = ", ".join(derived.get("unmet") or []) or "none"
    head = f"{derived['recognizer']} ({strength}): {label} at {refs[0]['locator']}"
    return f"{head}{f' [{detail}]' if detail else ''}; conditions met: {met}; unmet: {unmet}"


def _limitation(refs: Sequence[Row], rows: Sequence[Row], derived: Row) -> str:
    code = derived["code"]
    base = _LIMITATIONS.get(code, code)
    row = rows[0]
    detail = ""
    if code == "PARTIAL_PARSE_ARTIFACT":
        ranges = (row.get("artifact_facts") or {}).get("dropped_line_ranges") or []
        detail = ": dropped " + ", ".join(f"lines {a}–{b}" for a, b in ranges[:8])
    elif code == "MIXED_SCRIPT_IDENTIFIER":
        detail = ": " + ascii(str(row.get("name")))
    elif code == "DYNAMIC_FEATURES":
        detail = ": " + ", ".join((row.get("facts") or {}).get("dynamic_features") or [])
    elif code == "REFERENCES_TRUNCATED":
        detail = f": {(row.get('artifact_facts') or {}).get('truncated_references')} omitted"
    return f"{refs[0]['locator']}: {base}{detail}"


_RENDERERS = {
    "signature": _signature,
    "import": _import,
    "export": _export,
    "reference": _reference,
    "base": _base,
    "relation": _relation,
    "recognizer_match": _recognizer,
    "limitation": _limitation,
}


def render(kind: str, refs: Sequence[Row], rows: Sequence[Row], derived: Row) -> str:
    if len(refs) != len(rows) or not refs:
        raise ValueError("A fact renders from at least one cited row.")
    return _clip(_RENDERERS[kind](refs, rows, derived))


def rendered_fact(kind: str, refs: list[Row], rows: Sequence[Row], derived: Row) -> tuple[str, str]:
    """(rendered_text, fact_digest) for the current renderer version."""
    text = render(kind, refs, rows, derived)
    return text, fact_digest(RENDERER_VERSION, text, refs, derived)
