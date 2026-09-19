from fastapi.testclient import TestClient


def test_create_list_and_get_project(client: TestClient) -> None:
    created = client.post(
        "/api/v1/projects", json={"name": "BuildLens", "root_path": "/work/buildlens"}
    )

    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "BuildLens"
    assert body["root_path"] == "/work/buildlens"
    assert "id" in body

    listed = client.get("/api/v1/projects")
    assert listed.status_code == 200
    assert [project["id"] for project in listed.json()] == [body["id"]]

    fetched = client.get(f"/api/v1/projects/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == body


def test_project_api_returns_typed_errors(client: TestClient) -> None:
    invalid = client.post("/api/v1/projects", json={"name": " ", "root_path": "/work/buildlens"})
    assert invalid.status_code == 422

    relative = client.post(
        "/api/v1/projects", json={"name": "Relative", "root_path": "./not-allowed"}
    )
    assert relative.status_code == 422

    first = client.post(
        "/api/v1/projects", json={"name": "BuildLens", "root_path": "/work/buildlens"}
    )
    assert first.status_code == 201
    duplicate = client.post(
        "/api/v1/projects", json={"name": "BuildLens again", "root_path": "/work/buildlens"}
    )
    assert duplicate.status_code == 409

    missing = client.get("/api/v1/projects/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
