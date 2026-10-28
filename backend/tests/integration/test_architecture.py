"""Stage 14 Milestone A: architecture runs on real PostgreSQL (build, freshness, reproduction)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, func, select

from alembic import command
from buildlens.application.architecture import (
    ArchitectureRequiredError,
    ArchitectureService,
    ArchitectureStaleError,
)
from buildlens.application.semantic_catalog import SemanticCatalogRequiredError
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from buildlens.infrastructure.persistence.models import (
    ArchitectureEvidenceLinkRecord,
    ArchitectureRunRecord,
    ProjectVersionRecord,
)
from tests.semantic_support import REPOSITORY, CatalogStack, write

pytestmark = pytest.mark.integration


def architecture(stack: CatalogStack) -> ArchitectureService:
    return ArchitectureService(SqlAlchemyArchitectureStore(stack.factory), stack.catalog)


def prepared(engine: Engine, root: Path, name: str = "Stage 14 A") -> tuple[CatalogStack, Any]:
    write(root, REPOSITORY)
    stack = CatalogStack(engine, root, name)
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    return stack, snapshot


def test_build_projects_the_catalog_without_source_reads(
    pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, snapshot = prepared(pg_engine, tmp_path)
    service = architecture(stack)
    with pytest.raises(ArchitectureRequiredError):
        service.select(stack.project.id, snapshot.id, None)

    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("the architecture build must not read source files")

    monkeypatch.setattr(
        "buildlens.infrastructure.structural.supervisor.SupervisedSourceReader.read", forbidden
    )
    run = service.build(stack.project.id, snapshot.id)
    assert run.revision == 1 and run.graph_digest and run.portable_digest
    assert run.outcome == "partial"  # web/broken.ts parses partially
    _, reasons, loaded = service.graph(stack.project.id, snapshot.id, None)
    assert reasons == ()
    edges = {(e.kind, e.source_key, e.target_key or e.target_text) for e in loaded.edges.values()}
    assert ("imports", "module:app/api.py", "module:app/db.py") in edges
    assert ("imports", "module:web/Button.tsx", "module:web/hooks.ts") in edges
    assert ("imports", "module:web/alias.ts", "@/lib/x") in edges
    assert ("imports", "module:app/api.py", "external:python:fastapi") in edges
    assert ("imports", "module:web/alias.ts", "external:npm:lodash") in edges
    assert "module:notes.md" not in loaded.nodes  # not code and not an import target
    assert loaded.nodes["module:web/broken.ts"].coverage_state == "partial"
    assert loaded.nodes["workspace:."].attributes["manifests"] == ["pyproject.toml"]
    # Every element carries at least one persisted evidence link.
    with stack.factory() as session:
        links = session.scalar(
            select(func.count()).where(ArchitectureEvidenceLinkRecord.architecture_run_id == run.id)
        )
    total = sum(len(group) for group in (loaded.nodes, loaded.edges, loaded.components))
    assert links and links >= total
    # Unchanged inputs short-circuit to the active run.
    assert service.build(stack.project.id, snapshot.id).id == run.id


def test_reproduction_contracts_and_staleness(pg_engine: Engine, tmp_path: Path) -> None:
    stack, snapshot = prepared(pg_engine, tmp_path / "one")
    service = architecture(stack)
    first = service.build(stack.project.id, snapshot.id)

    # Processing reprocess + catalog rebuild over the same structural rows: same instance digest.
    stack.processing.process(stack.project.id, snapshot.id)
    with pytest.raises(ArchitectureStaleError) as stale:
        service.select(stack.project.id, snapshot.id, None)
    assert "CATALOG_AUTHORITY_CHANGED" in stale.value.reasons or stale.value.reasons
    stack.catalog.build(stack.project.id, snapshot.id)
    second = service.build(stack.project.id, snapshot.id)
    assert second.id != first.id
    assert second.architecture_digest == first.architecture_digest
    assert second.portable_digest == first.portable_digest
    history, reasons = service.select(stack.project.id, snapshot.id, first.id)
    assert history.id == first.id and "CATALOG_AUTHORITY_CHANGED" in reasons

    # A second project over identical bytes: different rows, same portable digest.
    other, other_snapshot = prepared(pg_engine, tmp_path / "two", "Stage 14 A copy")
    copy = architecture(other).build(other.project.id, other_snapshot.id)
    assert copy.portable_digest == first.portable_digest
    assert copy.architecture_digest != first.architecture_digest

    # A new structural run makes the catalog, and therefore the architecture, stale.
    stack.structural.analyse(stack.project.id, snapshot.id)
    with pytest.raises((ArchitectureStaleError, SemanticCatalogRequiredError)):
        service.select(stack.project.id, snapshot.id, None)


def test_superseded_and_failed_runs_never_move_the_pointer(
    pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, snapshot = prepared(pg_engine, tmp_path)
    store = SqlAlchemyArchitectureStore(stack.factory)
    service = ArchitectureService(store, stack.catalog)
    first = service.build(stack.project.id, snapshot.id)
    from dataclasses import replace
    from uuid import uuid4

    from buildlens.application.architecture_builder import build_architecture

    catalog, _ = stack.catalog.select(stack.project.id, snapshot.id, None)
    build = build_architecture(store.load_input(catalog))
    late = replace(
        first, id=uuid4(), captured_latest_revision=0, captured_active_architecture_run_id=None
    )
    published = store.publish(late, build)
    assert published.superseded and published.revision == 2
    failed = store.save_failure(replace(first, id=uuid4()), "ARCHITECTURE_LIMIT_EXCEEDED")
    assert failed.outcome == "failed" and failed.diagnostics == ("ARCHITECTURE_LIMIT_EXCEEDED",)
    with stack.factory() as session:
        version = session.get(ProjectVersionRecord, snapshot.id)
        assert version is not None and version.active_architecture_run_id == first.id
        assert session.scalar(select(func.count()).select_from(ArchitectureRunRecord)) == 3


def test_downgrade_refuses_with_architecture_rows(pg_engine: Engine, tmp_path: Path) -> None:
    stack, snapshot = prepared(pg_engine, tmp_path)
    architecture(stack).build(stack.project.id, snapshot.id)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with pytest.raises(RuntimeError, match="architecture runs exist"):
        command.downgrade(config, "0013_semantic_config_outcome")
