"""Conformance fixtures for the parse-only TS/TSX/JS declaration extractor (Stage 13)."""

from __future__ import annotations

from uuid import UUID

import pytest

from buildlens.domain import extractors
from buildlens.domain.structural import Symbol
from buildlens.infrastructure.structural import typescript
from buildlens.infrastructure.structural.typescript import extract, verify_canary


def _symbols(locator: str, source: str) -> dict[str, Symbol]:
    result = extract(locator, source.encode("utf-8"))
    assert result.outcome == "parsed"
    return {symbol.qualified_name: symbol for symbol in result.symbols}


def _facts(locator: str, source: str) -> dict[str, dict[str, object]]:
    result = extract(locator, source.encode("utf-8"))
    names = {symbol.id: symbol.qualified_name for symbol in result.symbols}
    return {names[item.symbol_id]: item.facts for item in result.extraction.symbol_facts}


def test_declarations_kinds_spaces_and_qualified_names() -> None:
    symbols = _symbols(
        "a.ts",
        "export class Store<T> { load(): T { return null!; } static s = () => 1; }\n"
        "interface Shape { a: string }\n"
        "type Alias<T> = T[];\n"
        "const enum Mode { A }\n"
        "namespace Outer { export function inner() {} }\n"
        "export abstract class Base { abstract run(): void; }\n",
    )
    assert {name: (s.kind, s.declaration_space) for name, s in symbols.items()} == {
        "Store": ("class", "value"),
        "Store.load": ("method", "value"),
        "Store.s": ("method", "value"),
        "Shape": ("interface", "type"),
        "Alias": ("type_alias", "type"),
        "Mode": ("enum", "value"),
        "Outer": ("namespace", "namespace"),
        "Outer.inner": ("function", "value"),
        "Base": ("class", "value"),
        "Base.run": ("method", "value"),
    }
    assert symbols["Store"].exported == "named"
    assert symbols["Store.s"].declaration_form == "field_function"
    assert symbols["Base.run"].declaration_form == "signature"


def test_adjacent_overloads_form_one_symbol_and_non_adjacent_do_not() -> None:
    result = extract(
        "o.ts",
        b"export function f(a: string): void;\n"
        b"export function f(a: number): void;\n"
        b"export function f(a: any) { return a; }\n"
        b"function g(a: string): void;\n"
        b"const between = 1;\n"
        b"function g(a: any) {}\n",
    )
    symbols = [s for s in result.symbols]
    f = [s for s in symbols if s.name == "f"]
    assert len(f) == 1 and f[0].declaration_form == "overload_group"
    assert f[0].declaration_span.start_line == 1 and f[0].declaration_span.end_line == 3
    facts = {x.symbol_id: x.facts for x in result.extraction.symbol_facts}
    assert facts[f[0].id]["signature_count"] == 2
    g = [s for s in symbols if s.name == "g"]
    assert [s.declaration_form for s in g] == ["signature", "def"]


def test_same_kind_merging_and_cross_kind_merges() -> None:
    source = (
        "interface Box { a: string }\n"
        "class Box {}\n"
        "interface Box { b: number }\n"
        "namespace NS { export const x = () => 1; }\n"
        "namespace NS { export function y() {} }\n"
    )
    symbols = _symbols("m.ts", source)
    facts = _facts("m.ts", source)
    interface = next(s for s in extract("m.ts", source.encode()).symbols if s.kind == "interface")
    assert len(interface.additional_spans) == 1
    left, right = interface.additional_spans[0]
    assert source[left:right] == "interface Box { b: number }"
    assert facts["NS"]["merge_count"] == 2
    assert {"NS.x", "NS.y"} <= set(symbols)
    assert facts["Box"]["merges_with"] in (["class"], ["interface"])


