"""Stage 11 uses migrated native evidence; all questions here are synthetic fixtures."""

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from buildlens.api.main import create_app
from buildlens.application.context import RerankerRuntime
from buildlens.application.embeddings import EmbeddingRuntime
from buildlens.application.evaluation import EvaluationRunner, preflight
from buildlens.core.config import Settings
from buildlens.domain.evaluation import (
    CATEGORIES,
    RUBRIC_VERSION,
    SCHEMA_VERSION,
    EvaluationCase,
    EvaluationDataset,
    EvaluationError,
    EvidenceRequirement,
    EvidenceTarget,
    digest,
    excerpt_hash,
)
from buildlens.domain.retrieval import RetrievalFilters
from buildlens.infrastructure.evaluation.corpus import SqlAlchemyEvaluationCorpus
from buildlens.infrastructure.evaluation.local import LocalRunStore, read_json
from buildlens.infrastructure.evaluation.report import report
from buildlens.infrastructure.runtime import ask_service, context_service
from tests.integration.test_retrieval import RetrievalProvider
from tests.unit.test_context import FakeReranker

pytestmark = pytest.mark.integration


def test_native_corpus_runner_reports_and_authority_fence(
    pg_engine: Engine, tmp_path: Path
) -> None:
    (tmp_path / "sample.py").write_text("def customer_record():\n    return 37\n")
    boundary = tmp_path / ".buildlens-eval"
    boundary.mkdir()
    (boundary / "excluded.md").write_text("Synthetic evaluation definitions must be excluded.\n")
    app = create_app(Settings(database_url=pg_engine.url.render_as_string(hide_password=False)))
    with TestClient(app) as client:
        app.state.embedding_runtime = EmbeddingRuntime(RetrievalProvider())
        app.state.reranker_runtime = RerankerRuntime(FakeReranker())
        project = UUID(
            client.post(
                "/api/v1/projects", json={"name": "Evaluation fixture", "root_path": str(tmp_path)}
            ).json()["id"]
        )
        snapshot = UUID(client.post(f"/api/v1/projects/{project}/snapshots", json={}).json()["id"])
        scope = f"/api/v1/projects/{project}/snapshots/{snapshot}"
        for operation in ("structure", "processing", "embeddings"):
            response = client.post(scope + "/" + operation, json={})
            assert response.status_code == 200, response.text
        adapter = SqlAlchemyEvaluationCorpus(app.state.session_factory)
        corpus = adapter.load(project, snapshot)
        app.state.reranker_runtime = RerankerRuntime(
            FakeReranker({c.id: float(i) for i, c in enumerate(corpus.chunks)})
        )
        assert corpus.chunks and len(corpus.chunks) == len(corpus.embeddings)
        assert all(not (c.locator or "").startswith(".buildlens-eval/") for c in corpus.chunks)
        assert any(a.locator == ".buildlens-eval" for a in corpus.artifacts)
        chunk = corpus.chunks[0]
        artifact = next(a for a in corpus.artifacts if a.id == chunk.artifact_id)
        assert artifact.content_fingerprint is not None
        target = EvidenceTarget(
            "fixture",
            "sample.py",
            artifact.content_fingerprint.digest,
            chunk.span.start_offset,
            chunk.span.end_offset,
            excerpt_hash(chunk.content),
        )
        cases = tuple(
            EvaluationCase(
                f"{category}-{index}",
                "customer record",
                category,
                RetrievalFilters(),
                category != "unsupported",
                ("Describe the synthetic declaration.",) if category != "unsupported" else (),
                ("Invented runtime facts.",),
                (target,),
                (EvidenceRequirement("fact", "Synthetic declaration", (("fixture",),)),)
                if category != "unsupported"
                else (),
                (),
            )
            for category in CATEGORIES
            for index in range(6)
        )
        dataset = EvaluationDataset(
            SCHEMA_VERSION,
            "synthetic",
            "1-draft",
            digest(corpus.manifest),
            corpus.manifest,
            "draft",
            None,
            RUBRIC_VERSION,
            cases,
        )
        bindings, invalid = preflight(dataset, corpus)
        assert len(bindings) == 30 and invalid == {}
        run = boundary / "runs/native"
        runner = EvaluationRunner(
            adapter, context_service(app.state), ask_service(app.state), LocalRunStore(run)
        )
        manifest = asyncio.run(
            runner.run(
                dataset, project, snapshot, answers=False, implementation={"test": "synthetic"}
            )
        )
        assert manifest["counts"] == {"success": 90, "error": 0, "invalid": 0}
        assert manifest["comparison_valid"] and manifest["provisional"]
        report(run)
        first_scores = (run / "scores.json").read_bytes()
        report(run)
        assert (run / "scores.json").read_bytes() == first_scores
        record = read_json(run / "cases/identifier-0.hybrid_reranked.json")
        assert record["observations"]["ranking"]
        assert record["observations"]["context"]
        assert record["metrics"]["ranking"]["1"]["recall"] is None

        class ChangingCorpus(SqlAlchemyEvaluationCorpus):
            calls = 0

            def verify(self, frozen: object) -> None:
                self.calls += 1
                if self.calls == 4:
                    with pg_engine.begin() as connection:
                        connection.execute(
                            text(
                                "UPDATE project_versions SET active_embedding_run_id=NULL "
                                "WHERE id=:id"
                            ),
                            {"id": snapshot},
                        )
                super().verify(corpus)

        drift = boundary / "runs/drift"
        drifting = EvaluationRunner(
            ChangingCorpus(app.state.session_factory),
            context_service(app.state),
            ask_service(app.state),
            LocalRunStore(drift),
        )
        changed = asyncio.run(
            drifting.run(
                dataset, project, snapshot, answers=False, implementation={"test": "synthetic"}
            )
        )
        assert not changed["comparison_valid"]
        assert changed["results_written"] == 2
        assert (
            read_json(drift / "cases/identifier-0.hybrid.json")["error_code"]
            == "RUN_AUTHORITY_CHANGED"
        )
        report(drift)
        assert (
            "aggregate scores and paired comparisons are withheld"
            in (drift / "summary.md").read_text()
        )
        with pg_engine.begin() as connection:
            connection.execute(
                text("UPDATE project_versions SET active_embedding_run_id=:run WHERE id=:id"),
                {"run": corpus.embedding.id, "id": snapshot},
            )
            connection.execute(
                text(
                    "UPDATE artifact_processings "
                    "SET locator='.buildlens-eval/contaminated.py' WHERE id=:id"
                ),
                {"id": chunk.artifact_processing_id},
            )
        with pytest.raises(EvaluationError, match="CORPUS_CONTAMINATED"):
            adapter.load(project, snapshot)
        with pytest.raises(EvaluationError, match="CORPUS_MANIFEST"):
            preflight(replace(dataset, corpus_digest="0" * 64), corpus)
