from pathlib import Path

import pytest
from sqlalchemy import Engine

from buildlens.application.fingerprinting import SnapshotFingerprinter
from buildlens.application.snapshots import SnapshotService
from buildlens.application.structural import StructuralAnalysisService
from buildlens.domain.project_factory import new_project
from buildlens.infrastructure.discovery.filesystem import LocalRepositoryInspector
from buildlens.infrastructure.persistence.database import create_session_factory_from_engine
from buildlens.infrastructure.persistence.repositories import SqlAlchemyProjectRepository
from buildlens.infrastructure.persistence.snapshots import SqlAlchemySnapshotStore
from buildlens.infrastructure.persistence.structural import SqlAlchemyStructuralAnalysisStore
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
from buildlens.infrastructure.structural.worker import SupervisedPythonAnalyzer

pytestmark = pytest.mark.integration


def test_structural_persistence_and_unchanged_rescan(pg_engine: Engine, tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    factory = create_session_factory_from_engine(pg_engine)
    with factory() as session:
        project = SqlAlchemyProjectRepository(session).add(
            new_project(name="Audit", root_path=str(tmp_path))
        )
    snapshots = SnapshotService(
        SqlAlchemySnapshotStore(factory),
        LocalRepositoryInspector(),
        SnapshotFingerprinter(reader=SupervisedSourceReader()),
    )
    structural = StructuralAnalysisService(
        SqlAlchemyStructuralAnalysisStore(factory), SupervisedPythonAnalyzer()
    )
    first = snapshots.create(project.id)
    observed = structural.analyse(project.id, first.id)
    assert observed.revision == 1
    assert observed.summary["symbols"] == 1
    second = snapshots.create(project.id)
    reused = structural.analyse(project.id, second.id)
    assert reused.artifact_analyses[0].execution_mode == "reused"
    artifacts, _ = snapshots._store.artifacts(project.id, second.id, 100, 0)
    assert reused.artifact_analyses[0].artifact_id == artifacts[0].id
    assert (
        reused.artifact_analyses[0].symbols[0].stable_identity_id
        == observed.artifact_analyses[0].symbols[0].stable_identity_id
    )


def test_processing_reuse_freshness_and_failed_retry(pg_engine: Engine, tmp_path: Path) -> None:
    from buildlens.application.processing import ProcessingService
    from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore

    source = tmp_path / "sample.py"
    source.write_text("value = 1\n\ndef sample():\n    return value\n")
    factory = create_session_factory_from_engine(pg_engine)
    with factory() as session:
        project = SqlAlchemyProjectRepository(session).add(
            new_project(name="Processing", root_path=str(tmp_path))
        )
    snapshots = SnapshotService(
        SqlAlchemySnapshotStore(factory),
        LocalRepositoryInspector(),
        SnapshotFingerprinter(reader=SupervisedSourceReader()),
    )
    structural = StructuralAnalysisService(
        SqlAlchemyStructuralAnalysisStore(factory), SupervisedPythonAnalyzer()
    )
    processing = ProcessingService(
        SqlAlchemyProcessingStore(factory), reader=SupervisedSourceReader()
    )
    first = snapshots.create(project.id)
    structural.analyse(project.id, first.id)
    run = processing.process(project.id, first.id)
    assert run.outcome == "complete"
    assert run.revision == 1
    assert run.coverage["chunks"] > 0
    structural.analyse(project.id, first.id)
    assert processing.status(project.id, first.id).active_freshness == "fresh"
    second = snapshots.create(project.id)
    structural.analyse(project.id, second.id)
    reused = processing.process(project.id, second.id)
    assert reused.artifact_processings[0].execution_mode == "reused"
    assert [c.stable_key for c in reused.artifact_processings[0].chunks] == [
        c.stable_key for c in run.artifact_processings[0].chunks
    ]
    # Invalidate cached contracts so retry must actually read missing bytes.
    from sqlalchemy import update

    from buildlens.infrastructure.persistence.models import ProcessingRunRecord

    with factory.begin() as session:
        session.execute(update(ProcessingRunRecord).values(processing_contract={"invalid": True}))
    source.unlink()
    failed = processing.process(project.id, second.id)
    assert failed.outcome == "failed"
    status = processing.status(project.id, second.id)
    assert status.active_run is not None and status.active_run.id == reused.id
    assert any(
        d.code == "SOURCE_NOT_FOUND" for a in failed.artifact_processings for d in a.diagnostics
    )


def test_embedding_content_reads_are_scoped_bounded_and_batched(
    pg_engine: Engine, tmp_path: Path
) -> None:
    from sqlalchemy import event

    from buildlens.application.processing import ProcessingService
    from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore

    (tmp_path / "guide.md").write_text("# Guide\n\nApply database migrations before startup.\n")
    factory = create_session_factory_from_engine(pg_engine)
    with factory() as session:
        project = SqlAlchemyProjectRepository(session).add(
            new_project(name="Embedding reads", root_path=str(tmp_path))
        )
    snapshot = SnapshotService(
        SqlAlchemySnapshotStore(factory),
        LocalRepositoryInspector(),
        SnapshotFingerprinter(reader=SupervisedSourceReader()),
    ).create(project.id)
    store = SqlAlchemyProcessingStore(factory)
    processing = ProcessingService(store, reader=SupervisedSourceReader())
    run = processing.process(project.id, snapshot.id)
    authority = processing.current_authority(project.id, snapshot.id)

    captured: list[str] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        captured.append(statement)

    event.listen(pg_engine, "before_cursor_execute", capture)
    try:
        count, source_bytes = store.content_stats(project.id, snapshot.id, authority.id)
        page = store.page_chunk_content(project.id, snapshot.id, authority.id, 128, 0)
        selected = store.chunks_by_id(
            project.id, snapshot.id, authority.id, [value.id for value in reversed(page)]
        )
    finally:
        event.remove(pg_engine, "before_cursor_execute", capture)

    assert len(captured) == 3
    assert count == run.coverage["chunks"] == len(page)
    assert source_bytes == sum(len(value.content.encode("utf-8")) for value in page)
    assert [value.id for value in selected] == [value.id for value in reversed(page)]
    assert all(
        value.run_id == authority.id
        and value.snapshot_id == snapshot.id
        and value.artifact_id is not None
        and value.locator == "guide.md"
        for value in page
    )


def test_competing_publications_and_rollback(pg_engine: Engine, tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Barrier
    from uuid import uuid4

    from sqlalchemy.exc import IntegrityError

    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    factory = create_session_factory_from_engine(pg_engine)
    with factory() as session:
        project = SqlAlchemyProjectRepository(session).add(
            new_project(name="Concurrent", root_path=str(tmp_path))
        )
    snapshots = SnapshotService(
        SqlAlchemySnapshotStore(factory),
        LocalRepositoryInspector(),
        SnapshotFingerprinter(reader=SupervisedSourceReader()),
    )
    store = SqlAlchemyStructuralAnalysisStore(factory)
    service = StructuralAnalysisService(store, SupervisedPythonAnalyzer())
    snapshot = snapshots.create(project.id)
    initial = service.analyse(project.id, snapshot.id)
    barrier = Barrier(2)

    def publish() -> object:
        attempt = replace(
            initial,
            id=uuid4(),
            artifact_analyses=(),
            summary_data=None,
            captured_active_analysis_id=initial.id,
            captured_latest_revision=initial.revision,
        )
        barrier.wait(timeout=5)
        return store.save_replacement(project.id, snapshot.id, attempt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(), range(2)))
    from buildlens.domain.structural import StructuralAnalysis

    assert all(isinstance(x, StructuralAnalysis) for x in results)
    latest, total = store.history(project.id, snapshot.id, 10, 0)
    assert total == 3 and sorted(x.revision for x in latest) == [1, 2, 3]
    assert sum(x.superseded for x in latest) == 1
    active = store.latest_analysis(project.id, snapshot.id)
    assert active is not None and active.revision == 2
    broken = service._owned(initial.artifact_analyses[0], uuid4(), snapshot.id)
    broken = replace(broken, symbols=(replace(broken.symbols[0], parent_id=uuid4()),))
    attempt = replace(
        initial,
        id=broken.structural_analysis_id,
        artifact_analyses=(broken,),
        summary_data=None,
        captured_active_analysis_id=active.id,
        captured_latest_revision=3,
    )
    with pytest.raises(IntegrityError):
        store.save_replacement(project.id, snapshot.id, attempt)
    retained = store.latest_analysis(project.id, snapshot.id)
    assert retained is not None and retained.id == active.id
    assert store.history(project.id, snapshot.id, 10, 0)[1] == 3


def test_run_chunk_limits_keep_completed_artifacts(pg_engine: Engine, tmp_path: Path) -> None:
    from buildlens.application.processing import ProcessingService
    from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "a.json").write_text('{"one":1,"two":2}')
    (tmp_path / "b.json").write_text('{"one":1,"two":2}')
    factory = create_session_factory_from_engine(pg_engine)
    with factory() as session:
        project = SqlAlchemyProjectRepository(session).add(
            new_project(name="Limits", root_path=str(tmp_path))
        )
    snapshot = SnapshotService(
        SqlAlchemySnapshotStore(factory),
        LocalRepositoryInspector(),
        SnapshotFingerprinter(reader=SupervisedSourceReader()),
    ).create(project.id)
    processing = ProcessingService(
        SqlAlchemyProcessingStore(factory),
        reader=SupervisedSourceReader(),
        max_artifacts=2,
        max_chunks_per_artifact=2,
        max_chunks=3,
    )
    result = processing.process(project.id, snapshot.id)
    by_path = {x.locator: x for x in result.artifact_processings}
    assert result.outcome == "partial" and result.coverage["chunks"] == 2
    assert len(by_path["a.json"].chunks) == 2
    assert by_path["b.json"].outcome == "skipped" and not by_path["b.json"].chunks
    assert by_path["b.json"].diagnostics[0].code == "CHUNK_LIMIT"
    assert by_path["node_modules"].outcome == "excluded"


