"""Stage 13 Milestone B: the pure catalog builder, resolver, recognizers and renderer."""

from __future__ import annotations

import random
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from buildlens.application import semantic_catalog
from buildlens.application.fact_renderer import rendered_fact
from buildlens.application.processors import declaration_chunks, python_chunks
from buildlens.application.recognizers import ArtifactView, recognize
from buildlens.application.semantic_catalog import (
    ArtifactInput,
    CatalogInput,
    ImportResolver,
    build_catalog,
)
from buildlens.domain.extractors import PYTHON_FACTS, extractor_contract_digest
from buildlens.domain.semantic import CatalogBuild, Row, SemanticEntity
from buildlens.domain.source import decode_source
from buildlens.domain.structural import ArtifactExtraction, ImportReference, Symbol
from buildlens.infrastructure.persistence.structural import _decorator, _expression, _signature
from buildlens.infrastructure.structural.parser import PythonAstParser
from buildlens.infrastructure.structural.python_facts import extract_python_facts
from buildlens.infrastructure.structural.typescript import extract

PRODUCER = "0" * 64


def _symbol_row(item: Symbol) -> Row:
    return {
        "id": str(item.id),
        "artifact_analysis_id": str(item.artifact_analysis_id),
        "parent_id": str(item.parent_id) if item.parent_id else None,
        "ordinal": item.ordinal,
        "kind": item.kind,
        "name": item.name,
        "qualified_name": item.qualified_name,
        "is_async": item.is_async,
        "start_line": item.declaration_span.start_line,
        "end_line": item.declaration_span.end_line,
        "full_start_line": item.full_start_line,
        "start_offset": item.start_offset,
        "end_offset": item.end_offset,
        "additional_spans": [list(span) for span in item.additional_spans],
        "signature": _signature(item.signature),
        "decorators": [_decorator(value) for value in item.decorators],
        "type_parameters": [_expression(value) for value in item.type_parameters],
        "stable_identity_id": str(item.stable_identity_id),
        "declaration_form": item.declaration_form,
        "declaration_space": item.declaration_space,
        "name_source": item.name_source,
        "exported": item.exported,
    }


def _import_row(item: ImportReference, analysis: str) -> Row:
    return {
        "id": str(item.id),
        "artifact_analysis_id": analysis,
        "owner_symbol_id": str(item.owner_symbol_id) if item.owner_symbol_id else None,
        "statement_ordinal": item.statement_ordinal,
        "item_ordinal": item.item_ordinal,
        "form": item.form,
        "from_module": item.from_module,
        "name": item.name,
        "alias": item.alias,
        "level": item.level,
        "start_line": item.span.start_line,
        "end_line": item.span.end_line,
    }


def _extraction_rows(
    extraction: ArtifactExtraction, analysis: str
) -> tuple[Row, list[Row], list[Row], list[Row]]:
    key = str(extraction.id)
    return (
        {
            "id": key,
            "artifact_analysis_id": analysis,
            "extractor": extraction.extractor,
            "outcome": extraction.outcome,
            "contract_digest": extraction.contract_digest,
            "artifact_facts": extraction.artifact_facts,
            "diagnostic_codes": [item.code for item in extraction.diagnostics],
        },
        [
            {
                "id": str(uuid4()),
                "extraction_id": key,
                "symbol_id": str(f.symbol_id),
                "facts": f.facts,
            }
            for f in extraction.symbol_facts
        ],
        [
            {
                "id": str(uuid4()),
                "extraction_id": key,
                "owner_symbol_id": str(r.owner_symbol_id) if r.owner_symbol_id else None,
                "ordinal": r.ordinal,
                "kind": r.kind,
                "target": r.target,
                "start_line": r.span.start_line,
                "end_line": r.span.end_line,
            }
            for r in extraction.references
        ],
        [
            {
                "id": str(uuid4()),
                "extraction_id": key,
                "owner_symbol_id": str(e.owner_symbol_id) if e.owner_symbol_id else None,
                "ordinal": e.ordinal,
                "kind": e.kind,
                "name": e.name,
                "local_name": e.local_name,
                "source_module": e.source_module,
                "start_line": e.span.start_line,
                "end_line": e.span.end_line,
            }
            for e in extraction.exports
        ],
    )


