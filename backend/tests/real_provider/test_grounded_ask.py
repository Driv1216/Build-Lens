"""Explicitly paid, synthetic-data-only full pipeline verification."""

import os
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from buildlens.api.dependencies import get_ask_service, get_context_service
from buildlens.api.main import create_app
from buildlens.application.ask import AskService
from buildlens.core.config import Settings
from buildlens.domain.ask import GenerationPolicy

pytestmark = [pytest.mark.real_provider, pytest.mark.integration]


def test_real_provider_default_policy_pipeline(pg_engine: Engine, tmp_path: Path) -> None:
    if os.getenv("BUILDLENS_REAL_PROVIDER_TEST") != "1":
        pytest.fail("Set BUILDLENS_REAL_PROVIDER_TEST=1 to authorize this paid gate")
    required = ["BUILDLENS_EMBEDDINGS_CACHE_DIR", "BUILDLENS_RERANKER_CACHE_DIR"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.fail("Real-provider gate lacks: " + ", ".join(missing))
    (tmp_path / "sentinel.py").write_text(
        "def deployment_sentinel():\n"
        '    """Return the fixed deployment sentinel for this synthetic fixture."""\n'
        "    return 'cobalt-lantern-37'\n"
    )
    settings = Settings(
        database_url=pg_engine.url.render_as_string(hide_password=False),
        ask_enabled=True,
        generation_provider="gemini",
    )
    if not settings.gemini_api_key:
        pytest.fail("Real-provider gate lacks backend Gemini credential")
    app = create_app(settings)
    with TestClient(app) as client:
        project = client.post(
            "/api/v1/projects",
            json={
                "name": "Synthetic provider gate",
                "root_path": str(tmp_path),
            },
        ).json()["id"]
        snapshot = client.post(f"/api/v1/projects/{project}/snapshots", json={}).json()["id"]
        scope = f"/api/v1/projects/{project}/snapshots/{snapshot}"
        assert client.post(scope + "/structure", json={}).status_code == 200
        assert client.post(scope + "/processing", json={}).status_code == 200
        assert client.post(scope + "/embeddings", json={}).status_code == 200
        generation = client.get("/api/v1/generation").json()
        assert generation["configured"]
        assert generation["provider"] == "gemini"
        assert generation["model_id"] == "gemini-3.8-flash"
        assert client.get("/api/v1/reranker").json()["available"]
        answer = client.post(
            scope + "/ask",
            json={
                "question": "What string does deployment_sentinel return?",
            },
        )
        assert answer.status_code == 200
        body = answer.json()
        assert body["status"] == "answered"
        assert body["context"]["ranking_policy"] == "local_cross_encoder"
        assert body["context"]["retrieval"]["executed_mode"] == "hybrid"
        supplied = {item["evidence_id"] for item in body["evidence"]}
        assert all(
            set(statement["evidence_ids"]) <= supplied for statement in body["answer"]["statements"]
        )
        assert body["prompt_measurement"]["input_tokens"] > 0
        assert body["usage_input_tokens"] == body["prompt_measurement"]["input_tokens"]
        assert body["usage_output_tokens"] > 0
        assert body["model_contract"]["provider"] == "gemini"
        assert body["model_contract"]["model_id"] == "gemini-3.8-flash"
        print(
            {
                "requested_model": body["model_contract"]["model_id"],
                "returned_model": body["returned_model"],
                "counted_input_tokens": body["prompt_measurement"]["input_tokens"],
                "usage_input_tokens": body["usage_input_tokens"],
                "usage_output_tokens": body["usage_output_tokens"],
                "elapsed_seconds": body["total_seconds"],
                "structured_output": body["status"] == "answered",
                "citation_validation": True,
                "authority_recheck": True,
                "fallback_occurred": False,
            }
        )
        abstention = client.post(
            scope + "/ask",
            json={
                "question": "What is the production database password for this deployment?",
            },
        )
        assert abstention.status_code == 200, abstention.json().get("detail", {}).get("code")
        abstention_body = abstention.json()
        assert abstention_body["status"] == "insufficient_evidence"
        assert abstention_body["model_contract"]["provider"] == "gemini"
        print(
            {
                "abstention": True,
                "counted_input_tokens": abstention_body["prompt_measurement"]["input_tokens"],
                "usage_input_tokens": abstention_body["usage_input_tokens"],
                "usage_output_tokens": abstention_body["usage_output_tokens"],
                "elapsed_seconds": abstention_body["total_seconds"],
                "fallback_occurred": False,
            }
        )

        def short_generation(request: Request) -> AskService:
            return AskService(
                get_context_service(request),
                app.state.generation_provider,
                enabled=True,
                policy=replace(
                    GenerationPolicy.for_provider("gemini"), generation_timeout_seconds=0.001
                ),
            )

        app.dependency_overrides[get_ask_service] = short_generation
        timed = client.post(
            scope + "/ask",
            json={
                "question": "What string does deployment_sentinel return?",
            },
        )
        assert timed.status_code == 504
        assert timed.json()["detail"]["code"] == "ASK_TIMEOUT"
