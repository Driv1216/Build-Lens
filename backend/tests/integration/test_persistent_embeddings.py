from __future__ import annotations

import math
from array import array
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from buildlens.application.embeddings import (
    EmbeddingRuntime,
    EmbeddingService,
    EmbeddingUnavailableError,
    ModelCacheStatus,
)
from buildlens.application.fingerprinting import SnapshotFingerprinter
from buildlens.application.processing import ProcessingService
from buildlens.application.snapshots import SnapshotService
from buildlens.domain.embeddings import (
    ChunkEmbedding,
    EmbeddingContract,
    EmbeddingRun,
    EmbeddingVector,
    SemanticSearchFilters,
    embedding_key,
    membership_digest,
)
from buildlens.domain.project_factory import new_project
from buildlens.infrastructure.discovery.filesystem import LocalRepositoryInspector
from buildlens.infrastructure.persistence.database import create_session_factory_from_engine
from buildlens.infrastructure.persistence.embeddings import SqlAlchemyEmbeddingStore
from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore
from buildlens.infrastructure.persistence.repositories import SqlAlchemyProjectRepository
from buildlens.infrastructure.persistence.snapshots import SqlAlchemySnapshotStore
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader

pytestmark = pytest.mark.integration


def _unit(values: list[float]) -> tuple[float, ...]:
    norm = math.sqrt(math.fsum(value * value for value in values))
    return tuple(array("f", (value / norm for value in values)))


def _assert_constraint(
    engine: Engine, statement: str, parameters: dict[str, object], name: str
) -> None:
    with pytest.raises(IntegrityError, match=name):
        with engine.begin() as connection:
            connection.execute(text(statement), parameters)


def test_pgvector_cosine_matches_stage6_float32_oracle(pg_engine: Engine) -> None:
    query = _unit([math.sin(index + 0.25) for index in range(384)])
    documents = [
        query,
        query,
        query,
        tuple(-value for value in query),
        _unit([math.cos(index * 0.01 + 0.5) for index in range(384)]),
    ]
    ids = [UUID(int=value) for value in (3, 1, 2, 4, 5)]
    keys = ["z", "a", "a", "negative", "mixed"]
    with pg_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TEMP TABLE parity (id uuid PRIMARY KEY, stable_key text, embedding vector)"
            )
        )
        for identifier, key, document in zip(ids, keys, documents, strict=True):
            connection.execute(
                text("INSERT INTO parity VALUES (:id, :key, CAST(:embedding AS vector))"),
                {
                    "id": identifier,
                    "key": key,
                    "embedding": str(list(document)),
                },
            )
        connection.execute(
            text("INSERT INTO parity VALUES (:id, 'other-contract', CAST('[1,0]' AS vector))"),
            {"id": UUID(int=6)},
        )
        rows = connection.execute(
            text(
                "SELECT id, 1 - (embedding <=> CAST(:query AS vector)) AS score "
                "FROM parity WHERE vector_dims(embedding)=384 "
                "ORDER BY embedding <=> CAST(:query AS vector), "
                'stable_key COLLATE "C", id'
            ),
            {"query": str(list(query))},
        ).all()
        persisted = connection.scalar(
            text("SELECT embedding::text FROM parity WHERE id=:id"), {"id": ids[0]}
        )
        near_document = tuple(value * 0.99991 for value in query)
        near_query = tuple(value * 1.00009 for value in query)
        near_cosine = float(
            connection.scalar(
                text("SELECT 1 - (CAST(:document AS vector) <=> CAST(:query AS vector))"),
                {"document": str(list(near_document)), "query": str(list(near_query))},
            )
        )
    scores = {identifier: float(score) for identifier, score in rows}
    assert [identifier for identifier, _score in rows[:3]] == [ids[1], ids[2], ids[0]]
    assert scores[ids[3]] == pytest.approx(-1.0, abs=1e-7)
    errors: list[float] = []
    for identifier, document in zip(ids, documents, strict=True):
        high_precision = math.fsum(
            float(x) * float(y) for x, y in zip(document, query, strict=True)
        ) / (
            math.sqrt(math.fsum(float(x) * float(x) for x in document))
            * math.sqrt(math.fsum(float(y) * float(y) for y in query))
        )
        stage6_dot = math.fsum(float(x) * float(y) for x, y in zip(document, query, strict=True))
        errors.extend(
            [abs(scores[identifier] - high_precision), abs(scores[identifier] - stage6_dot)]
        )
    assert max(errors) <= 1e-5
    assert persisted is not None and len(persisted.strip("[]").split(",")) == 384
    near_dot = math.fsum(a * b for a, b in zip(near_document, near_query, strict=True))
    norm_product = math.sqrt(math.fsum(x * x for x in near_document)) * math.sqrt(
        math.fsum(x * x for x in near_query)
    )
    assert abs(near_cosine - near_dot) <= abs(norm_product - 1.0) + 1e-7


