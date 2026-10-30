"""Stage 14 Milestone B literal passes on real PostgreSQL: proof obligations P1, P3 and P4.

P1: the literal pass never changes catalog digests or the catalog contract.
P3: processing stays fresh and dependency fingerprints are byte-identical.
P4: a literal-pass failure (fresh or reuse path) never changes artifact or run outcomes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, delete, func, select

from alembic import command
from buildlens.application import structural as structural_module
from buildlens.application.semantic_catalog import build_catalog
from buildlens.application.source_ports import SourceReadError
from buildlens.domain import extractors
from buildlens.domain.semantic import catalog_contract
from buildlens.domain.structural import ArtifactAnalysis, ArtifactExtraction
from buildlens.infrastructure.persistence.models import (
    ArchitectureLiteralRecord,
    ArtifactExtractionRecord,
)
from buildlens.infrastructure.structural import supervisor
from buildlens.infrastructure.structural.reader import PosixSecureSourceReader
from buildlens.infrastructure.structural.worker import SupervisedArtifactAnalyzer
from tests.semantic_support import REPOSITORY, CatalogStack, write

pytestmark = pytest.mark.integration

LITERALS = [extractors.PYTHON_LITERALS, extractors.TS_LITERALS]


def _strip_literals(stack: CatalogStack) -> int:
    """Simulate structural evidence written before Stage 14 (no literal pass rows)."""
    with stack.factory.begin() as session:
        count = session.scalar(select(func.count()).select_from(ArchitectureLiteralRecord)) or 0
        session.execute(delete(ArchitectureLiteralRecord))
        session.execute(
            delete(ArtifactExtractionRecord).where(ArtifactExtractionRecord.extractor.in_(LITERALS))
        )
    return count


def _catalog(stack: CatalogStack, snapshot_id: UUID) -> Any:
    structural = stack.structure_store.latest_analysis(stack.project.id, snapshot_id)
    assert structural is not None
    processing = stack.processing.current_authority(stack.project.id, snapshot_id)
    source = stack.store.load_input(stack.project.id, snapshot_id, structural, processing)
    return build_catalog(source)


def _artifacts(stack: CatalogStack, snapshot_id: UUID) -> list[Any]:
    return stack.structure_store.artifact_page(stack.project.id, snapshot_id, 100, 0, None)[0]


def _literal_passes(stack: CatalogStack, snapshot_id: UUID) -> dict[str, ArtifactExtraction]:
    values: dict[str, ArtifactExtraction] = {}
    for item in _artifacts(stack, snapshot_id):
        with stack.factory() as session:
            for value in stack.structure_store.extractions(session, item.id):
                if value.extractor in LITERALS:
                    values[item.locator] = value
    return values


def test_p1_catalog_is_invariant_to_literal_rows_and_contract(
    pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, REPOSITORY)
    stack = CatalogStack(pg_engine, tmp_path, "P1")
    snapshot = stack.prepared()
    passes = _literal_passes(stack, snapshot.id)
    assert passes["app/api.py"].outcome == "extracted"
    assert any(row.kind == "app_or_router" for row in passes["app/api.py"].literals)
    assert any(
        row.kind == "migration_table"
        for row in passes["migrations/versions/0001_users.py"].literals
    )
    with_literals = _catalog(stack, snapshot.id)
    # P1a: the same structural run with and without literal rows yields an identical catalog.
    assert _strip_literals(stack) > 0
    without = _catalog(stack, snapshot.id)
    assert with_literals.catalog_digest == without.catalog_digest
    assert with_literals.portable_digest == without.portable_digest
    # P1c: the catalog contract never includes the literal contracts.
    before = catalog_contract()
    original = extractors.extractor_contract

    def bumped(extractor: str) -> dict[str, str] | None:
        value = original(extractor)
        if value is not None and extractor in LITERALS:
            value = {**value, "rows": value["rows"] + "+bumped"}
        return value

    monkeypatch.setattr(extractors, "extractor_contract", bumped)
    assert catalog_contract() == before


def test_p1b_p3_new_structural_run_gains_literals_without_disturbing_stage13(
    pg_engine: Engine, tmp_path: Path
) -> None:
    write(tmp_path, REPOSITORY)
    stack = CatalogStack(pg_engine, tmp_path, "P1b")
    snapshot = stack.prepared()
    _strip_literals(stack)
    legacy = stack.catalog.build(stack.project.id, snapshot.id)
    processing_before = stack.processing.process(stack.project.id, snapshot.id)
    fingerprints_before = {
        item.locator: item.dependency_fingerprint for item in processing_before.artifact_processings
    }

    # The upgrade path: an explicit new structural run reuses declarations and adds literals.
    upgraded = stack.structural.analyse(stack.project.id, snapshot.id)
    for item in upgraded.artifact_analyses:
        if item.outcome != "parsed":
            continue
        assert item.execution_mode == "reused", item.locator
        literal = [x for x in item.extractions if x.extractor in LITERALS]
        assert [x.execution_mode for x in literal] == ["extracted"], item.locator
        others = [x for x in item.extractions if x.extractor not in LITERALS]
        assert all(x.execution_mode == "reused" for x in others), item.locator
    assert upgraded.outcome == "complete"

    # P3: processing stays fresh with byte-identical dependency fingerprints.
    status = stack.processing.status(stack.project.id, snapshot.id)
    assert status.active_freshness == "fresh", status.freshness_reasons
    reprocessed = stack.processing.process(stack.project.id, snapshot.id)
    assert {
        item.locator: item.dependency_fingerprint for item in reprocessed.artifact_processings
    } == fingerprints_before

    # P1b: the new catalog run has the same contract and portable digest.
    current = stack.catalog.build(stack.project.id, snapshot.id)
    assert current.id != legacy.id
    assert current.catalog_contract == legacy.catalog_contract
    assert current.portable_digest == legacy.portable_digest

    # Unchanged rescans reuse the literal pass and its rows.
    again = stack.structural.analyse(stack.project.id, snapshot.id)
    api = next(item for item in again.artifact_analyses if item.locator == "app/api.py")
    reused = next(x for x in api.extractions if x.extractor == extractors.PYTHON_LITERALS)
    assert reused.execution_mode == "reused" and reused.literals


class _FailingLiterals(SupervisedArtifactAnalyzer):
    def extract_additional(self, *args: Any, **kwargs: Any) -> Any:
        raise SourceReadError("WORKER_UNAVAILABLE", bytes_consumed=0)


def test_p4_reuse_path_literal_failure_is_confined_to_the_pass(
    pg_engine: Engine, tmp_path: Path
) -> None:
    write(tmp_path, REPOSITORY)
    stack = CatalogStack(pg_engine, tmp_path, "P4 reuse")
    snapshot = stack.prepared()
    first = stack.structure_store.latest_analysis(stack.project.id, snapshot.id)
    assert first is not None
    first_outcomes = {x.locator: x.outcome for x in _artifacts(stack, snapshot.id)}
    _strip_literals(stack)
    service = structural_module.StructuralAnalysisService(stack.structure_store, _FailingLiterals())
    run = service.analyse(stack.project.id, snapshot.id)
    assert run.outcome == first.outcome
    assert {x.locator: x.outcome for x in run.artifact_analyses} == first_outcomes
    api = next(item for item in run.artifact_analyses if item.locator == "app/api.py")
    literal = next(x for x in api.extractions if x.extractor == extractors.PYTHON_LITERALS)
    assert literal.outcome == "failed" and literal.diagnostics[0].code == "WORKER_UNAVAILABLE"
    facts = next(x for x in api.extractions if x.extractor == extractors.PYTHON_FACTS)
    assert facts.outcome == "extracted" and facts.execution_mode == "reused"


def test_p4_fresh_path_literal_failure_is_confined_to_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, {"a.py": "router = APIRouter()\n", "b.ts": "fetch('/x')\n"})

    def broken(*_: Any) -> ArtifactExtraction:
        raise RuntimeError("literal pass crashed")

    monkeypatch.setattr(supervisor, "_python_literals", broken)
    monkeypatch.setattr(supervisor, "_ts_literals", broken)
    reader = PosixSecureSourceReader()
    zero = uuid4()
    for locator, language in (("a.py", "Python"), ("b.ts", "TypeScript")):
        result = cast(
            ArtifactAnalysis,
            supervisor._extract(
                reader,
                (zero, zero, zero, str(tmp_path), locator, 65536, None, language, None),
                [],
            ),
        )
        assert result.outcome == "parsed", locator
        literal = next(x for x in result.extractions if x.extractor in LITERALS)
        assert literal.outcome == "failed"
        assert literal.diagnostics[0].code == "EXTRACTOR_FAILED"
        assert all(x.outcome == "extracted" for x in result.extractions if x is not literal)


def test_run_level_literal_budget_fails_passes_not_artifacts(
    pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(
        tmp_path,
        {
            "a.ts": "fetch('/a')\nfetch('/b')\n",
            "b.ts": "fetch('/c')\nfetch('/d')\n",
        },
    )
    monkeypatch.setattr(structural_module, "ROWS_PER_RUN", 3)
    stack = CatalogStack(pg_engine, tmp_path, "Budget")
    snapshot = stack.snapshots.create(stack.project.id)
    run = stack.structural.analyse(stack.project.id, snapshot.id)
    assert run.outcome == "complete"
    outcomes = {
        item.locator: next(x for x in item.extractions if x.extractor in LITERALS)
        for item in run.artifact_analyses
    }
    assert outcomes["a.ts"].outcome == "extracted" and len(outcomes["a.ts"].literals) == 2
    assert outcomes["b.ts"].outcome == "failed"
    assert outcomes["b.ts"].diagnostics[0].code == "LITERAL_RUN_LIMIT"


def test_downgrade_refuses_while_literal_rows_exist(pg_engine: Engine, tmp_path: Path) -> None:
    write(tmp_path, {"a.py": "router = APIRouter(prefix='/a')\n"})
    stack = CatalogStack(pg_engine, tmp_path, "Downgrade")
    stack.structural.analyse(stack.project.id, stack.snapshots.create(stack.project.id).id)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    with pytest.raises(RuntimeError, match="architecture literal rows exist"):
        command.downgrade(config, "0014_architecture_model")
    _strip_literals(stack)
    command.downgrade(config, "0014_architecture_model")
    command.upgrade(config, "head")


def test_p5a_generation_entries_stay_current_after_gaining_literals(
    pg_engine: Engine, tmp_path: Path
) -> None:
    from tests.generation_support import StubModelServer, generation_service
    from tests.integration.test_semantic_generation import _keys, _plan

    server = StubModelServer()
    try:
        write(tmp_path, REPOSITORY)
        stack = CatalogStack(pg_engine, tmp_path, "P5a")
        snapshot = stack.prepared()
        _strip_literals(stack)  # entries generated over pre-Stage-14 structural evidence
        stack.catalog.build(stack.project.id, snapshot.id)
        service = generation_service(stack, server)
        keys = _keys(stack, snapshot, "get_user", "session")
        plan = _plan(service, stack, snapshot, *keys)
        service.execute(stack.project.id, snapshot.id, plan.id, plan.plan_digest, True)
        _, before = service.entry_views(stack.project.id, snapshot.id, keys)
        assert {view.state for view in before.values()} == {"current"}
        requests = len(server.requests)

        stack.structural.analyse(stack.project.id, snapshot.id)  # gains the literal pass
        stack.processing.process(stack.project.id, snapshot.id)
        stack.catalog.build(stack.project.id, snapshot.id)
        assert _keys(stack, snapshot, "get_user", "session") == keys
        _, after = service.entry_views(stack.project.id, snapshot.id, keys)
        assert {view.state for view in after.values()} == {"current"}
        assert len(server.requests) == requests  # nothing regenerated or contacted
    finally:
        server.close()
