from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text

from buildlens.api.main import create_app
from buildlens.core.config import Settings

pytestmark = pytest.mark.integration


def test_api_evidence_lifecycle_and_quarantine(pg_engine: Engine, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import os\n\ndef f():\n    return 1\n")
    app = create_app(Settings(database_url=pg_engine.url.render_as_string(hide_password=False)))
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/projects", json={"name": "Lifecycle", "root_path": str(tmp_path)}
        )
        assert response.status_code == 201, response.text
        project = response.json()["id"]
        base = f"/api/v1/projects/{project}/snapshots"
        snap = client.post(base, json={}).json()["id"]
        scope = f"{base}/{snap}"
        assert client.get(scope + "/structure/status").json()["active_run"] is None
        assert client.get(scope + "/structure").status_code == 409
        assert client.get(scope + "/processing/chunks").status_code == 409
        structure = client.post(scope + "/structure", json={})
        assert structure.status_code == 200, structure.text
        assert structure.json()["revision"] == 1
        symbols = client.get(scope + "/structure/symbols?limit=1").json()
        assert symbols["total"] == 1 and symbols["items"][0]["locator"] == "a.py"
        symbol_id = symbols["items"][0]["id"]
        assert client.get(scope + "/structure/symbols/" + symbol_id).status_code == 200
        assert client.get(scope + "/structure/artifacts").json()["items"][0]["symbol_count"] == 1
        assert client.get(scope + "/structure/imports").json()["total"] == 1
        processed = client.post(scope + "/processing", json={})
        assert processed.status_code == 200, processed.text
        run_id = processed.json()["active_run"]["run_id"]
        rows = client.get(scope + "/processing/chunks?limit=1").json()
        assert rows["total"] >= 1 and rows["items"][0]["content"] is None
        chunk_id = rows["items"][0]["id"]
        content = client.get(scope + "/processing/chunks/" + chunk_id)
        assert content.status_code == 200 and content.json()["content"]
        assert client.get(scope + "/processing/runs/" + str(uuid4())).status_code == 404
        assert client.get(scope + "/processing/runs/" + run_id).status_code == 200
        assert client.get(scope + "/processing/runs?limit=1").json()["total"] == 1
        assert (
            client.post(
                scope + "/processing", content="{}", headers={"Content-Type": "text/plain"}
            ).status_code
            == 415
        )
        missing = f"{base}/{uuid4()}"
        assert client.get(missing + "/processing").status_code == 404
        assert client.get(missing + "/structure/status").status_code == 404
        # Legacy content stays retained in PostgreSQL, inaccessible through every content path.
        with pg_engine.begin() as connection:
            connection.execute(text("UPDATE processing_runs SET quarantined=true"))
            connection.execute(text("UPDATE structural_analyses SET quarantined=true"))
        assert client.get(scope + "/processing/chunks/" + chunk_id).status_code == 409
        assert (
            client.get(scope + "/processing/chunks/" + chunk_id + "?run_id=" + run_id).status_code
            == 409
        )
        assert client.get(scope + "/structure/symbols/" + symbol_id).status_code == 409
        assert (
            client.get(
                scope
                + "/structure/symbols/"
                + symbol_id
                + "?run_id="
                + structure.json()["analysis_id"]
            ).status_code
            == 409
        )
        with pg_engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM chunks WHERE content <> ''")) > 0


def test_metadata_queries_never_select_chunk_content(pg_engine: Engine, tmp_path: Path) -> None:
    from buildlens.infrastructure.persistence.database import create_session_factory_from_engine
    from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore
    from buildlens.infrastructure.persistence.structural import SqlAlchemyStructuralAnalysisStore

    captured: list[str] = []

    def capture(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        captured.append(statement)

    event.listen(pg_engine, "before_cursor_execute", capture)
    try:
        store = SqlAlchemyProcessingStore(create_session_factory_from_engine(pg_engine))
        store.page_chunks(uuid4(), uuid4(), uuid4(), 50, 0)
        store.page_artifacts(uuid4(), uuid4(), uuid4(), 50, 0)
    finally:
        event.remove(pg_engine, "before_cursor_execute", capture)
    assert len(captured) == 5
    assert all(
        "chunks.content," not in statement and "chunks.content " not in statement
        for statement in captured
    )
    # Force a scoped selection to exercise summary SQL even in an empty database.
    from unittest.mock import patch

    captured.clear()
    event.listen(pg_engine, "before_cursor_execute", capture)
    try:
        structure = SqlAlchemyStructuralAnalysisStore(create_session_factory_from_engine(pg_engine))
        with patch.object(structure, "_analysis_id", return_value=uuid4()):
            structure.symbol_page(uuid4(), uuid4(), 50, 0, None, None, None)
    finally:
        event.remove(pg_engine, "before_cursor_execute", capture)
    assert len(captured) == 2
    assert all(
        all(
            column not in statement
            for column in ("symbols.signature", "symbols.decorators", "symbols.type_parameters")
        )
        for statement in captured
    )
