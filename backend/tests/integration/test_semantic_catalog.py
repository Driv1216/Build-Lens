"""Stage 13 Milestone B: deterministic semantic catalog on real PostgreSQL."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select, text, update

from alembic import command
from buildlens.api.main import create_app
from buildlens.application.semantic_catalog import (
    EntityFilters,
    SemanticCatalogConflictError,
    SemanticCatalogStaleError,
)
from buildlens.core.config import Settings
from buildlens.domain.extractors import PYTHON_FACTS
from buildlens.infrastructure.persistence.models import (
    ArtifactExtractionRecord,
    ChunkRecord,
    SemanticCatalogRunRecord,
    SemanticEntityRecord,
    SymbolRecord,
)
from tests.semantic_support import REPOSITORY, CatalogStack, write

pytestmark = pytest.mark.integration


def _entities(stack: CatalogStack, run_id: Any) -> dict[str, Any]:
    values, _ = stack.store.entity_page(run_id, EntityFilters(), 500, 0)
    return {f"{item.kind}:{item.locator}:{item.name}": item for item in values}


def test_catalog_entities_recognizers_relations_and_reproduction(
    pg_engine: Engine, tmp_path: Path
) -> None:
    write(tmp_path, REPOSITORY)
    stack = CatalogStack(pg_engine, tmp_path)
    snapshot = stack.prepared()
    run = stack.catalog.build(stack.project.id, snapshot.id)
    assert run.outcome == "complete" and run.revision == 1 and run.catalog_digest
    entities = _entities(stack, run.id)

    endpoint = entities["api_endpoint:app/api.py:GET /users/{user_id}"]
    table = entities["db_table:app/db.py:users"]
    migration = entities[
        "db_table:migrations/versions/0001_users.py:table #1 in migrations/versions/0001_users.py"
    ]  # noqa: E501
    component = entities["component:web/Button.tsx:Button"]
    hook = entities["hook:web/hooks.ts:useToggle"]
    for role in (endpoint, table, migration, component, hook):
        assert role.subject_entity_key and role.subject_entity_key.startswith("symbol:")
    # G4: a partially parsed file keeps facts but its file entity is not generation-eligible.
    broken = entities["file:web/broken.ts:web/broken.ts"]
    assert broken.limitations == ("PARTIAL_PARSE_ARTIFACT",)
    assert broken.context_coverage == "not_eligible"
    assert broken.context_reason == "PARTIAL_PARSE_ARTIFACT" and broken.package_digest is None
    assert entities["symbol:web/broken.ts:ok"].context_coverage == "full"
    # Modules are outlines; small symbols are full packages; everything stored is complete.
    assert entities["file:app/api.py:app/api.py"].context_coverage == "outline"
    assert entities["symbol:app/api.py:load"].context_coverage == "full"
    assert {item.stored_coverage for item in entities.values()} == {"complete"}
    assert entities["symbol:app/api.py:Service.helper"].limitations == ("DYNAMIC_FEATURES",)

    detail = stack.catalog.entity(stack.project.id, snapshot.id, endpoint.entity_key)
    assert detail.integrity_errors == () and detail.freshness_reasons == ()
    assert {fact.verification for fact in [*detail.facts, *detail.neighbor_facts]} == {"verified"}
    match = next(fact for fact in detail.facts if fact.fact_kind == "recognizer_match")
    assert match.derived["strength"] == "pattern_with_local_evidence"
    assert match.derived["attributes"]["path"] == "/users/{user_id}"
    assert "framework_import" in match.derived["met"]
    assert [fact.rendered_text for fact in detail.neighbor_facts] == [
        "function session at app/db.py lines 13–14: def session()"
    ]
    assert all(chunk.verified and chunk.content for chunk in detail.entity.evidence)

    button = stack.catalog.entity(
        stack.project.id, snapshot.id, entities["symbol:web/Button.tsx:Button"].entity_key
    )
    uses = [item for item in button.outgoing if item.kind == "uses_hook"]
    assert [(item.target_entity_key, item.resolution_status) for item in uses] == [
        (hook.entity_key, "imported")
    ]
    page = stack.catalog.entity(
        stack.project.id, snapshot.id, entities["symbol:web/Button.tsx:Page"].entity_key
    )
    assert [(item.kind, item.target_entity_key) for item in page.outgoing] == [
        ("renders", component.entity_key)
    ]
    service = stack.catalog.entity(
        stack.project.id, snapshot.id, entities["symbol:app/api.py:Service"].entity_key
    )
    inherits = next(item for item in service.outgoing if item.kind == "inherits")
    assert inherits.resolution_status == "imported"
    assert inherits.target_entity_key == entities["symbol:app/db.py:User"].entity_key
    resolution = run.coverage["import_resolution"]
    assert isinstance(resolution, dict)
    assert resolution == {"external": 6, "not_attempted": 1, "resolved": 2}

    # Unchanged inputs short-circuit; an explicit reprocess stales the catalog (409 path).
    assert stack.catalog.build(stack.project.id, snapshot.id).id == run.id
    stack.processing.process(stack.project.id, snapshot.id)
    assert stack.catalog.status(stack.project.id, snapshot.id).freshness == "stale"
    with pytest.raises(SemanticCatalogStaleError):
        stack.catalog.entity_page(stack.project.id, snapshot.id, None, EntityFilters(), 10, 0)
    history, reasons, _, _ = stack.catalog.entity_page(
        stack.project.id, snapshot.id, run.id, EntityFilters(), 10, 0
    )
    assert history.id == run.id and "PROCESSING_AUTHORITY_CHANGED" in reasons
    rebuilt = stack.catalog.build(stack.project.id, snapshot.id)
    assert rebuilt.id != run.id and rebuilt.revision == 2
    # Stable identities and chunk keys: the rebuilt catalog reproduces exactly.
    assert rebuilt.catalog_digest == run.catalog_digest
    assert rebuilt.portable_digest == run.portable_digest

    # A second project over the same bytes has different identities but the same portable
    # digest (the cross-database comparison).
    other_root = tmp_path.parent / (tmp_path.name + "-copy")
    shutil.copytree(tmp_path, other_root)
    other = CatalogStack(pg_engine, other_root, name="Copy")
    other_snapshot = other.prepared()
    copy = other.catalog.build(other.project.id, other_snapshot.id)
    assert copy.catalog_digest != run.catalog_digest
    assert copy.portable_digest == run.portable_digest


def test_fact_and_chunk_verification_detect_tampered_rows(
    pg_engine: Engine, tmp_path: Path
) -> None:
    write(tmp_path, REPOSITORY)
    stack = CatalogStack(pg_engine, tmp_path)
    snapshot = stack.prepared()
    run = stack.catalog.build(stack.project.id, snapshot.id)
    entity = _entities(stack, run.id)["symbol:app/api.py:load"]
    assert (
        stack.catalog.entity(stack.project.id, snapshot.id, entity.entity_key).integrity_errors
        == ()
    )
    with stack.factory.begin() as session:
        symbol = session.scalar(
            select(SymbolRecord).where(SymbolRecord.stable_identity_id == entity.symbol_identity_id)
        )
        assert symbol is not None
        session.execute(
            update(SymbolRecord).where(SymbolRecord.id == symbol.id).values(qualified_name="evil")
        )
        session.execute(
            update(ChunkRecord)
            .where(ChunkRecord.symbol_identity_id == entity.symbol_identity_id)
            .values(content=func.upper(ChunkRecord.content))
        )
    # The tampered row is still "current" (no pointer moved); verification must catch it.
    detail = stack.catalog.entity(stack.project.id, snapshot.id, entity.entity_key, run.id)
    signature = next(fact for fact in detail.facts if fact.fact_kind == "signature")
    assert signature.verification == "FACT_DIGEST_MISMATCH"
    assert any(code.startswith("FACT_DIGEST_MISMATCH") for code in detail.integrity_errors)
    assert any(code.startswith("CHUNK_INTEGRITY") for code in detail.integrity_errors)
    assert all(chunk.content is None for chunk in detail.entity.evidence)


def test_legacy_catalog_needs_no_source_reads(
    pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    write(root, {"pkg/__init__.py": "", "pkg/core.py": "import os\n\n\ndef f():\n    return 1\n"})
    stack = CatalogStack(pg_engine, root)
    snapshot = stack.prepared()
    # Pre-Stage-13 runs carry no additive extractor passes.
    with stack.factory.begin() as session:
        session.execute(
            delete(ArtifactExtractionRecord).where(
                ArtifactExtractionRecord.extractor == PYTHON_FACTS
            )
        )
    shutil.rmtree(root)

    from buildlens.infrastructure.structural import supervisor

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("The catalog must not read repository source.")

    monkeypatch.setattr(supervisor.SupervisedSourceReader, "__init__", forbidden)
    monkeypatch.setattr(supervisor.SupervisedSourceReader, "read", forbidden, raising=False)
    run = stack.catalog.build(stack.project.id, snapshot.id)
    entities = _entities(stack, run.id)
    function = entities["symbol:pkg/core.py:f"]
    assert function.limitations == ("FACTS_NOT_EXTRACTED",)
    assert function.stored_coverage == "complete" and function.context_coverage == "full"
    detail = stack.catalog.entity(stack.project.id, snapshot.id, function.entity_key)
    assert detail.integrity_errors == ()
    assert [fact.fact_kind for fact in detail.facts].count("limitation") == 1


def test_superseded_and_failed_runs_never_move_the_pointer(
    pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, REPOSITORY)
    stack = CatalogStack(pg_engine, tmp_path)
    snapshot = stack.prepared()
    first = stack.catalog.build(stack.project.id, snapshot.id)

    # A concurrent builder that captured revision 0 publishes after `first`: superseded.
    from buildlens.application.semantic_catalog import build_catalog

    structural = stack.structure_store.latest_analysis(stack.project.id, snapshot.id)
    processing = stack.processing.current_authority(stack.project.id, snapshot.id)
    assert structural is not None
    build = build_catalog(
        stack.store.load_input(stack.project.id, snapshot.id, structural, processing)
    )
    late = stack.store.publish(
        replace(first, id=uuid4(), captured_active_catalog_run_id=None, captured_latest_revision=0),
        build,
    )
    assert late.superseded and late.revision == 2
    assert stack.catalog.status(stack.project.id, snapshot.id).active_run.id == first.id  # type: ignore[union-attr]

    # Authority changing during the build fails it closed; the attempt is retained.
    original = stack.store.load_input

    def changing(*args: Any) -> Any:
        value = original(*args)
        stack.processing.process(stack.project.id, snapshot.id)
        return value

    monkeypatch.setattr(stack.store, "load_input", changing)
    with stack.factory.begin() as session:
        session.execute(
            text(
                "UPDATE project_versions SET active_semantic_catalog_run_id = NULL WHERE id = :id"
            ),
            {"id": snapshot.id},
        )
    with pytest.raises(SemanticCatalogConflictError):
        stack.catalog.build(stack.project.id, snapshot.id)
    latest = stack.catalog.status(stack.project.id, snapshot.id)
    assert latest.latest_attempt is not None and latest.latest_attempt.outcome == "failed"
    assert latest.latest_attempt.diagnostics == ("CATALOG_AUTHORITY_CHANGED",)
    assert latest.active_run is None
    with stack.factory() as session:
        failed_rows = session.scalar(
            select(func.count())
            .select_from(SemanticEntityRecord)
            .where(SemanticEntityRecord.catalog_run_id == latest.latest_attempt.id)
        )
    assert failed_rows == 0


def test_catalog_http_contracts(pg_engine: Engine, tmp_path: Path) -> None:
    write(tmp_path, REPOSITORY)
    app = create_app(Settings(database_url=pg_engine.url.render_as_string(hide_password=False)))
    with TestClient(app) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "Catalog", "root_path": str(tmp_path)}
        ).json()["id"]
        base = f"/api/v1/projects/{project}/snapshots"
        scope = f"{base}/{client.post(base, json={}).json()['id']}"
        semantic = scope + "/semantic"
        assert client.get(semantic + "/catalog").json()["freshness"] == "none"
        assert client.get(semantic + "/entities").json()["detail"]["code"] == (
            "SEMANTIC_CATALOG_REQUIRED"
        )
        assert client.post(semantic + "/catalog", json={}).json()["detail"]["code"] == (
            "STRUCTURE_UNAVAILABLE"
        )
        assert client.post(scope + "/structure", json={}).status_code == 200
        assert client.post(scope + "/processing", json={}).status_code == 200
        rejected = client.post(
            semantic + "/catalog", content="{}", headers={"Content-Type": "text/plain"}
        )
        assert rejected.status_code == 415
        built = client.post(semantic + "/catalog", json={})
        assert built.status_code == 200, built.text
        assert built.json()["freshness"] == "fresh"
        page = client.get(semantic + "/entities?kind=component&limit=5").json()
        assert page["total"] == 2 and {item["name"] for item in page["items"]} == {"Button", "Page"}
        assert client.get(semantic + "/entities?locator_prefix=web/&q=toggle").json()["total"] == 2
        key = page["items"][0]["entity_key"]
        detail = client.get(semantic + "/entities/" + key)
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["integrity_errors"] == []
        assert {fact["basis"] for fact in body["facts"]} == {
            "parsed",
            "pattern_with_local_evidence",
        }
        assert all(chunk["content"] for chunk in body["evidence"])
        assert client.get(semantic + "/entities/symbol:" + str(uuid4())).status_code == 404
        runs = client.get(semantic + "/catalog/runs").json()
        assert runs["total"] == 1
        assert (
            client.get(semantic + "/catalog/runs/" + runs["items"][0]["run_id"]).status_code == 200
        )
        assert client.get(semantic + "/catalog/runs/" + str(uuid4())).status_code == 404
        assert client.post(scope + "/processing", json={}).status_code == 200
        stale = client.get(semantic + "/entities")
        assert (
            stale.status_code == 409 and stale.json()["detail"]["code"] == "SEMANTIC_CATALOG_STALE"
        )


def test_downgrade_refuses_with_catalog_rows(pg_engine: Engine, tmp_path: Path) -> None:
    write(tmp_path, {"a.py": "def f():\n    return 1\n"})
    stack = CatalogStack(pg_engine, tmp_path)
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "alembic"))
    with pytest.raises(RuntimeError, match="semantic catalog runs exist"):
        command.downgrade(config, "0010_multilanguage_structure")
    with stack.factory() as session:
        assert session.scalar(select(func.count()).select_from(SemanticCatalogRunRecord)) == 1