def test_selective_update_ambiguous_slots_and_contract_changes(
    pg_engine: Engine, tmp_path: Path
) -> None:
    from sqlalchemy import update

    from buildlens.application.processing import ProcessingService
    from buildlens.infrastructure.persistence.models import (
        ProcessingRunRecord,
        StructuralAnalysisRecord,
    )
    from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore

    source = tmp_path / "a.py"
    source.write_text("def stable():\n    return 1\n\ndef duplicate():\n    pass\n")
    (tmp_path / "b.py").write_text("def untouched():\n    pass\n")
    factory = create_session_factory_from_engine(pg_engine)
    with factory() as session:
        project = SqlAlchemyProjectRepository(session).add(
            new_project(name="Continuity", root_path=str(tmp_path))
        )
    snapshots = SnapshotService(
        SqlAlchemySnapshotStore(factory),
        LocalRepositoryInspector(),
        SnapshotFingerprinter(reader=SupervisedSourceReader()),
    )
    structure = StructuralAnalysisService(
        SqlAlchemyStructuralAnalysisStore(factory), SupervisedPythonAnalyzer()
    )
    processing = ProcessingService(
        SqlAlchemyProcessingStore(factory), reader=SupervisedSourceReader()
    )
    first = snapshots.create(project.id)
    original = structure.analyse(project.id, first.id)
    original_processing = processing.process(project.id, first.id)
    original_symbols = {
        s.name: s.stable_identity_id for a in original.artifact_analyses for s in a.symbols
    }
    source.write_text(
        "def stable():\n    return 2\n\ndef duplicate():\n    pass\n"
        "\ndef duplicate():\n    pass\n\ndef added():\n    pass\n"
    )
    second = snapshots.create(project.id)
    updated = structure.analyse(project.id, second.id)
    by_path = {a.locator: a for a in updated.artifact_analyses}
    assert by_path["a.py"].execution_mode == "parsed"
    assert by_path["b.py"].execution_mode == "reused"
    symbols = by_path["a.py"].symbols
    assert (
        next(s.stable_identity_id for s in symbols if s.name == "stable")
        == original_symbols["stable"]
    )
    duplicates = [s.stable_identity_id for s in symbols if s.name == "duplicate"]
    assert len(set(duplicates)) == 2 and original_symbols["duplicate"] not in duplicates
    assert by_path["b.py"].symbols[0].stable_identity_id == original_symbols["untouched"]
    processed = processing.process(project.id, second.id)
    assert {a.locator: a.execution_mode for a in processed.artifact_processings} == {
        "a.py": "processed",
        "b.py": "reused",
    }
    before_keys = {
        a.locator: [c.stable_key for c in a.chunks]
        for a in original_processing.artifact_processings
    }
    assert (
        next(
            [c.stable_key for c in a.chunks]
            for a in processed.artifact_processings
            if a.locator == "b.py"
        )
        == before_keys["b.py"]
    )
    repeated = structure.analyse(project.id, second.id)
    assert [s.stable_identity_id for a in repeated.artifact_analyses for s in a.symbols] == [
        s.stable_identity_id for a in updated.artifact_analyses for s in a.symbols
    ]
    # A producer change invalidates current and predecessor caches, including identity inference.
    with factory.begin() as session:
        session.execute(
            update(StructuralAnalysisRecord).values(producer_contract={"runtime": "old"})
        )
        session.execute(
            update(ProcessingRunRecord).values(processing_contract={"screening": "old"})
        )
    reparsed = structure.analyse(project.id, second.id)
    assert all(a.execution_mode == "parsed" for a in reparsed.artifact_analyses)
    regenerated = processing.process(project.id, second.id)
    assert all(a.execution_mode == "processed" for a in regenerated.artifact_processings)