def _artifact(locator: str, body: str, *, facts: bool = True) -> ArtifactInput:
    """Real parser, facts and chunker output, normalised exactly like the store's rows."""
    data = body.encode()
    analysis_id, artifact_id = str(uuid4()), str(uuid4())
    identity = str(uuid5(NAMESPACE_URL, "artifact:" + locator))
    extractions: list[Row] = []
    facts_rows: list[Row] = []
    references: list[Row] = []
    exports: list[Row] = []
    if locator.endswith(".py"):
        parsed = PythonAstParser().parse(
            analysis_id=uuid4(), version_id=uuid4(), artifact_id=uuid4(), locator=locator, data=data
        )
        symbols = [
            replace(s, stable_identity_id=uuid5(NAMESPACE_URL, f"{locator}:{s.qualified_name}"))
            for s in parsed.symbols
        ]
        imports = parsed.imports
        language, declaration = "Python", None
        if facts:
            extraction = extract_python_facts(data, tuple(symbols))
            row, facts_rows, references, exports = _extraction_rows(extraction, analysis_id)
            extractions.append(row)
        source, _ = decode_source(data, python=True)
        chunks = python_chunks(source, uuid4(), identity, symbols)
    else:
        result = extract(locator, data)
        symbols = [
            replace(s, stable_identity_id=uuid5(NAMESPACE_URL, f"{locator}:{s.qualified_name}"))
            for s in result.symbols
        ]
        imports = result.imports
        assert result.extraction is not None
        row, facts_rows, references, exports = _extraction_rows(result.extraction, analysis_id)
        extractions.append(row)
        language = "TypeScript" if locator.endswith((".ts", ".tsx")) else "JavaScript"
        declaration = row["id"]
        source = body
        processor = "typescript" if language == "TypeScript" else "javascript"
        chunks = declaration_chunks(source, uuid4(), identity, symbols, processor)
    return ArtifactInput(
        {
            "id": analysis_id,
            "artifact_id": artifact_id,
            "locator": locator,
            "outcome": "parsed",
            "language": language,
            "line_count": body.count("\n"),
            "is_package_initializer": locator.endswith("__init__.py"),
            "declaration_extraction_id": declaration,
            "diagnostic_codes": [],
        },
        identity,
        tuple(_symbol_row(item) for item in symbols),
        tuple(_import_row(item, analysis_id) for item in imports),
        tuple(extractions),
        tuple(facts_rows),
        tuple(references),
        tuple(exports),
        {"id": str(uuid4()), "outcome": "processed", "fingerprint_scheme": None},
        tuple(
            {
                "id": str(c.id),
                "stable_key": c.stable_key,
                "ordinal": c.ordinal,
                "content": c.content,
                "content_hash": c.content_hash,
                "start_offset": c.span.start_offset,
                "end_offset": c.span.end_offset,
                "start_line": c.span.start_line,
                "end_line": c.span.end_line,
                "start_column": c.span.start_column,
                "end_column": c.span.end_column,
                "symbol_identity_id": str(c.symbol_identity_id) if c.symbol_identity_id else None,
            }
            for c in chunks
        ),
        True,
    )


def _catalog(*artifacts: ArtifactInput, extra: tuple[str, ...] = ()) -> CatalogBuild:
    digests = {
        name: value
        for name in (PYTHON_FACTS, "typescript-declarations.v1", "javascript-declarations.v1")
        if (value := extractor_contract_digest(name)) is not None
    }
    locators = [str(item.analysis["locator"]) for item in artifacts] + list(extra)
    return build_catalog(
        CatalogInput(
            PRODUCER,
            "complete",
            "complete",
            tuple(
                {"id": str(uuid5(NAMESPACE_URL, "inventory:" + value)), "locator": value}
                for value in locators
            ),
            artifacts,
            digests,
        )
    )


def _by_name(build: CatalogBuild) -> dict[str, SemanticEntity]:
    return {f"{item.kind}:{item.name}": item for item in build.entities}


CLASS = (
    "class Store:\n    def get(self, key):\n        return key\n\n"
    "    def put(self, key):\n        return None\n"
)


# --------------------------------------------------------------------------- import resolution


