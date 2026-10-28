"""Stage 14 C-1 integrity: tampering is detected and authoritative queries fail closed."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import IntegrityError

from buildlens.application.architecture import (
    ArchitectureBudgetError,
    ArchitectureEvidenceInvalidError,
    ArchitectureGraphCache,
    ArchitectureIntegrityError,
    ArchitectureService,
)
from buildlens.application.architecture_queries import ArchitectureQueries
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from buildlens.infrastructure.persistence.models import (
    ArchitectureEdgeRecord,
    ArchitectureEvidenceLinkRecord,
    ImportReferenceRecord,
    SemanticFactRecord,
)
from tests.integration.test_architecture import prepared
from tests.semantic_support import CatalogStack

pytestmark = pytest.mark.integration


def _setup(
    engine: Engine, root: Path
) -> tuple[Any, Any, ArchitectureService, ArchitectureQueries, Any]:
    stack, snapshot = prepared(engine, root)
    service = ArchitectureService(
        SqlAlchemyArchitectureStore(stack.factory), stack.catalog, cache=ArchitectureGraphCache()
    )
    run = service.build(stack.project.id, snapshot.id)
    return stack, snapshot, service, ArchitectureQueries(service), run


def _path(queries: ArchitectureQueries, stack: Any, snapshot: Any) -> Any:
    return queries.path(
        stack.project.id, snapshot.id, None, "module:app/api.py", "module:app/db.py", class_set="S4"
    )


def _edge(stack: CatalogStack, run: Any, key: str) -> ArchitectureEdgeRecord:
    with stack.factory() as session:
        record = session.scalar(
            select(ArchitectureEdgeRecord).where(
                ArchitectureEdgeRecord.architecture_run_id == run.id,
                ArchitectureEdgeRecord.edge_key == key,
            )
        )
        assert record is not None
        session.expunge(record)
        return cast(ArchitectureEdgeRecord, record)


API_DB = "imports|module:app/api.py|module:app/db.py|resolved|presumed_unconditional"


def test_queries_answer_with_verified_paths(pg_engine: Engine, tmp_path: Path) -> None:
    stack, snapshot, service, queries, run = _setup(pg_engine, tmp_path)
    found = _path(queries, stack, snapshot)
    assert found.status == "found" and [e.key for e in found.path] == [API_DB]
    missing = queries.path(
        stack.project.id, snapshot.id, None, "module:app/db.py", "module:app/api.py"
    )
    # A complete search over a graph with known gaps is never reported as plain absence.
    assert missing.status == "not_found_in_observed_graph"
    assert "OPAQUE_DYNAMIC_IMPORTS_NOT_OBSERVABLE" in missing.gaps.run_gaps
    deps = queries.dependencies(
        stack.project.id, snapshot.id, None, "container:web", class_set="S4"
    )
    assert deps.search == "complete" and "module:web/Button.tsx" in deps.origins
    overview = queries.overview(stack.project.id, snapshot.id, None)
    assert overview.node_counts["module"] >= 8
    detail = queries.node(stack.project.id, snapshot.id, None, "module:app/api.py")
    assert any(edge.key == API_DB for edge in detail.outgoing)
    assert service.integrity(stack.project.id, snapshot.id, None)["structure"] == "verified"


@pytest.mark.parametrize(
    "change",
    [{"tier": "inferred"}, {"attributes": {"forged": True}}, {"conditions": ["FORGED"]}],
)
def test_edited_edge_semantics_fail_closed(
    pg_engine: Engine, tmp_path: Path, change: dict[str, Any]
) -> None:
    stack, snapshot, service, queries, run = _setup(pg_engine, tmp_path)
    _path(queries, stack, snapshot)  # warm the cache
    with stack.factory.begin() as session:
        session.execute(
            update(ArchitectureEdgeRecord)
            .where(
                ArchitectureEdgeRecord.architecture_run_id == run.id,
                ArchitectureEdgeRecord.edge_key == API_DB,
            )
            .values(**change)
        )
    with pytest.raises(ArchitectureIntegrityError):
        _path(queries, stack, snapshot)
    report = service.integrity(stack.project.id, snapshot.id, None)
    assert report["structure"] == "failed"


def test_cross_run_and_dangling_references_are_refused(pg_engine: Engine, tmp_path: Path) -> None:
    stack, snapshot, _, _, run = _setup(pg_engine, tmp_path)
    record = _edge(stack, run, API_DB)
    with pytest.raises(IntegrityError), stack.factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO architecture_edges (architecture_run_id, ordinal, edge_key, kind, "
                "source_key, target_key, tier, count, attributes, conditions, edge_digest) VALUES "
                "(:run, 99999, 'forged', 'imports', 'module:not-in-this-run', :target, "
                "'derived', 1, '{}', '[]', :digest)"
            ),
            {"run": run.id, "target": record.target_key, "digest": record.edge_digest},
        )
    with pytest.raises(IntegrityError), stack.factory.begin() as session:
        session.execute(
            update(ArchitectureEvidenceLinkRecord)
            .where(
                ArchitectureEvidenceLinkRecord.architecture_run_id == run.id,
                ArchitectureEvidenceLinkRecord.edge_ordinal == record.ordinal,
                ArchitectureEvidenceLinkRecord.evidence_digest.is_not(None),
            )
            .values(evidence_digest="f" * 64)
        )


@pytest.mark.parametrize("mode", ["delete", "retarget"])
def test_evidence_link_changes_are_detected(pg_engine: Engine, tmp_path: Path, mode: str) -> None:
    stack, snapshot, _, queries, run = _setup(pg_engine, tmp_path)
    record = _edge(stack, run, API_DB)
    with stack.factory.begin() as session:
        link = session.scalar(
            select(ArchitectureEvidenceLinkRecord).where(
                ArchitectureEvidenceLinkRecord.architecture_run_id == run.id,
                ArchitectureEvidenceLinkRecord.edge_ordinal == record.ordinal,
                ArchitectureEvidenceLinkRecord.catalog_fact_digest.is_not(None),
            )
        )
        assert link is not None
        if mode == "delete":
            session.delete(link)
        else:
            other = session.scalar(
                select(SemanticFactRecord.fact_digest).where(
                    SemanticFactRecord.catalog_run_id == run.catalog_run_id,
                    SemanticFactRecord.fact_digest != link.catalog_fact_digest,
                )
            )
            link.catalog_fact_digest = other
    with pytest.raises(ArchitectureIntegrityError):
        _path(queries, stack, snapshot)


def test_tampered_cited_rows_fail_closed(pg_engine: Engine, tmp_path: Path) -> None:
    stack, snapshot, service, queries, run = _setup(pg_engine, tmp_path)
    _path(queries, stack, snapshot)
    with stack.factory.begin() as session:
        session.execute(
            update(ImportReferenceRecord)
            .where(ImportReferenceRecord.from_module == "db")
            .values(start_line=ImportReferenceRecord.start_line + 1)
        )
    with pytest.raises(ArchitectureEvidenceInvalidError) as error:
        _path(queries, stack, snapshot)
    assert error.value.failures
    report = service.integrity(stack.project.id, snapshot.id, None)
    assert report["evidence_failure_count"]


def test_cache_detects_digest_changes_and_untouched_content_edits(
    pg_engine: Engine, tmp_path: Path
) -> None:
    stack, snapshot, service, queries, run = _setup(pg_engine, tmp_path)
    _path(queries, stack, snapshot)
    with stack.factory.begin() as session:
        session.execute(
            update(ArchitectureEdgeRecord)
            .where(
                ArchitectureEdgeRecord.architecture_run_id == run.id,
                ArchitectureEdgeRecord.edge_key != API_DB,
                ArchitectureEdgeRecord.kind == "imports",
            )
            .values(edge_digest="0" * 64)
        )
    with pytest.raises(ArchitectureIntegrityError):
        _path(queries, stack, snapshot)
    # A fresh service and cache: content-only edits to untouched rows are found by /integrity.
    clean_stack, clean_snapshot, clean, clean_queries, clean_run = _setup(
        pg_engine, tmp_path / "clean"
    )
    _path(clean_queries, clean_stack, clean_snapshot)
    with clean_stack.factory.begin() as session:
        session.execute(
            update(ArchitectureEdgeRecord)
            .where(
                ArchitectureEdgeRecord.architecture_run_id == clean_run.id,
                ArchitectureEdgeRecord.edge_key.like("imports|module:web/%"),
            )
            .values(count=ArchitectureEdgeRecord.count + 1)
        )
    answer = _path(clean_queries, clean_stack, clean_snapshot)
    assert answer.status == "found"  # answered for the published run's verified content
    assert clean.integrity(clean_stack.project.id, clean_snapshot.id, None)["structure"] == "failed"


def test_verification_budget_fails_closed(
    pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack, snapshot, _, queries, _ = _setup(pg_engine, tmp_path)
    monkeypatch.setattr("buildlens.application.architecture.VERIFICATION_BUDGET", 1)
    with pytest.raises(ArchitectureBudgetError):
        queries.overview(stack.project.id, snapshot.id, None)