def test_processing_competing_publications(pg_engine: Engine, tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Barrier
    from uuid import uuid4

    from buildlens.application.processing import ProcessingService
    from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore

    (tmp_path / "a.md").write_text("# A\n\nContent\n")
    factory = create_session_factory_from_engine(pg_engine)
    with factory() as session:
        project = SqlAlchemyProjectRepository(session).add(
            new_project(name="Publication", root_path=str(tmp_path))
        )
    snapshot = SnapshotService(
        SqlAlchemySnapshotStore(factory),
        LocalRepositoryInspector(),
        SnapshotFingerprinter(reader=SupervisedSourceReader()),
    ).create(project.id)
    store = SqlAlchemyProcessingStore(factory)
    initial = ProcessingService(store, reader=SupervisedSourceReader()).process(
        project.id, snapshot.id
    )
    barrier = Barrier(2)

    def publish() -> object:
        run_id, artifact_id = uuid4(), uuid4()
        artifact = initial.artifact_processings[0]
        artifact = replace(
            artifact,
            id=artifact_id,
            processing_run_id=run_id,
            chunks=tuple(
                replace(c, id=uuid4(), artifact_processing_id=artifact_id) for c in artifact.chunks
            ),
        )
        attempt = replace(
            initial,
            id=run_id,
            artifact_processings=(artifact,),
            captured_active_processing_run_id=initial.id,
            captured_latest_revision=initial.revision,
        )
        barrier.wait(timeout=5)
        return store.save(project.id, snapshot.id, attempt, True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: publish(), range(2)))
    latest = store.latest(project.id, snapshot.id)
    active = store.active(project.id, snapshot.id)
    assert latest is not None and latest.revision == 3 and latest.superseded
    assert active is not None and active.revision == 2 and not active.superseded