def test_anonymous_default_exports_wrappers_and_hoc() -> None:
    first = _symbols("d.tsx", "export default function () { return <div/>; }\n")
    assert first["default"].name_source == "export_default"
    assert first["default"].exported == "default"
    wrapped = extract(
        "w.tsx",
        b"export default memo(() => <span/>);\n"
        b"export const Field = forwardRef(function Field(props, ref) { return <input/>; });\n",
    )
    facts = {
        s.qualified_name: f.facts
        for s in wrapped.symbols
        for f in wrapped.extraction.symbol_facts
        if f.symbol_id == s.id
    }
    assert facts["default"]["wrapper"] == "memo" and facts["default"]["returns_jsx"] is True
    assert facts["Field"]["wrapper"] == "forwardRef"
    hoc = extract(
        "h.tsx", b"function Page() { return <main/>; }\nexport default withRouter(Page);\n"
    )
    assert [s.name for s in hoc.symbols] == ["Page"]
    exports = [(e.kind, e.name, e.local_name) for e in hoc.extraction.exports]
    assert ("default", "default", "Page") in exports


def test_imports_exports_and_commonjs() -> None:
    result = extract(
        "i.ts",
        b'import React, { useState as uS, type X } from "react";\n'
        b'import * as ns from "./ns";\n'
        b'import "./side";\n'
        b'import type { T } from "./t";\n'
        b'import fs = require("fs");\n'
        b'export { a as b } from "./c";\n'
        b'export * from "./d";\n'
        b"export const value = 1;\n"
        b'function lazy() { return import("./lazy"); }\n',
    )
    imports = {(i.form, i.from_module, i.name, i.alias) for i in result.imports}
    assert imports == {
        ("es_import", "react", "default", "React"),
        ("es_import", "react", "useState", "uS"),
        ("es_import_type", "react", "X", None),
        ("es_import", "./ns", "*", "ns"),
        ("es_import", "./side", "", None),
        ("es_import_type", "./t", "T", None),
        ("require", "fs", "*", "fs"),
        ("dynamic_import", "./lazy", "*", None),
    }
    exports = {(e.kind, e.name, e.source_module) for e in result.extraction.exports}
    assert {("reexport", "b", "./c"), ("reexport_all", "*", "./d"), ("named", "value", None)} <= (
        exports
    )
    commonjs = extract(
        "c.js", b'const x = require("x");\nmodule.exports = { x };\nexports.y = 2;\n'
    )
    assert {(e.kind, e.name) for e in commonjs.extraction.exports} == {
        ("commonjs", "module.exports"),
        ("commonjs", "y"),
    }
    assert [(i.form, i.from_module) for i in commonjs.imports] == [("require", "x")]


def test_references_hooks_jsx_and_raise_are_owned_by_declarations() -> None:
    result = extract(
        "r.jsx",
        b"class View extends Base {\n"
        b"  render() { const [v] = useState(0); helper(v); return <Panel item={v}/>; }\n"
        b"}\n"
        b'function fail() { throw new TypeError("x"); }\n',
    )
    owners: dict[UUID | None, str] = {s.id: s.qualified_name for s in result.symbols}
    references = {
        (r.kind, r.target, owners.get(r.owner_symbol_id)) for r in result.extraction.references
    }
    assert {
        ("base", "Base", "View"),
        ("hook_call", "useState", "View.render"),
        ("call", "helper", "View.render"),
        ("jsx_element", "Panel", "View.render"),
        ("raise", "TypeError", "fail"),
    } <= references


def test_ambient_declarations_and_declaration_files() -> None:
    result = extract("types.d.ts", b'declare module "m" { export function g(): void; }\n')
    facts = {
        s.qualified_name: f.facts
        for s in result.symbols
        for f in result.extraction.symbol_facts
        if f.symbol_id == s.id
    }
    assert facts["m"]["ambient"] is True and facts["m.g"]["ambient"] is True
    assert result.extraction.artifact_facts is not None
    assert result.extraction.artifact_facts["declaration_file"] is True