def test_python_import_resolution_against_inventory() -> None:
    resolver = ImportResolver(
        [
            "backend/pyproject.toml",
            "backend/alembic/env.py",
            "backend/src/app/__init__.py",
            "backend/src/app/db.py",
            "backend/src/app/sub/__init__.py",
            "backend/src/app/sub/mod.py",
            "tools/app/db.py",
            "lib/util.py",
            "util.py",
        ]
    )

    def row(form: str, name: str, module: str | None = None, level: int = 0) -> Row:
        return {"form": form, "name": name, "from_module": module, "level": level}

    here = "backend/src/app/sub/mod.py"
    assert resolver.python(here, row("from", "db", "app")) == semantic_catalog.Resolved(
        "resolved", "backend/src/app/db.py", True
    )
    relative = resolver.python(here, row("from", "Session", "db", 2))
    assert (relative.status, relative.locator) == ("resolved", "backend/src/app/db.py")
    assert resolver.python(here, row("from", "mod", None, 1)).locator == here
    assert resolver.python(here, row("import", "app.missing")).status == "not_found"
    assert resolver.python(here, row("import", "os.path")).status == "external"
    # A namespace-style folder (no __init__.py) does not shadow the installed package.
    assert resolver.python(here, row("from", "op", "alembic")).status == "external"
    assert resolver.python(here, row("import", "util")).locator == "util.py"
    assert resolver.python(here, row("from", "x", "app.sub", 5)).status == "not_found"


def test_specifier_resolution_is_priority_ordered_and_bounded() -> None:
    resolver = ImportResolver(["web/a.ts", "web/a.tsx", "web/lib/index.ts", "web/b.ts"])
    assert resolver.specifier("web/x.ts", "./a").locator == "web/a.ts"
    assert resolver.specifier("web/x.ts", "./b.js").locator == "web/b.ts"
    assert resolver.specifier("web/x.ts", "./lib").locator == "web/lib/index.ts"
    assert resolver.specifier("web/x.ts", "./missing").status == "not_found"
    assert resolver.specifier("web/x.ts", "../../escape").status == "not_found"
    assert resolver.specifier("web/x.ts", "@/lib/x").status == "not_attempted"
    assert resolver.specifier("web/x.ts", "react").status == "external"
    assert resolver.specifier("web/x.ts", None).status == "not_attempted"


# --------------------------------------------------------------------------- recognizers


def _view(item: ArtifactInput) -> ArtifactView:
    facts = {str(row["symbol_id"]): row for row in item.symbol_facts}
    return ArtifactView(
        item.analysis, item.symbols, item.imports, facts, item.references, bool(item.extractions)
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "from flask import Flask\napp = Flask(__name__)\n\n"
            "@app.route('/a', methods=['POST', 'PUT'])\ndef a():\n    return 1\n",
            [("flask-route.v1", "pattern_with_local_evidence", "POST,PUT /a")],
        ),
        (
            "@cache.get(key)\ndef cached():\n    return 1\n",
            [("fastapi-route.v1", "pattern_only", "GET")],
        ),
        ("@property\ndef p(self):\n    return 1\n\n@app.getter('/x')\ndef q():\n    pass\n", []),
        (
            "from sqlmodel import SQLModel\n\nclass Hero(SQLModel, table=True):\n    id: int\n",
            [("sqlalchemy-declarative.v1", "pattern_with_local_evidence", "Hero")],
        ),
        ("class Plain(Base):\n    name = 'x'\n", []),
    ],
)
def test_python_recognizers_with_negatives(body: str, expected: list[tuple[str, str, str]]) -> None:
    matches = recognize(_view(_artifact("m.py", body)))
    assert [(m.recognizer, m.strength, m.name) for m in matches] == expected
    for match in matches:
        assert "decorator_shape" in match.met or match.role == "db_table"


def test_table_recognizer_needs_facts_and_reports_unmet_conditions() -> None:
    body = "class User(Base):\n    __tablename__ = 'users'\n"
    assert recognize(_view(_artifact("m.py", body, facts=False))) == []
    [match] = recognize(_view(_artifact("m.py", body)))
    assert (match.strength, match.name, match.unmet) == ("pattern_only", "users", ("orm_import",))


