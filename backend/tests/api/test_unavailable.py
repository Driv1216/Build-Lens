from fastapi.testclient import TestClient

from buildlens.api.main import create_app
from buildlens.core.config import Settings


def test_database_outage_is_sanitized_and_does_not_claim_persistence() -> None:
    app = create_app(
        Settings(
            database_url="postgresql+psycopg://test:private-test-value@127.0.0.1:1/unavailable"
        )
    )
    with TestClient(app) as client:
        for response in (
            client.get("/api/v1/projects"),
            client.post(
                "/api/v1/projects", json={"name": "Unavailable", "root_path": "/tmp/unavailable"}
            ),
        ):
            assert response.status_code == 503
            assert response.json() == {
                "detail": {
                    "code": "PERSISTENCE_UNAVAILABLE",
                    "message": "Database operation could not be completed.",
                }
            }
            assert "private-test-value" not in response.text
            assert "postgresql" not in response.text