def test_partial_parse_drops_erroneous_top_level_statements_only() -> None:
    source = (
        "export function before() {}\n"
        "class Broken { method() { const x = ( } }\n"
        "export function after() {}\n"
    )
    result = extract("p.ts", source.encode())
    assert result.outcome == "parsed"
    assert result.extraction.outcome == "partial"
    names = [s.qualified_name for s in result.symbols]
    assert names == ["before", "after"]  # G1: nothing from inside the broken statement
    assert result.extraction.artifact_facts is not None
    assert result.extraction.artifact_facts["dropped_line_ranges"] == [[2, 2]]
    assert [d.code for d in result.diagnostics] == ["SYNTAX_RECOVERED"]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_offsets_are_decoded_unicode_with_bom_newlines_and_astral(newline: str, bom: bytes) -> None:
    body = newline.join(
        ["// café \U0001f600", "export function afteré() { return '\U0001f600'; }", ""]
    )
    result = extract("u.ts", bom + body.encode("utf-8"))
    symbol = result.symbols[0]
    assert symbol.start_offset is not None and symbol.end_offset is not None
    assert body[symbol.start_offset : symbol.end_offset] == (
        "export function afteré() { return '\U0001f600'; }"
    )
    assert result.encoding == "utf-8-sig"
    comment = result.extraction.artifact_facts["comment_ranges"][0]  # type: ignore[index]
    assert body[comment[0] : comment[1]] == "// café \U0001f600"


def test_rejected_and_undecodable_sources_never_echo_content() -> None:
    result = extract("bad.ts", b"const x = '\xff';")
    assert result.outcome == "failed"
    assert result.diagnostics[0].code == "ENCODING_ERROR"
    assert "\xff" not in result.diagnostics[0].message


def test_canary_matches_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert verify_canary() is None
    monkeypatch.setattr(typescript, "TREE_SITTER_CANARY_DIGEST", "0" * 64)
    assert verify_canary() == "GRAMMAR_CANARY_MISMATCH"
    # The recorded canary is part of the extractor contract, so a mismatch is never silent.
    assert extractors.extractor_contract(extractors.TYPESCRIPT_DECLARATIONS) is not None


def test_state_fingerprint_ignores_comments_and_whitespace_but_not_code() -> None:
    base = extract("s.ts", b"function f(a: number) { return a + 1; }\n").symbols[0]
    spaced = extract("s.ts", b"function f(a: number) {\n  // note\n  return a + 1;\n}\n").symbols[0]
    changed = extract("s.ts", b"function f(a: number) { return a + 2; }\n").symbols[0]
    assert base.state_fingerprint == spaced.state_fingerprint
    assert base.state_fingerprint != changed.state_fingerprint


def test_long_lines_do_not_corrupt_the_heap() -> None:
    """Regression: py-tree-sitter 0.26.0 ``Point`` integers above 256 outlived their owner.

    Runs in a subprocess so a native crash fails this test instead of the whole session.
    """
    import subprocess
    import sys
    from pathlib import Path

    script = (
        "import gc\n"
        "from buildlens.infrastructure.structural.typescript import extract\n"
        "line = 'export type Wide = { ' + ' '.join(f'f{i}: string;' for i in range(80)) + ' }\\n'\n"
        "data = (line * 20 + 'export const g = (a: number) => a;\\n').encode()\n"
        "for _ in range(40):\n"
        "    result = extract('wide.ts', data)\n"
        "    gc.collect()\n"
        "    junk = [object() for _ in range(50000)]\n"
        "assert result.symbols[-1].declaration_span.start_column == 0\n"
        "print('ok')\n"
    )
    source_root = Path(__file__).resolve().parents[2] / "src"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(source_root)},
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip() == "ok"


def test_extractor_never_reads_tree_sitter_points() -> None:
    from pathlib import Path

    source = Path(typescript.__file__).read_text()
    code = [line for line in source.splitlines() if not line.strip().startswith(("#", '"', "``"))]
    assert not any(".start_point" in line or ".end_point" in line for line in code)
