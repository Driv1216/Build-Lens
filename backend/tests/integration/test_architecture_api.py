"""Stage 14 Milestone A: architecture HTTP contracts through the real application."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from buildlens.api.main import create_app
from buildlens.core.config import Settings
from tests.semantic_support import REPOSITORY, write

pytestmark = pytest.mark.integration


def test_architecture_http_contracts(pg_engine: Engine, tmp_path: Path) -> None:
    write(tmp_path, REPOSITORY)
    app = create_app(Settings(database_url=pg_engine.url.render_as_string(hide_password=False)))
    with TestClient(app) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "Architecture", "root_path": str(tmp_path)}
        ).json()["id"]
        base = f"/api/v1/projects/{project}/snapshots"
        scope = f"{base}/{client.post(base, json={}).json()['id']}"
        arch = scope + "/architecture"
        status = client.get(arch).json()
        assert status["freshness"] == "none" and status["catalog_ready"] is False
        assert client.get(arch + "/overview").json()["detail"]["code"] == "ARCHITECTURE_REQUIRED"
        assert client.post(arch, json={}).json()["detail"]["code"] == "SEMANTIC_CATALOG_REQUIRED"
        for step in ("/structure", "/processing", "/semantic/catalog"):
            assert client.post(scope + step, json={}).status_code == 200
        rejected = client.post(arch, content="{}", headers={"Content-Type": "text/plain"})
        assert rejected.status_code == 415
        built = client.post(arch, json={})
        assert built.status_code == 200, built.text
        assert built.json()["freshness"] == "fresh"
        run_id = built.json()["active_run"]["run_id"]

        overview = client.get(arch + "/overview").json()
        assert overview["verification"] == "verified" and overview["node_counts"]["module"] >= 8
        tree = client.get(arch + "/tree").json()
        assert [child["key"] for child in tree["children"]][:1]
        api = quote("module:app/api.py", safe="")
        db = quote("module:app/db.py", safe="")
        node = client.get(f"{arch}/node?key={api}").json()
        assert node["node"]["coverage_state"] == "analysed"
        assert any(edge["target_key"] == "module:app/db.py" for edge in node["outgoing"])
        edges = client.get(arch + "/edges?kind=imports&resolution=not_attempted").json()
        assert [edge["target_text"] for edge in edges["items"]] == ["@/lib/x"]
        found = client.get(f"{arch}/path?source={api}&target={db}&class_set=S4").json()
        assert found["status"] == "found" and len(found["path"]) == 1
        reverse = client.get(f"{arch}/path?source={db}&target={api}").json()
        assert reverse["status"] == "not_found"  # complete search, no gaps (literal pass)
        deps = client.get(f"{arch}/dependencies?node={api}&class_set=S4").json()
        assert "module:app/db.py" in deps["nodes"] and deps["search"] == "complete"
        matrix = client.get(arch + "/matrix?parent=" + quote("workspace:.", safe="")).json()
        assert {child["key"] for child in matrix["children"]} >= {"container:app", "container:web"}
        hood = client.get(f"{arch}/neighbourhood?node={api}").json()
        assert hood["origin"] == "module:app/api.py" and not hood["truncated"]
        assert client.get(arch + "/cycles").json()["total"] >= 0
        diagnostics = client.get(arch + "/diagnostics?code=ALIAS_NOT_RESOLVED").json()
        assert diagnostics["total"] == 1
        assert client.get(arch + "/coverage").json()["coverage"]["alias"]["statements"] == 1
        link = node["node"]["evidence"][0]["digest"]
        evidence = client.get(f"{arch}/evidence/{link}").json()
        assert evidence["verification"] == "verified" and evidence["rendered_text"]
        assert client.get(arch + "/integrity").json()["structure"] == "verified"
        # Milestone B API view: the fixture router is never mounted, so its endpoint has no
        # full path and nothing is connected.
        view = client.get(arch + "/api").json()
        assert view["verification"] == "verified" and view["endpoints_total"] == 1
        assert view["endpoints"][0]["attributes"]["paths"][0]["reasons"] == ["ROUTER_NOT_MOUNTED"]
        assert view["connections"] == [] and view["coverage"]["endpoints"]["total"] == 1
        assert client.get(arch + "/api?limit=0").status_code == 422
        database = client.get(arch + "/views/database").json()
        assert database["verification"] == "verified"
        assert {n["name"] for n in database["nodes"]} == {"users"}
        assert client.get(arch + "/views/subsystems").status_code == 200
        assert client.get(arch + "/views/frontend").json()["coverage"]["frontend_routes"]
        assert client.get(arch + "/views/unknown").status_code == 422
        assert client.get(arch + "/runs").json()["total"] == 1
        assert client.get(f"{arch}/runs/{run_id}").status_code == 200
        assert client.get(f"{arch}/runs/{uuid4()}").status_code == 404

        invalid = client.get(f"{arch}/path?source={api}&target={quote('workspace:.')}")
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "ARCHITECTURE_QUERY_INVALID"
        assert client.get(f"{arch}/node?key=module:missing").status_code == 422

        # Rebuilding the catalog after reprocessing makes the current architecture stale.
        assert client.post(scope + "/processing", json={}).status_code == 200
        assert client.post(scope + "/semantic/catalog", json={}).status_code == 200
        stale = client.get(arch + "/overview")
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "ARCHITECTURE_STALE"
        history = client.get(f"{arch}/overview?run_id={run_id}").json()
        assert "CATALOG_AUTHORITY_CHANGED" in history["freshness_reasons"]