class DeterministicProvider:
    contract = EmbeddingContract(
        "test.provider",
        "1",
        "test/model",
        "a" * 40,
        2,
        "test",
        "cpu",
        "float32",
        "empty",
        "empty",
        "chunk-content.v1",
        "query-text.v1",
        256,
        "windows.v1",
        "weighted.v1",
        "l2.v1",
        "5",
        "4",
        "2",
    )

    def __init__(
        self,
        fail_documents: bool = False,
        active_contract: EmbeddingContract | None = None,
    ) -> None:
        self.contract = active_contract or type(self).contract
        self.fail_documents = fail_documents
        self.document_calls: list[list[str]] = []

    def status(self) -> ModelCacheStatus:
        return ModelCacheStatus(True, "/deterministic")

    def embed_documents(self, texts: list[str]) -> list[EmbeddingVector]:
        self.document_calls.append(texts)
        if self.fail_documents:
            raise EmbeddingUnavailableError("document inference must not run")
        return [
            EmbeddingVector((1.0, 0.0), 1)
            if "database" in value.lower()
            else EmbeddingVector((0.0, 1.0), 1)
            for value in texts
        ]

    def embed_query(self, text: str) -> EmbeddingVector:
        return EmbeddingVector((1.0, 0.0), 1)

    def close(self) -> None:
        pass


