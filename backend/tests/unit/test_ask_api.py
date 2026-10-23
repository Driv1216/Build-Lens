from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from buildlens.api.dependencies import get_ask_service
from buildlens.api.main import create_app
from buildlens.application.ask import AskService
from buildlens.core.config import Settings
from buildlens.domain.ask import GenerationPolicy
from tests.unit.test_ask import FakeProvider
from tests.unit.test_context import FakeReranker, chunk, service


def test_ask_api_validation_status_and_no_store() -> None:
    provider = FakeProvider()
    context = service([chunk(1, "Shown.")], FakeReranker())
    ask = AskService(context, provider, enabled=True, policy=GenerationPolicy())
    app = create_app(Settings(database_url="postgresql+psycopg://test@127.0.0.1:1/test"))
    app.dependency_overrides[get_ask_service] = lambda: ask
    scope = f"/api/v1/projects/{UUID(int=500)}/snapshots/{UUID(int=400)}"
    with TestClient(app) as client:
        status = client.get("/api/v1/generation")
        assert status.status_code == 200
        assert status.json()["configured"]
        invalid = client.post(scope + "/ask", json={"question": "x", "instructions": "override"})
        assert invalid.status_code == 422
        assert invalid.headers["cache-control"] == "no-store"
        assert invalid.json()["detail"]["code"] == "REQUEST_INVALID"
        answer = client.post(
            scope + "/ask",
            json={
                "question": "What is shown?",
                "retrieval_mode": "lexical",
                "ranking_policy": "retrieval_order",
            },
        )
        assert answer.status_code == 200, answer.text
        assert answer.headers["cache-control"] == "no-store"
        body = answer.json()
        assert body["status"] == "answered"
        assert body["answer"]["statements"][0]["evidence_ids"] == ["E1"]
        assert body["citations"][0]["item"]["content"] == "Shown."
        assert body["citations"][0]["item"]["locator"] == "src/example.py"
        assert body["generation_state"] == "completed"
        assert body["context"]["budget"]["generation_token_count"] is None
        assert len(provider.generations) == 1
