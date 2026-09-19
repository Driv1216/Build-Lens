from fastapi.testclient import TestClient


def test_health_reports_api_liveness(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["environment"] == "development"
