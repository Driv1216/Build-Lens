from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from buildlens.api.main import create_app
from buildlens.application.embeddings import EmbeddingRuntime, ModelCacheStatus
from buildlens.core.config import Settings
from buildlens.domain.embeddings import EmbeddingContract, EmbeddingVector

pytestmark = pytest.mark.integration


class RetrievalProvider:
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

    def __init__(self) -> None:
        self.document_calls = 0
        self.query_calls = 0

    def status(self) -> ModelCacheStatus:
        return ModelCacheStatus(True, "/test")

    def embed_documents(self, texts: list[str]) -> list[EmbeddingVector]:
        self.document_calls += 1
        return [
            EmbeddingVector((1.0, 0.0), 1)
            if "customer" in value
            else EmbeddingVector((0.0, 1.0), 1)
            for value in texts
        ]

    def embed_query(self, text: str) -> EmbeddingVector:
        self.query_calls += 1
        return EmbeddingVector((1.0, 0.0), 1)

    def close(self) -> None:
        pass


def test_stage8_normalization_function_and_indexes(pg_engine: Engine) -> None:
    with pg_engine.connect() as connection:
        normalized = connection.scalar(
            text("SELECT public.buildlens_lexical_normalize_v1(:value)"),
            {"value": r"HTTPServer.getUser src\api-v1"},
        )
        properties = connection.execute(
            text(
                "SELECT provolatile, proparallel, proisstrict FROM pg_proc "
                "WHERE oid='public.buildlens_lexical_normalize_v1(text)'::regprocedure"
            )
        ).one()
        indexes = set(
            connection.scalars(
                text("SELECT indexname FROM pg_indexes WHERE indexname = ANY(:names)"),
                {
                    "names": [
                        "ix_chunks_lexical_content_v1",
                        "ix_symbols_lexical_qualified_name_v1",
                        "ix_artifact_processings_lexical_locator_v1",
                    ]
                },
            )
        )
    assert normalized == "HTTP Server get User src api v1"
    assert properties == ("i", "s", True)
    assert indexes == {
        "ix_chunks_lexical_content_v1",
        "ix_symbols_lexical_qualified_name_v1",
        "ix_artifact_processings_lexical_locator_v1",
    }


def test_exact_and_lexical_api_need_no_embeddings(pg_engine: Engine, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "class HTTPServer:\n    def getUser(self):\n        return 'customer record'\n"
    )
    app = create_app(Settings(database_url=pg_engine.url.render_as_string(hide_password=False)))
    with TestClient(app) as client:
        provider = RetrievalProvider()
        app.state.embedding_runtime = EmbeddingRuntime(provider)
        project = client.post(
            "/api/v1/projects", json={"name": "Retrieval", "root_path": str(tmp_path)}
        ).json()["id"]
        snapshot = client.post(f"/api/v1/projects/{project}/snapshots", json={}).json()["id"]
        scope = f"/api/v1/projects/{project}/snapshots/{snapshot}"
        assert client.post(scope + "/structure", json={}).status_code == 200
        assert client.post(scope + "/processing", json={}).status_code == 200

        exact = client.post(scope + "/search", json={"mode": "exact", "query": "getUser"})
        assert exact.status_code == 200, exact.text
        exact_body = exact.json()
        assert exact_body["embedding_run"] is None
        assert exact_body["components"]["semantic"]["state"] == "not_requested"
        assert exact_body["hits"]
        assert exact_body["hits"][0]["score"] is None
        assert exact_body["hits"][0]["exact_matches"] == [
            {"field": "symbol_name", "value": "getUser"}
        ]

        lexical = client.post(scope + "/search", json={"mode": "lexical", "query": "get user"})
        assert lexical.status_code == 200, lexical.text
        lexical_body = lexical.json()
        assert lexical_body["hits"]
        assert lexical_body["hits"][0]["lexical"]["field_scores"]
        assert lexical_body["components"]["semantic"]["state"] == "not_requested"
        assert provider.document_calls == provider.query_calls == 0

        path = client.post(scope + "/search", json={"mode": "exact", "query": "a.py"}).json()
        assert len(path["hits"]) >= 2
        assert all(hit["locator"] == "a.py" for hit in path["hits"])

        tokenless = client.post(scope + "/search", json={"mode": "lexical", "query": "___"})
        assert tokenless.status_code == 200
        assert tokenless.json()["hits"] == []

        prepared = client.post(scope + "/embeddings", json={})
        assert prepared.status_code == 200, prepared.text
        hybrid = client.post(scope + "/search", json={"mode": "hybrid", "query": "customer"})
        assert hybrid.status_code == 200, hybrid.text
        hybrid_body = hybrid.json()
        assert hybrid_body["components"]["lexical"]["state"] == "completed"
        assert hybrid_body["components"]["semantic"]["state"] == "completed"
        assert hybrid_body["embedding_run"] is not None
        assert hybrid_body["hits"][0]["score_kind"] == "rrf"
        assert hybrid_body["hits"][0]["hybrid"]["lexical_contribution"] > 0
        assert hybrid_body["hits"][0]["hybrid"]["semantic_contribution"] > 0

        compatibility = client.post(
            scope + "/semantic-search", json={"query": "customer", "top_k": 1}
        )
        assert compatibility.status_code == 200, compatibility.text
        assert compatibility.json()["hits"][0]["score"] == pytest.approx(1.0)
        assert provider.query_calls == 2

        invalid = client.post(
            scope + "/search",
            json={"mode": "lexical", "query": "secret source", "unknown": True},
        )
        assert invalid.status_code == 422
        assert "secret source" not in invalid.text
