from dataclasses import replace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from buildlens.api.dependencies import get_embedding_service
from buildlens.api.main import create_app
from buildlens.application.embeddings import EmbeddingStatus, ModelCacheStatus
from buildlens.domain.embeddings import EmbeddingContract


def contract() -> EmbeddingContract:
    return EmbeddingContract(
        "fake",
        "1",
        "sentence-transformers/all-MiniLM-L6-v2",
        "a" * 40,
        384,
        "pytorch",
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


class StatusService:
    def __init__(self) -> None:
        self.value = EmbeddingStatus(
            model=ModelCacheStatus(False, "/cache", "MODEL_MANIFEST_MISSING"),
            contract=contract(),
            prepared=False,
            search_available=False,
            freshness_reasons=(),
            latest_attempt=None,
            active_run=None,
            active_processing_run_id=None,
        )

    def status(self, project_id: UUID, snapshot_id: UUID) -> EmbeddingStatus:
        return self.value

    def prepare(self, project_id: UUID, snapshot_id: UUID) -> None:
        self.value = replace(self.value, freshness_reasons=("NOT_PREPARED_IN_TEST",))


def test_embedding_status_does_not_load_or_download_model() -> None:
    app = create_app()
    service = StatusService()
    app.dependency_overrides[get_embedding_service] = lambda: service
    with TestClient(app) as client:
        response = client.get(f"/api/v1/projects/{uuid4()}/snapshots/{uuid4()}/embeddings")
    assert response.status_code == 200
    assert response.json()["model"] == {
        "available": False,
        "path": "/cache",
        "reason": "MODEL_MANIFEST_MISSING",
    }
    assert response.json()["contract"]["dimensions"] == 384


def test_embedding_posts_require_json_and_validation_never_echoes_query() -> None:
    app = create_app()
    service = StatusService()
    app.dependency_overrides[get_embedding_service] = lambda: service
    project_id, snapshot_id = uuid4(), uuid4()
    base = f"/api/v1/projects/{project_id}/snapshots/{snapshot_id}"
    with TestClient(app) as client:
        media = client.post(
            f"{base}/embeddings", content="", headers={"content-type": "text/plain"}
        )
        invalid = client.post(
            f"{base}/semantic-search",
            json={"query": "private query text", "top_k": 0},
        )
    assert media.status_code == 415
    assert invalid.status_code == 422
    assert invalid.json() == {
        "detail": {"code": "REQUEST_INVALID", "message": "Request validation failed."}
    }
    assert "private query text" not in invalid.text