def test_react_recognizers_with_negatives() -> None:
    body = (
        "export function useThing() { return useState(1); }\n"
        "export function useless() { return 1; }\n"
        "export function lower() { return <div/>; }\n"
        "export function Upper() { return 1; }\n"
        "export class Old extends React.Component { render() { return null; } }\n"
        "export const Card = () => <section/>;\n"
    )
    matches = recognize(_view(_artifact("w.tsx", body)))
    assert sorted((m.recognizer, m.name, m.strength) for m in matches) == [
        ("react-component.v1", "Card", "pattern_with_local_evidence"),
        ("react-component.v1", "Old", "pattern_with_local_evidence"),
        ("react-hook.v1", "useThing", "pattern_with_local_evidence"),
    ]


# --------------------------------------------------------------------------- coverage and packages


def test_coverage_states_and_reasons() -> None:
    entities = _by_name(_catalog(_artifact("s.py", CLASS)))
    assert {item.stored_coverage for item in entities.values()} == {"complete"}
    assert entities["symbol:Store"].context_coverage == "full"
    assert entities["file:s.py"].context_coverage == "outline"
    assert [region.entity_key for region in entities["file:s.py"].omitted] == [
        entities["symbol:Store"].entity_key
    ]

    item = _artifact("s.py", CLASS)
    put = next(row for row in item.symbols if row["name"] == "put")
    missing = replace(
        item,
        chunks=tuple(
            c for c in item.chunks if c["symbol_identity_id"] != put["stable_identity_id"]
        ),
    )
    entities = _by_name(_catalog(missing))
    assert (
        entities["symbol:Store.put"].stored_coverage,
        entities["symbol:Store.put"].coverage_reason,
    ) == (
        "incomplete",
        "NO_STORED_CHUNKS",
    )
    store = entities["symbol:Store"]
    assert (store.stored_coverage, store.coverage_reason) == (
        "incomplete",
        "DECLARATION_WITHOUT_CHUNK",
    )
    assert store.context_coverage == "not_eligible" and store.package_digest is None
    assert entities["file:s.py"].coverage_reason == "DECLARATION_WITHOUT_CHUNK"

    tampered = replace(item, chunks=(replace_content(item.chunks[0], "x"), *item.chunks[1:]))
    assert _by_name(_catalog(tampered))["file:s.py"].coverage_reason == "CHUNK_INTEGRITY"
    shifted = replace(
        item,
        chunks=tuple(
            {**c, "start_line": c["start_line"] + 1}
            if c["symbol_identity_id"] == put["stable_identity_id"]
            else c
            for c in item.chunks
        ),
    )
    assert (
        _by_name(_catalog(shifted))["symbol:Store.put"].coverage_reason == "REGION_START_MISMATCH"
    )
    for change, reason in (
        ({"processing": None}, "NOT_PROCESSED"),
        ({"processing": {"id": "p", "outcome": "failed"}}, "PROCESSING_FAILED"),
        ({"dependency_current": False}, "STRUCTURAL_DEPENDENCY_CHANGED"),
    ):
        unavailable = _by_name(_catalog(replace(item, **change)))["symbol:Store"]
        assert (unavailable.stored_coverage, unavailable.coverage_reason) == ("unavailable", reason)
        assert unavailable.context_coverage == "not_eligible"


def replace_content(chunk: Row, value: str) -> Row:
    return {**chunk, "content": value + str(chunk["content"])[1:]}


def test_budget_selects_outline_or_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    method_body = "        x = 1\n" * 60
    function_body = "    x = 1\n" * 60
    body = (
        f"class Store:\n    def get(self, key):\n{method_body}        return key\n\n"
        "    def put(self, key):\n        return None\n\n\n"
        f"def long():\n{function_body}    return 1\n"
    )
    monkeypatch.setattr(semantic_catalog, "PACKAGE_BYTE_CEILING", 400)
    entities = _by_name(_catalog(_artifact("s.py", body)))
    assert entities["symbol:long"].context_coverage == "not_eligible"
    assert entities["symbol:long"].context_reason == "CONTEXT_BUDGET_EXCEEDED"
    store = entities["symbol:Store"]
    assert store.context_coverage == "outline" and store.package_bytes <= 400
    assert [region.start_line for region in store.omitted] == [2, 65]
    assert len(store.package_chunks) == 1  # only the class's own residual text
    assert entities["symbol:Store.put"].context_coverage == "full"
    monkeypatch.setattr(semantic_catalog, "PACKAGE_BYTE_CEILING", 10)
    assert _by_name(_catalog(_artifact("s.py", body)))["symbol:Store"].context_reason == (
        "CONTEXT_BUDGET_EXCEEDED"
    )


