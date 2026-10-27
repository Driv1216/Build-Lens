"""Stage 12 LS-1/LS-2/LS-3: three pgvector-backed framework stores on a SEPARATE lab database.

Requires BUILDLENS_TEST_LAB_DATABASE_URL (a provisioned least-privilege lab role on a server
other than the production/test server). Skipped otherwise; never uses production.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from buildlens.api.main import create_app
from buildlens.core.config import Settings
from buildlens.domain.lab import parse_config
from buildlens.infrastructure.lab.database import role_privileges
from buildlens.infrastructure.lab.service import (
    LabWorkerPool,
    build_lab_service,
    isolated_python,
    lab_db,
)
from buildlens.infrastructure.lab.store import LabStore
from tests.lab_support import install_fakes, onboard, write_repository

pytestmark = [pytest.mark.integration, pytest.mark.lab]
LAB_URL = os.environ.get("BUILDLENS_TEST_LAB_DATABASE_URL")
QUESTION = "How is a customer user record loaded?"


@pytest.mark.skipif(
    not LAB_URL, reason="Set BUILDLENS_TEST_LAB_DATABASE_URL for lab-database tests"
)
def test_three_pgvector_stores_match_native_exact_search(pg_engine: Engine, tmp_path: Path) -> None:
    assert LAB_URL
    privileges = role_privileges(LAB_URL)
    assert (
        not privileges["superuser"] and not privileges["createdb"] and not privileges["createrole"]
    )
    assert privileges["connectable_databases"] == ["buildlens_lab"]
    assert privileges["public_schema_create"] is False
    with create_engine(LAB_URL, isolation_level="AUTOCOMMIT").connect() as connection:
        with pytest.raises(Exception) as denied:
            connection.execute(text("CREATE DATABASE lab_escape"))
        assert "permission denied" in str(denied.value).lower()
    repository = tmp_path / "repo"
    repository.mkdir()
    write_repository(repository)
    lab_root = tmp_path / "state" / ".buildlens-eval" / "lab"
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False),
        lab_root=str(lab_root),
        lab_database_url=LAB_URL,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        install_fakes(app.state)
        project, snapshot = onboard(client, repository)
        app.state.lab_workers = LabWorkerPool(
            LabStore(lab_root), max_rss_bytes=3 * 1024**3, lab_db=lab_db(settings)
        )
        app.state.lab_service_instance = None
        service = build_lab_service(app.state)
        try:
            native = service.query(
                parse_config(
                    {"name": "n", "pipeline": "native.context", "params": {"mode": "semantic"}}
                ),
                project,
                snapshot,
                QUESTION,
            )
            native_scores = {i["chunk_id"]: i["score"] for i in native["retrieved"]}
            cutoff = native["retrieved"][7]["score"]
            must_include = {c for c, s in native_scores.items() if s > cutoff + 1e-6}
            results: dict[str, Any] = {}
            pipelines = ["langchain.vector", "langchain.pg_custom", "llamaindex.pgvector"]
            if isolated_python("langchain_postgres").is_file():
                pipelines.append("langchain_postgres.pgvector")
            for pipeline in pipelines:
                config = parse_config(
                    {"name": pipeline.replace(".", "-"), "pipeline": pipeline, "params": {"k": 8}}
                )
                build = service.build(config, project, snapshot)
                assert build["reuse_verification"]["verified_genuine_reuse"] is True, pipeline
                result = service.query(config, project, snapshot, QUESTION)
                results[pipeline] = [(i["chunk_id"], i["score"]) for i in result["ranked"]]
                assert result["verification"]["rejected_anchors"] == 0
            for pipeline, ranked in results.items():
                ids = {c for c, _ in ranked}
                # Exact cosine search: every strictly-better native hit is present, and each
                # store's similarity equals native's (ties at the cutoff may break differently).
                assert must_include <= ids, (pipeline, must_include - ids)
                for chunk_id, score in ranked:
                    assert abs(score - native_scores[chunk_id]) < 1e-5, (pipeline, chunk_id, score)
            # S12-PRIV-04: the in-memory pipeline ran in an uncredentialed process; only the
            # database-scoped processes received the lab URL and the lab-server allowance.
            pool = app.state.lab_workers
            lab_server = f"{pool.lab_db[0]}:{pool.lab_db[1]}"
            allowances = {key: w.spec.allow_net for key, w in pool._workers.items()}
            credentials = {
                key: "BUILDLENS_LAB_PG_URL" in w.spec.secret_env for key, w in pool._workers.items()
            }
            assert credentials["langchain"] is False and allowances["langchain"] == []
            for key in ("langchain+lab-db", "llamaindex+lab-db"):
                assert credentials[key] is True
                assert [f"{h}:{p}" for h, p in allowances[key]] == [lab_server]
        finally:
            app.state.lab_workers.close()
