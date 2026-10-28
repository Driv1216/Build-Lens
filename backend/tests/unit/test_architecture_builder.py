"""Stage 14 Milestone A: the pure architecture builder over real parser and catalog output."""

from __future__ import annotations

from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

import pytest

from buildlens.application.architecture_builder import (
    ArchitectureBuildError,
    ArchitectureInput,
    architecture_input,
    build_architecture,
)
from buildlens.application.semantic_catalog import ArtifactInput, CatalogInput, build_catalog
from buildlens.domain.architecture import (
    ArchitectureBuild,
    component_digest,
    diagnostic_digest,
    edge_digest,
    graph_digest_of,
    node_digest,
)
from buildlens.domain.extractors import PYTHON_FACTS, extractor_contract_digest
from tests.unit.test_semantic_catalog import PRODUCER, _artifact

LANGUAGES = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
}


def _language(locator: str) -> str | None:
    return next((v for k, v in LANGUAGES.items() if locator.endswith(k)), None)


def _inputs(files: dict[str, str], extra: tuple[str, ...] = ()) -> ArchitectureInput:
    artifacts: list[ArtifactInput] = [
        _artifact(locator, body) for locator, body in files.items() if _language(locator)
    ]
    inventory = tuple(
        {
            "id": str(uuid5(NAMESPACE_URL, "inventory:" + locator)),
            "locator": locator,
            "language": _language(locator),
            "content_digest": str(uuid5(NAMESPACE_URL, "bytes:" + locator)),
            "stable_identity_id": str(uuid5(NAMESPACE_URL, "artifact:" + locator)),
        }
        for locator in [*files, *extra]
    )
    digests = {
        name: value
        for name in (PYTHON_FACTS, "typescript-declarations.v1", "javascript-declarations.v1")
        if (value := extractor_contract_digest(name)) is not None
    }
    catalog = build_catalog(
        CatalogInput(PRODUCER, "complete", "complete", inventory, tuple(artifacts), digests)
    )
    counts = {
        fact.fact_digest: int(fact.derived.get("count", 1))
        for fact in catalog.facts
        if fact.fact_kind == "relation"
    }
    return architecture_input(
        inventory=inventory,
        analyses=[item.analysis for item in artifacts],
        imports={str(item.analysis["id"]): item.imports for item in artifacts},
        exports={str(item.analysis["id"]): item.exports for item in artifacts},
        extractions={str(item.analysis["id"]): item.extractions for item in artifacts},
        owner_kinds={
            str(symbol["id"]): str(symbol["kind"]) for item in artifacts for symbol in item.symbols
        },
        producer_digest=PRODUCER,
        entities=catalog.entities,
        relations=catalog.relations,
        relation_counts=counts,
        catalog_outcome="complete",
    )


def _edges(build: ArchitectureBuild, kind: str = "imports") -> dict[tuple[str, str], str]:
    return {
        (edge.source_key, edge.target_key or f"text:{edge.target_text}"): str(edge.import_class)
        for edge in build.edges
        if edge.kind == kind
    }


PYTHON = {
    "pyproject.toml": "",
    "src/app/__init__.py": "",
    "src/app/a.py": "from app import b\nimport yaml\nfrom .missing import x\n",
    "src/app/b.py": "from app.c import thing\n\ndef late():\n    import app.a\n",
    "src/app/c.py": "from . import a\nthing = 1\n",
    "src/app/self.py": "import app.self\n",
}


def test_python_edges_classes_and_cycles() -> None:
    build = build_architecture(_inputs(PYTHON))
    edges = _edges(build)
    assert edges[("module:src/app/a.py", "module:src/app/b.py")] == "presumed_unconditional"
    assert edges[("module:src/app/b.py", "module:src/app/a.py")] == "deferred"
    assert edges[("module:src/app/a.py", "external:python:yaml")] == "presumed_unconditional"
    assert ("module:src/app/a.py", "text:.missing") in edges
    unresolved = next(e for e in build.edges if e.target_text == ".missing")
    assert unresolved.tier == "unknown" and unresolved.resolution_status == "not_found"
    cycles = {c.class_set: c for c in build.components if c.level == "module"}
    # a -> b -> c -> a exists without guard information: reported in S1, not S0.
    assert set(cycles) == {"S1"}
    assert cycles["S1"].members == (
        "module:src/app/a.py",
        "module:src/app/b.py",
        "module:src/app/c.py",
    )
    assert len(cycles["S1"].witness) == 3
    diagnostic = next(d for d in build.diagnostics if d.code == "IMPORT_CYCLE")
    assert diagnostic.attributes["limitations"] == ["PYTHON_GUARD_CONTEXT_UNKNOWN"]
    codes = {d.code for d in build.diagnostics}
    assert {"SELF_IMPORT", "IMPORT_NOT_FOUND"} <= codes
    nodes = {n.key: n for n in build.nodes}
    assert nodes["workspace:."].attributes["manifests"] == ["pyproject.toml"]
    assert nodes["container:src/app"].attributes["python_package"] is True
    assert nodes["container:src"].attributes["source_root"] is True
    assert nodes["module:src/app/a.py"].metrics["external_packages"] == 1
    assert build.coverage["python_guard_context"] == "unknown"