def test_typescript_merged_declarations_and_partial_files() -> None:
    body = (
        "export interface Options { a: string }\n"
        "export function get(path: string): string;\n"
        "export function get(path: any) { return path; }\n"
        "interface Options { b: number }\n"
        "const = ;\n"
    )
    entities = _by_name(_catalog(_artifact("w.ts", body)))
    options = entities["symbol:Options"]
    assert options.stored_coverage == "complete" and len(options.evidence) == 2
    assert entities["file:w.ts"].limitations == ("PARTIAL_PARSE_ARTIFACT",)
    assert entities["file:w.ts"].context_coverage == "not_eligible"
    assert entities["symbol:get"].limitations == ("PARTIAL_PARSE_ARTIFACT",)


def test_noncurrent_declaration_contract_hides_symbols() -> None:
    item = _artifact("w.ts", "export function f() { return 1; }\n")
    stale = replace(item, extractions=({**item.extractions[0], "contract_digest": "f" * 64},))
    entities = _by_name(_catalog(stale))
    assert list(entities) == ["file:w.ts"]
    assert entities["file:w.ts"].coverage_reason == "EXTRACTOR_CONTRACT_CHANGED"


# --------------------------------------------------------------------------- determinism and facts


def test_catalog_is_deterministic_and_order_independent() -> None:
    items = [
        _artifact("app/db.py", "class User(Base):\n    __tablename__ = 'users'\n"),
        _artifact("app/api.py", "from .db import User\n\nclass Admin(User):\n    pass\n"),
        _artifact("web/a.tsx", "import { B } from './b';\nexport const A = () => <B/>;\n"),
        _artifact("web/b.tsx", "export function B() { return <i/>; }\n"),
    ]
    first = _catalog(*items)
    shuffled = list(items)
    random.Random(7).shuffle(shuffled)
    second = _catalog(*shuffled)
    assert (first.catalog_digest, first.portable_digest) == (
        second.catalog_digest,
        second.portable_digest,
    )
    assert first.relations == second.relations
    # Fresh row ids (another database) keep the portable digest only.
    third = _catalog(*[_artifact(str(i.analysis["locator"]), _body(i)) for i in items])
    assert third.portable_digest == first.portable_digest
    # Content digests (the generation freshness key) are row-id independent as well.
    assert {e.portable_key: e.content_digest for e in third.entities} == {
        e.portable_key: e.content_digest for e in first.entities
    }
    assert all(e.content_digest for e in first.entities if e.package_digest)
    assert third.catalog_digest != first.catalog_digest
    renders = [r for r in first.relations if r.kind == "renders"]
    assert [r.resolution_status for r in renders] == ["imported"]


def _body(item: ArtifactInput) -> str:
    return "".join(
        str(chunk["content"]) + ("\n" if not str(chunk["content"]).endswith("\n") else "")
        for chunk in item.chunks
    )


def test_rendered_facts_depend_on_every_cited_row() -> None:
    item = _artifact("s.py", CLASS)
    build = _catalog(item)
    tables: dict[str, tuple[Row, ...]] = {
        "symbols": item.symbols,
        "symbol_facts": item.symbol_facts,
        "symbol_references": item.references,
    }
    for fact in build.facts:
        if fact.fact_kind not in {"signature", "reference"}:
            continue
        refs = list(fact.source_refs)
        rows = [next(r for r in tables[ref["table"]] if r["id"] == ref["id"]) for ref in refs]
        text, value = rendered_fact(fact.fact_kind, refs, rows, fact.derived)
        assert (text, value) == (fact.rendered_text, fact.fact_digest)
        tampered = [{**rows[0], "qualified_name": "Other"}, *rows[1:]]
        changed, digest_value = rendered_fact(fact.fact_kind, refs, tampered, fact.derived)
        assert "Other" in changed and digest_value != fact.fact_digest
    # Rendered text is portable: it never embeds row or identity ids.
    identities = {str(row["stable_identity_id"]) for row in item.symbols}
    assert not any(value in fact.rendered_text for fact in build.facts for value in identities)
