"""Python facts pass and TS/JS declaration chunking (Stage 13 Milestone A)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from buildlens.application.processors import declaration_chunks, process_source
from buildlens.domain.source import ContentRejectedError, decode_source
from buildlens.domain.structural import ArtifactAnalysis, ArtifactExtraction, SourceSpan, Symbol
from buildlens.infrastructure.structural.parser import PythonAstParser
from buildlens.infrastructure.structural.python_facts import extract_python_facts
from buildlens.infrastructure.structural.typescript import extract

PYTHON = '''"""Module docstring."""
# café comment
from sqlalchemy.orm import mapped_column

__all__ = ["Account", "load"]


class Account(Base, metaclass=Meta):
    """An account."""

    __tablename__ = "accounts"
    id = mapped_column(Integer, primary_key=True)

    @property
    def label(self) -> str:
        return format_label(self.id)


async def load(session):
    row = await session.get(Account, 1)
    if row is None:
        raise LookupError("missing")
    return getattr(row, "label")


if __name__ == "__main__":
    load(None)
'''


def _python() -> tuple[ArtifactAnalysis, ArtifactExtraction]:
    data = PYTHON.encode()
    parsed = PythonAstParser().parse(
        analysis_id=uuid4(), version_id=uuid4(), artifact_id=uuid4(), locator="m.py", data=data
    )
    return parsed, extract_python_facts(data, parsed.symbols)


def test_python_facts_are_attached_to_parser_declarations() -> None:
    parsed, extraction = _python()
    assert extraction.outcome == "extracted"
    names: dict[UUID | None, str] = {s.id: s.qualified_name for s in parsed.symbols}
    facts: dict[str, Any] = {names[f.symbol_id]: f.facts for f in extraction.symbol_facts}
    assert facts["Account"]["bases"] == ["Base"]
    assert facts["Account"]["keywords"] == ["metaclass=Meta"]
    assignments = {a["name"]: a["value"] for a in facts["Account"]["class_assignments"]}
    assert assignments["__tablename__"] == "'accounts'"
    assert assignments["id"] == "mapped_column(Integer, primary_key=True)"
    assert facts["load"]["awaits"] is True and facts["load"]["returns_value"] is True
    assert facts["load"]["dynamic_features"] == ["getattr"]
    references = {(r.kind, r.target, names.get(r.owner_symbol_id)) for r in extraction.references}
    assert {
        ("base", "Base", "Account"),
        ("decorator", "property", "Account.label"),
        ("call", "format_label", "Account.label"),
        ("call", "session.get", "load"),
        ("raise", "LookupError", "load"),
    } <= references
    assert [(e.kind, e.name) for e in extraction.exports] == [
        ("dunder_all", "Account"),
        ("dunder_all", "load"),
    ]


def test_python_fact_ranges_are_decoded_offsets() -> None:
    _, extraction = _python()
    facts: Any = extraction.artifact_facts
    assert facts is not None and facts["main_guard"] is True
    left, right = facts["comment_ranges"][0]
    assert PYTHON[left:right] == "# café comment"
    left, right = facts["module_docstring_range"]
    assert PYTHON[left:right] == '"""Module docstring."""'
    assert facts["unmatched_declarations"] == 0


def test_python_facts_fail_without_echoing_source() -> None:
    extraction = extract_python_facts(b"def broken(:\n", ())
    assert extraction.outcome == "failed"
    assert extraction.diagnostics[0].code == "SYNTAX_ERROR"


def _identified(symbols: tuple[Symbol, ...]) -> tuple[Symbol, ...]:
    return tuple(
        replace(s, stable_identity_id=uuid5(NAMESPACE_DNS, f"{s.qualified_name}|{s.ordinal}"))
        for s in symbols
    )


@pytest.mark.parametrize(
    "locator",
    ["a.ts", "a.tsx", "a.js"],
)
def test_declaration_chunks_cover_all_text_without_overlap(locator: str) -> None:
    text = (
        "﻿import x from 'x';\n"
        "// café \U0001f600\n"
        "export function f(a) { return a; }\n"
        "class C { m() { const inner = () => 1; return inner(); } }\n"
        "const broken = (;\n"
        "export const later = () => 2;\n"
    )
    data = text.encode()
    result = extract(locator, data)
    source, _ = decode_source(data)
    suffix = locator.rsplit(".", 1)[1]
    chunks, diagnostics, contract = process_source(
        source, suffix, uuid4(), "anchor", _identified(result.symbols), 1024, 5
    )
    assert diagnostics == ()
    assert contract.processor == ("javascript" if suffix == "js" else "typescript")
    cursor = 0
    for chunk in chunks:
        assert chunk.span.start_offset >= cursor
        assert not source[cursor : chunk.span.start_offset].strip()
        assert source[chunk.span.start_offset : chunk.span.end_offset] == chunk.content
        cursor = chunk.span.end_offset
    assert not source[cursor:].strip()
    bound = {c.symbol_identity_id for c in chunks if c.symbol_identity_id}
    assert len(bound) == len(result.symbols)
    module = [c for c in chunks if c.symbol_identity_id is None]
    assert any("const broken" in c.content for c in module)  # G5: dropped text stays evidence


def test_declaration_chunk_keys_are_stable_when_lines_shift() -> None:
    first = "export function f() { return 1; }\n"
    second = "\n\n// header\n" + first
    keys = []
    for text in (first, second):
        result = extract("k.ts", text.encode())
        chunks, _, _ = process_source(
            text, "ts", uuid4(), "anchor", _identified(result.symbols), 1024, 5
        )
        keys.append({c.stable_key for c in chunks if c.symbol_identity_id})
    assert keys[0] == keys[1]


def test_partial_region_overlap_is_a_span_conflict() -> None:
    def symbol(name: str, start: int, end: int) -> Symbol:
        return Symbol(
            uuid4(),
            uuid4(),
            None,
            0,
            "function",
            name,
            name,
            False,
            SourceSpan(1, 1),
            1,
            None,
            stable_identity_id=uuid4(),
            start_offset=start,
            end_offset=end,
        )

    text = "function a() {}\nfunction b() {}\n"
    with pytest.raises(ContentRejectedError, match="SPAN_CONFLICT"):
        declaration_chunks(
            text, uuid4(), "anchor", (symbol("a", 0, 20), symbol("b", 10, 30)), "typescript"
        )