TYPESCRIPT = {
    "web/package.json": "{}",
    "web/src/main.tsx": (
        "import './styles.css';\nimport { App } from './App';\n"
        "import type { Props } from './types';\n"
        "import React from 'react';\nimport { thing } from '@/alias';\n"
        "export async function load() { return import('./lazy'); }\n"
    ),
    "web/src/App.tsx": "import { type Props, helper } from './types';\nexport function App() {}\n",
    "web/src/types.ts": (
        "import { App } from './App';\nexport type Props = {};\nexport const helper = 1;\n"
    ),
    "web/src/lazy.ts": "export * from './types';\n",
    "web/src/plain.js": "import { App } from './App';\nconst x = require('./lazy');\n",
}


def test_typescript_classes_aliases_reexports_and_side_effects() -> None:
    build = build_architecture(_inputs(TYPESCRIPT, extra=("web/src/styles.css",)))
    edges = _edges(build)
    main = "module:web/src/main.tsx"
    assert edges[(main, "module:web/src/styles.css")] == "unconditional"
    assert edges[(main, "module:web/src/App.tsx")] == "elision_dependent"
    types_edge = next(
        e
        for e in build.edges
        if e.kind == "imports"
        and e.source_key == main
        and e.target_key == "module:web/src/types.ts"
    )
    assert types_edge.import_class == "elision_dependent"
    assert "TYPE_IMPORT_FORM_UNDISTINGUISHED" in types_edge.conditions
    assert edges[(main, "module:web/src/lazy.ts")] == "deferred"
    assert edges[(main, "external:npm:react")] == "elision_dependent"
    alias = next(e for e in build.edges if e.target_text == "@/alias")
    assert alias.resolution_status == "not_attempted" and alias.tier == "unknown"
    assert edges[("module:web/src/plain.js", "module:web/src/App.tsx")] == "unconditional"
    assert edges[("module:web/src/plain.js", "module:web/src/lazy.ts")] == "presumed_unconditional"
    assert _edges(build, "reexports")[("module:web/src/lazy.ts", "module:web/src/types.ts")]
    alias_coverage = build.coverage["alias"]
    assert isinstance(alias_coverage, dict)
    assert alias_coverage["prefixes"] == {"@/": 1} and alias_coverage["files"] == [
        "web/src/main.tsx"
    ]
    cycles = {c.class_set for c in build.components if c.level == "module"}
    assert cycles == {"S2"}  # App <-> types only through elision-dependent imports
    nodes = {n.key: n for n in build.nodes}
    assert nodes["module:web/src/styles.css"].coverage_state == "not_code"
    assert nodes["workspace:web"].attributes["ecosystems"] == ["npm"]


def test_aggregate_cycle_is_distinct_from_module_cycle() -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/x/__init__.py": "",
        "pkg/x/one.py": "from pkg.y import two\n",
        "pkg/x/three.py": "",
        "pkg/y/__init__.py": "",
        "pkg/y/two.py": "",
        "pkg/y/four.py": "from pkg.x import three\n",
    }
    build = build_architecture(_inputs(files))
    assert [c for c in build.components if c.level == "module"] == []
    aggregate = [c for c in build.components if c.level == "container"]
    assert len(aggregate) == 1 and aggregate[0].attributes["module_cycle_underlying"] is False
    assert aggregate[0].members == ("container:pkg/x", "container:pkg/y")
    diagnostic = next(d for d in build.diagnostics if d.code == "AGGREGATE_DEPENDENCY_CYCLE")
    assert diagnostic.attributes["module_cycle_underlying"] is False
    depends = [e for e in build.edges if e.kind == "depends_on"]
    assert {(e.source_key, e.target_key) for e in depends} == {
        ("container:pkg/x", "container:pkg/y"),
        ("container:pkg/y", "container:pkg/x"),
    }


def test_digests_are_complete_reproducible_and_portable() -> None:
    first = build_architecture(_inputs(PYTHON))
    again = build_architecture(_inputs(PYTHON))  # new row ids, same bytes
    assert first.portable_digest == again.portable_digest
    assert first.architecture_digest != again.architecture_digest
    same = _inputs(PYTHON)
    assert (
        build_architecture(same).architecture_digest == build_architecture(same).architecture_digest
    )
    recomputed = graph_digest_of(
        [node_digest(n) for n in first.nodes],
        [edge_digest(e) for e in first.edges],
        [component_digest(c) for c in first.components],
        [diagnostic_digest(d) for d in first.diagnostics],
        first.coverage,
    )
    assert recomputed == first.graph_digest
    edge = next(e for e in first.edges if e.kind == "imports" and len(e.evidence) == 2)
    for changed in (
        replace(edge, tier="inferred"),
        replace(edge, attributes={**edge.attributes, "star": not edge.attributes.get("star")}),
        replace(edge, conditions=(*edge.conditions, "X")),
        replace(edge, evidence=edge.evidence[:1]),
    ):
        assert edge_digest(changed) != edge_digest(edge)
    assert all(edge.evidence for edge in first.edges)
    assert all(node.evidence for node in first.nodes)
    assert all(item.evidence for item in first.diagnostics)


def test_projection_must_match_the_catalog() -> None:
    source = _inputs(PYTHON)
    tampered = replace(
        source,
        relations=tuple(replace(r, count=r.count + 1) for r in source.relations[:1])
        + source.relations[1:],
    )
    with pytest.raises(ArchitectureBuildError) as error:
        build_architecture(tampered)
    assert error.value.code == "ARCHITECTURE_CATALOG_MISMATCH"
    missing = replace(source, relations=source.relations[1:])
    with pytest.raises(ArchitectureBuildError):
        build_architecture(missing)
