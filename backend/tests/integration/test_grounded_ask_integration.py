"""Migrated PostgreSQL -> retrieval -> context -> fake generation."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

from buildlens.api.main import create_app
from buildlens.core.config import Settings
from buildlens.domain.ask import (
    GenerationResponse,
    GroundedPrompt,
    ModelContract,
    PromptMeasurement,
)

pytestmark = pytest.mark.integration


class FakeGeneration:
    contract = ModelContract("fake", "gpt-6-luna", "fake.v1", "0", "fake.full-request", True)

    def __init__(self) -> None:
        self.counts = 0
        self.generations = 0

    def status(self) -> dict[str, object]:
        return {"dependency_installed": True, "key_present": True}

    async def count_prompt(self, prompt: GroundedPrompt, deadline: float) -> PromptMeasurement:
        self.counts += 1
        return PromptMeasurement(
            str(self.counts),
            100 + len(prompt.evidence) * 100,
            self.contract.counting_method,
            True,
            self.contract.model_id,
            self.contract.adapter_version,
        )

    async def generate(
        self, prompt: GroundedPrompt, policy: object, deadline: float
    ) -> GenerationResponse:
        self.generations += 1
        return GenerationResponse(
            '{"status":"answered","statements":[{"text":"The declaration exists.",'
            '"support":"direct","evidence_ids":["E1"]}]}',
            "completed",
            "gpt-6-luna",
            200,
            20,
        )

    async def close(self) -> None:
        pass


def test_grounded_ask_uses_current_postgres_evidence(pg_engine: Engine, tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("def named_sentinel():\n    return 37\n")
    app = create_app(
        Settings(
            database_url=pg_engine.url.render_as_string(hide_password=False),
            ask_enabled=True,
            generation_provider="openai",
            openai_api_key=SecretStr("synthetic"),
        )
    )
    with TestClient(app) as client:
        provider = FakeGeneration()
        app.state.generation_provider = provider
        project = client.post(
            "/api/v1/projects",
            json={
                "name": "Ask integration",
                "root_path": str(tmp_path),
            },
        ).json()["id"]
        snapshot = client.post(f"/api/v1/projects/{project}/snapshots", json={}).json()["id"]
        scope = f"/api/v1/projects/{project}/snapshots/{snapshot}"
        assert client.post(scope + "/structure", json={}).status_code == 200
        assert client.post(scope + "/processing", json={}).status_code == 200
        for mode, question in (("exact", "named_sentinel"), ("lexical", "named sentinel")):
            result = client.post(
                scope + "/ask",
                json={
                    "question": question,
                    "retrieval_mode": mode,
                    "ranking_policy": "retrieval_order",
                },
            )
            assert result.status_code == 200, result.text
            body = result.json()
            assert body["status"] == "answered"
            assert body["context"]["retrieval"]["executed_mode"] == mode
            assert body["context"]["reranker_state"] == "not_requested"
            assert body["citations"][0]["item"]["locator"] == "sample.py"
            assert body["citations"][0]["item"]["content_hash"]
        empty = client.post(
            scope + "/ask",
            json={
                "question": "no-such-literal",
                "retrieval_mode": "exact",
                "ranking_policy": "retrieval_order",
            },
        )
        assert empty.status_code == 200, empty.text
        assert empty.json()["reason"] == "no_usable_evidence"
        assert provider.counts == 6 and provider.generations == 2
