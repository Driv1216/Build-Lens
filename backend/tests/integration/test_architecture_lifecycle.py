"""Stage 14 Milestone B lifecycle (plan F10): pre-literal runs, the contract bump, gaining
literals through an explicit structural run, mixed coverage and literal contract changes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, delete, select

from buildlens.application import architecture as service_module
from buildlens.application.architecture import ArchitectureService, ArchitectureStaleError
from buildlens.domain import architecture as domain
from buildlens.domain import extractors
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from buildlens.infrastructure.persistence.models import (
    ArchitectureLiteralRecord,
    ArtifactAnalysisRecord,
    ArtifactExtractionRecord,
)
from tests.semantic_support import CatalogStack, write

pytestmark = pytest.mark.integration

LITERALS = [extractors.PYTHON_LITERALS, extractors.TS_LITERALS]
FILES = {
    "pyproject.toml": '[project]\nname = "life"\n',
    "app/__init__.py": "",
    "app/a.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from app import b\n",
    "app/b.py": "from app import a\n",
    "web/x.ts": "import type { Y } from './y'\nexport type X = Y\n",
    "web/y.ts": "import type { X } from './x'\nexport type Y = X\n",
}


def _stack(engine: Engine, root: Path) -> tuple[CatalogStack, Any, ArchitectureService]:
    write(root, FILES)
    stack = CatalogStack(engine, root, "Lifecycle")
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    return (
        stack,
        snapshot,
        ArchitectureService(SqlAlchemyArchitectureStore(stack.factory), stack.catalog),
    )


def _strip(stack: CatalogStack, locator: str | None = None) -> None:
    with stack.factory.begin() as session:
        extractions = select(ArtifactExtractionRecord.id).where(
            ArtifactExtractionRecord.extractor.in_(LITERALS)
        )
        if locator is not None:
            extractions = extractions.join(
                ArtifactAnalysisRecord,
                ArtifactAnalysisRecord.id == ArtifactExtractionRecord.artifact_analysis_id,
            ).where(ArtifactAnalysisRecord.locator == locator)
        ids = list(session.scalars(extractions))
        session.execute(
            delete(ArchitectureLiteralRecord).where(
                ArchitectureLiteralRecord.extraction_id.in_(ids)
            )
        )
        session.execute(
            delete(ArtifactExtractionRecord).where(ArtifactExtractionRecord.id.in_(ids))
        )


def _classes(service: ArchitectureService, stack: CatalogStack, snapshot: Any) -> dict[str, str]:
    _, _, loaded = service.graph(stack.project.id, snapshot.id, None)
    return {
        f"{edge.source_key}->{edge.target_key}": str(edge.import_class)
        for edge in loaded.edges.values()
        if edge.kind == "imports" and edge.target_key and edge.target_key.startswith("module:")
    }


def test_guard_rows_move_cycles_out_of_runtime_class_sets(
    pg_engine: Engine, tmp_path: Path
) -> None:
    stack, snapshot, service = _stack(pg_engine, tmp_path)
    run = service.build(stack.project.id, snapshot.id)
    classes = _classes(service, stack, snapshot)
    assert classes["module:app/a.py->module:app/b.py"] == "type_level"
    assert classes["module:app/b.py->module:app/a.py"] == "unconditional"
    # ``import type`` statements are type level; the cycles exist only from S3 upward.
    assert classes["module:web/x.ts->module:web/y.ts"] == "type_level"
    _, _, loaded = service.graph(stack.project.id, snapshot.id, None)
    sets = sorted(c.class_set for c in loaded.components.values() if c.level == "module")
    assert sets == ["S3", "S3"]
    assert run.coverage["python_guard_context"] == "known"
    assert run.coverage["opaque_dynamic_imports"] == "none_observed"


def test_pre_literal_structure_keeps_milestone_a_semantics(
    pg_engine: Engine, tmp_path: Path
) -> None:
    write(tmp_path, FILES)
    stack = CatalogStack(pg_engine, tmp_path, "Pre-literal")
    snapshot = stack.prepared()
    _strip(stack)
    stack.catalog.build(stack.project.id, snapshot.id)
    service = ArchitectureService(SqlAlchemyArchitectureStore(stack.factory), stack.catalog)
    run = service.build(stack.project.id, snapshot.id)
    classes = _classes(service, stack, snapshot)
    assert classes["module:app/a.py->module:app/b.py"] == "presumed_unconditional"
    assert classes["module:web/x.ts->module:web/y.ts"] == "elision_dependent"
    assert run.coverage["python_guard_context"] == "unknown"
    assert run.coverage["opaque_dynamic_imports"] == "not_observable"
    assert run.coverage["literals"] == {"not_extracted": 5}
    codes = {
        d.code for d in service.graph(stack.project.id, snapshot.id, None)[2].stored.diagnostics
    }
    assert "LITERALS_NOT_EXTRACTED" in codes

    # Gaining literals needs an explicit structural run; the chain then goes stale in order.
    stack.structural.analyse(stack.project.id, snapshot.id)
    stack.processing.process(stack.project.id, snapshot.id)
    assert service.status(stack.project.id, snapshot.id).freshness == "stale"
    with pytest.raises(ArchitectureStaleError):
        service.select(stack.project.id, snapshot.id, None)
    stack.catalog.build(stack.project.id, snapshot.id)
    upgraded = service.build(stack.project.id, snapshot.id)
    assert upgraded.id != run.id
    assert upgraded.coverage["python_guard_context"] == "known"
    assert _classes(service, stack, snapshot)["module:app/a.py->module:app/b.py"] == "type_level"
    # The earlier run stays readable as labelled history.
    history, _, _ = service.graph(stack.project.id, snapshot.id, run.id)
    assert history.id == run.id


def test_mixed_coverage_is_per_artifact(pg_engine: Engine, tmp_path: Path) -> None:
    write(tmp_path, FILES)
    stack = CatalogStack(pg_engine, tmp_path, "Mixed")
    snapshot = stack.prepared()
    _strip(stack, "app/a.py")
    stack.catalog.build(stack.project.id, snapshot.id)
    service = ArchitectureService(SqlAlchemyArchitectureStore(stack.factory), stack.catalog)
    run = service.build(stack.project.id, snapshot.id)
    classes = _classes(service, stack, snapshot)
    assert classes["module:app/a.py->module:app/b.py"] == "presumed_unconditional"
    assert classes["module:app/b.py->module:app/a.py"] == "unconditional"
    assert run.coverage["python_guard_context"] == "unknown"
    _, _, loaded = service.graph(stack.project.id, snapshot.id, None)
    subjects = [d.subjects for d in loaded.stored.diagnostics if d.code == "LITERALS_NOT_EXTRACTED"]
    assert subjects == [("module:app/a.py",)]


def test_contract_changes_make_runs_stale_with_named_reasons(
    pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, snapshot, service = _stack(pg_engine, tmp_path)
    original = domain.architecture_contract
    monkeypatch.setattr(
        service_module,
        "architecture_contract",
        lambda: {**original(), "architecture": "architecture-model.v1", "literals": None},
    )
    legacy = service.build(stack.project.id, snapshot.id)
    monkeypatch.setattr(service_module, "architecture_contract", original)
    reasons = service.freshness(stack.project.id, snapshot.id, legacy)
    assert reasons == ("ARCHITECTURE_CONTRACT_CHANGED",)  # a v1 run is stale under v2
    current = service.build(stack.project.id, snapshot.id)
    assert service.freshness(stack.project.id, snapshot.id, current) == ()

    contract = extractors.extractor_contract

    def bumped(name: str) -> dict[str, str] | None:
        value = contract(name)
        return {**value, "rows": value["rows"] + "+bumped"} if value and name in LITERALS else value

    monkeypatch.setattr(extractors, "extractor_contract", bumped)
    assert service.freshness(stack.project.id, snapshot.id, current) == (
        "ARCHITECTURE_CONTRACT_CHANGED",
        "LITERAL_CONTRACT_CHANGED",
    )