def test_persistence_restart_exact_search_and_unchanged_reuse(
    pg_engine: Engine, tmp_path: Path
) -> None:
    (tmp_path / "guide.md").write_text(
        "# Database\n\nApply database migrations before startup.\n\n# Other\n\nUnrelated.\n"
    )
    (tmp_path / "notes.txt").write_text("Plain notes.\n")
    factory = create_session_factory_from_engine(pg_engine)
    with factory() as session:
        project = SqlAlchemyProjectRepository(session).add(
            new_project(name="Persistent embeddings", root_path=str(tmp_path))
        )
    snapshots = SnapshotService(
        SqlAlchemySnapshotStore(factory),
        LocalRepositoryInspector(),
        SnapshotFingerprinter(reader=SupervisedSourceReader()),
    )
    processing = ProcessingService(
        SqlAlchemyProcessingStore(factory), reader=SupervisedSourceReader()
    )
    store = SqlAlchemyEmbeddingStore(factory)
    first_snapshot = snapshots.create(project.id)
    processing.process(project.id, first_snapshot.id)
    provider = DeterministicProvider()
    first_service = EmbeddingService(processing, EmbeddingRuntime(provider), store)
    first_run = first_service.prepare(project.id, first_snapshot.id)
    assert first_run.coverage["generated_chunks"] == first_run.chunk_count
    assert first_run.coverage["reused_chunks"] == 0
    with pg_engine.connect() as connection:
        evidence_ids = list(
            connection.scalars(
                text(
                    "SELECT id FROM chunk_embeddings WHERE embedding_run_id=:run "
                    "ORDER BY stable_chunk_key, chunk_id LIMIT 2"
                ),
                {"run": first_run.id},
            )
        )
    assert len(evidence_ids) == 2
    row_id, other_id = evidence_ids
    _assert_constraint(
        pg_engine,
        "UPDATE chunk_embeddings SET embedding=CAST('[2,0]' AS vector) WHERE id=:id",
        {"id": row_id},
        "ck_chunk_embeddings_unit_norm",
    )
    _assert_constraint(
        pg_engine,
        "UPDATE chunk_embeddings SET dimensions=3, embedding=CAST('[1,0,0]' AS vector) "
        "WHERE id=:id",
        {"id": row_id},
        "fk_chunk_embedding_contract_owner",
    )
    _assert_constraint(
        pg_engine,
        "UPDATE chunk_embeddings SET input_hash=:hash WHERE id=:id",
        {"id": row_id, "hash": "b" * 64},
        "fk_chunk_embedding_chunk_identity",
    )
    _assert_constraint(
        pg_engine,
        "UPDATE chunk_embeddings SET execution_mode='reused' WHERE id=:id",
        {"id": row_id},
        "ck_chunk_embeddings_reuse_provenance",
    )
    _assert_constraint(
        pg_engine,
        "UPDATE chunk_embeddings SET execution_mode='reused', "
        "reuse_source_embedding_id=:source WHERE id=:id",
        {"id": row_id, "source": other_id},
        "fk_chunk_embedding_reuse_source",
    )
    _assert_constraint(
        pg_engine,
        "UPDATE chunk_embeddings SET project_id=:project WHERE id=:id",
        {"id": row_id, "project": uuid4()},
        "fk_chunk_embedding_contract_owner",
    )
    _assert_constraint(
        pg_engine,
        "UPDATE chunk_embeddings SET processing_run_id=:processing WHERE id=:id",
        {"id": row_id, "processing": uuid4()},
        "fk_chunk_embedding_complete_owner",
    )
    _assert_constraint(
        pg_engine,
        "INSERT INTO chunk_embeddings "
        "(id, embedding_run_id, project_id, project_version_id, processing_run_id, "
        "artifact_processing_id, chunk_id, stable_chunk_key, input_hash, "
        "logical_embedding_key, contract_digest, dimensions, embedding, window_count, "
        "execution_mode, reuse_source_embedding_id) "
        "SELECT :new_id, embedding_run_id, project_id, project_version_id, processing_run_id, "
        "artifact_processing_id, chunk_id, stable_chunk_key, input_hash, "
        "logical_embedding_key, contract_digest, dimensions, embedding, window_count, "
        "execution_mode, reuse_source_embedding_id FROM chunk_embeddings WHERE id=:id",
        {"new_id": uuid4(), "id": row_id},
        "uq_chunk_embeddings_run_chunk",
    )
    _assert_constraint(
        pg_engine,
        "UPDATE embedding_runs SET processing_revision=999 WHERE id=:id",
        {"id": first_run.id},
        "fk_embedding_run_processing_owner",
    )

    # Two candidates captured against the same publication authority: the first becomes active,
    # while the second is retained with rows as a superseded terminal attempt.
    authority = processing.current_authority(project.id, first_snapshot.id)
    count, _source_bytes = processing.current_content_stats(
        project.id, first_snapshot.id, authority
    )
    chunks = processing.current_content_page(project.id, first_snapshot.id, authority, count, 0)
    logical_keys = [
        embedding_key(first_run.contract.digest, value.stable_key, value.content_hash)
        for value in chunks
    ]
    sources = store.reusable(
        project.id,
        first_snapshot.id,
        logical_keys,
        first_run.contract,
        authority.processing_contract,
    )

    def competing_candidate() -> tuple[EmbeddingRun, list[ChunkEmbedding]]:
        candidate_id, now = uuid4(), datetime.now(UTC)
        candidate_rows = [
            ChunkEmbedding(
                uuid4(),
                candidate_id,
                project.id,
                first_snapshot.id,
                authority.id,
                value.artifact_processing_id,
                value.id,
                value.stable_key,
                value.content_hash,
                logical_key,
                first_run.contract.digest,
                first_run.contract.dimensions,
                sources[logical_key].window_count,
                "reused",
                sources[logical_key].id,
                sources[logical_key].values,
            )
            for value, logical_key in zip(chunks, logical_keys, strict=True)
        ]
        candidate = replace(
            first_run,
            id=candidate_id,
            revision=0,
            started_at=now,
            completed_at=now,
            captured_active_embedding_run_id=first_run.id,
            captured_latest_revision=first_run.revision,
            membership_digest=membership_digest(candidate_rows),
            coverage={
                **first_run.coverage,
                "generated_chunks": 0,
                "reused_chunks": len(candidate_rows),
            },
        )
        return candidate, candidate_rows

    winner_candidate, winner_rows = competing_candidate()
    losing_candidate, losing_rows = competing_candidate()
    winner = store.publish(winner_candidate, winner_rows, authority)
    loser = store.publish(losing_candidate, losing_rows, authority)
    assert not winner.superseded
    assert loser.superseded
    active_after_race = store.active(project.id, first_snapshot.id)
    latest_after_race = store.latest(project.id, first_snapshot.id)
    assert active_after_race is not None and active_after_race.id == winner.id
    assert latest_after_race is not None and latest_after_race.id == loser.id
    first_result = first_service.search(
        project.id,
        first_snapshot.id,
        "database",
        10,
        SemanticSearchFilters(locator_prefix="guide"),
    )
    assert first_result.hits
    assert all((hit.chunk.locator or "").startswith("guide") for hit in first_result.hits)
    assert first_result.hits[0].score == pytest.approx(1.0, abs=1e-7)
    kind_filtered = first_service.search(
        project.id,
        first_snapshot.id,
        "database",
        10,
        SemanticSearchFilters(kind=first_result.hits[0].chunk.kind),
    )
    assert kind_filtered.hits
    assert not first_service.search(
        project.id,
        first_snapshot.id,
        "database",
        10,
        SemanticSearchFilters(locator_prefix="does-not-exist"),
    ).hits
    assert (
        math.fsum(first_result.hits[0].embedding.values or ()) == 0
    )  # Search metadata never hydrates stored vectors.

    # A fresh application runtime can search without document inference.
    restart_provider = DeterministicProvider(fail_documents=True)
    restarted = EmbeddingService(processing, EmbeddingRuntime(restart_provider), store)
    restarted_result = restarted.search(project.id, first_snapshot.id, "database", 3)
    assert restarted_result.hits
    assert restart_provider.document_calls == []

    # Stage 5 assigns new chunk UUIDs on unchanged reuse; Stage 7 copies vectors into new rows.
    second_snapshot = snapshots.create(project.id)
    processing.process(project.id, second_snapshot.id)
    second_run = restarted.prepare(project.id, second_snapshot.id)
    assert second_run.coverage["generated_chunks"] == 0
    assert second_run.coverage["reused_chunks"] == second_run.chunk_count
    rows, total = restarted.rows(project.id, second_snapshot.id, second_run.id, 100, 0)
    assert total == second_run.chunk_count
    assert all(row.execution_mode == "reused" for row in rows)
    assert all(row.reuse_source_embedding_id is not None for row in rows)
    assert all(row.values is None for row in rows)

    history, count = restarted.history(project.id, second_snapshot.id, 20, 0)
    assert count == 1 and history[0].id == second_run.id

    # A selective edit embeds only unmatched logical identities and reuses the rest.
    (tmp_path / "guide.md").write_text(
        "# Database\n\nApply database migrations before deployment.\n\n# Other\n\nUnrelated.\n"
    )
    third_snapshot = snapshots.create(project.id)
    processing.process(project.id, third_snapshot.id)
    changed_provider = DeterministicProvider()
    changed = EmbeddingService(processing, EmbeddingRuntime(changed_provider), store)
    third_run = changed.prepare(project.id, third_snapshot.id)
    inferred = sum(len(batch) for batch in changed_provider.document_calls)
    assert inferred == third_run.coverage["generated_chunks"]
    assert 0 < inferred < third_run.chunk_count
    assert third_run.coverage["reused_chunks"] + inferred == third_run.chunk_count

    # A changed contract cannot reuse old vectors. A handled failure is retained without rows and
    # does not move the active pointer; a successful retry generates the complete new contract.
    changed_contract = replace(DeterministicProvider.contract, model_revision="b" * 40)
    failing_provider = DeterministicProvider(True, changed_contract)
    failing = EmbeddingService(processing, EmbeddingRuntime(failing_provider), store)
    with pytest.raises(EmbeddingUnavailableError):
        failing.prepare(project.id, third_snapshot.id)
    failed_status = failing.status(project.id, third_snapshot.id)
    assert failed_status.active_run is not None
    assert failed_status.active_run.id == third_run.id
    assert failed_status.latest_attempt is not None
    assert failed_status.latest_attempt.outcome == "failed"
    assert failed_status.latest_attempt.chunk_count == 0

    new_provider = DeterministicProvider(active_contract=changed_contract)
    new_contract_service = EmbeddingService(processing, EmbeddingRuntime(new_provider), store)
    changed_contract_run = new_contract_service.prepare(project.id, third_snapshot.id)
    assert changed_contract_run.coverage["generated_chunks"] == changed_contract_run.chunk_count
    assert changed_contract_run.coverage["reused_chunks"] == 0

    # Deleting all eligible files produces a durable, searchable empty run without document
    # inference; its historical predecessors remain retained.
    (tmp_path / "guide.md").unlink()
    (tmp_path / "notes.txt").unlink()
    (tmp_path / "empty.txt").write_text("")
    empty_snapshot = snapshots.create(project.id)
    processing.process(project.id, empty_snapshot.id)
    empty_provider = DeterministicProvider(True, changed_contract)
    empty_service = EmbeddingService(processing, EmbeddingRuntime(empty_provider), store)
    empty_run = empty_service.prepare(project.id, empty_snapshot.id)
    assert empty_run.chunk_count == 0
    assert empty_provider.document_calls == []
    assert empty_service.search(project.id, empty_snapshot.id, "database", 50).hits == ()

    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "alembic"))
    # Stage 13 (0010) refuses first: new processing runs record their processor set.
    with pytest.raises(Exception, match="Stage 13 multi-language evidence exists"):
        command.downgrade(config, "0007_evidence_ownership")
    with pg_engine.begin() as connection:
        connection.execute(text("UPDATE processing_runs SET processor_set = NULL"))
    # Without Stage 13 evidence, the retained-embedding guard still refuses; the whole
    # downgrade is one transaction, so nothing is removed.
    with pytest.raises(Exception, match="Refusing to remove retained embedding history"):
        command.downgrade(config, "0007_evidence_ownership")
    with pg_engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0012_semantic_generation"
        )
