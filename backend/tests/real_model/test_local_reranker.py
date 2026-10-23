import math
import os
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from buildlens.api.main import create_app
from buildlens.application.context import RerankerRuntime
from buildlens.core.config import Settings
from buildlens.domain.processing import Chunk, ChunkSpan
from buildlens.infrastructure.reranking.supervisor import SupervisedCrossEncoder


def _chunk(identifier: int, content: str) -> Chunk:
    return Chunk(
        UUID(int=identifier),
        UUID(int=100),
        f"key-{identifier}",
        identifier,
        f"unit-{identifier}",
        0,
        "text",
        content,
        "0" * 64,
        ChunkSpan(0, len(content), 1, 1, 0, len(content)),
    )


@pytest.mark.real_model
def test_pinned_reranker_runs_offline_with_batches_and_tail_windows() -> None:
    cache = Path(os.environ["BUILDLENS_RERANKER_CACHE_DIR"])
    provider = SupervisedCrossEncoder(cache)
    try:
        assert provider.status().available
        passages = [
            _chunk(1, "A puppy plays with a ball in the garden."),
            _chunk(2, "Apply database schema migrations before starting the API."),
            _chunk(3, "The ocean contains many species of fish."),
            *[_chunk(index, f"Unrelated fixture passage {index}.") for index in range(4, 11)],
        ]
        values = provider.score("How should I update the database schema?", passages, 45)
        assert len(values) == 10
        assert [item.chunk_id for item in values] == [item.id for item in passages]
        assert all(item.score is not None and math.isfinite(item.score) for item in values)
        assert (
            max(
                values, key=lambda item: item.score if item.score is not None else -math.inf
            ).chunk_id
            == passages[1].id
        )

        repeated = provider.score("How should I update the database schema?", [passages[1]], 45)[0]
        assert repeated.score == pytest.approx(values[1].score, abs=1e-6)

        long_tail = _chunk(
            20, "garden " * 600 + "apply the database schema migration before startup"
        )
        tail = provider.score("database schema migration", [long_tail], 45)[0]
        assert tail.window_count > 1
        assert tail.winning_window == tail.window_count - 1
    finally:
        provider.close()


@pytest.mark.real_model
@pytest.mark.integration
def test_real_reranker_builds_postgresql_backed_context(pg_engine: Engine, tmp_path: Path) -> None:
    (tmp_path / "migration.py").write_text(
        "def apply_schema_migration():\n"
        '    """Apply the database schema migration before API startup."""\n'
        "    return True\n"
    )
    provider = SupervisedCrossEncoder(Path(os.environ["BUILDLENS_RERANKER_CACHE_DIR"]))
    app = create_app(Settings(database_url=pg_engine.url.render_as_string(hide_password=False)))
    with TestClient(app) as client:
        app.state.reranker_runtime.close()
        app.state.reranker_runtime = RerankerRuntime(provider)
        project_id = client.post(
            "/api/v1/projects", json={"name": "Reranker", "root_path": str(tmp_path)}
        ).json()["id"]
        snapshot_id = client.post(f"/api/v1/projects/{project_id}/snapshots", json={}).json()["id"]
        scope = f"/api/v1/projects/{project_id}/snapshots/{snapshot_id}"
        assert client.post(scope + "/structure", json={}).status_code == 200
        assert client.post(scope + "/processing", json={}).status_code == 200
        response = client.post(
            scope + "/context",
            json={
                "retrieval_mode": "lexical",
                "ranking_policy": "local_cross_encoder",
                "query": "database schema migration",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reranker_state"] == "completed"
        assert body["items"]
        assert all(
            item["rerank_score"] is not None and math.isfinite(item["rerank_score"])
            for item in body["items"]
            if item["selected_as_seed"]
        )
        assert body["budget"]["used"] <= body["budget"]["limit"]
