from __future__ import annotations

import asyncio
import json
import time
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("google.genai")

from google import genai
from google.genai import types

from buildlens.api.main import create_app
from buildlens.application.ask import (
    GenerationConfigurationInvalidError,
    GenerationRateLimitedError,
)
from buildlens.application.evaluation import Collector
from buildlens.application.observation import observing
from buildlens.core.config import Settings
from buildlens.domain.ask import GenerationPolicy, GroundedPrompt
from buildlens.infrastructure.generation.gemini import GeminiProvider, _prepared


def test_count_and_generation_use_identical_complete_contents() -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 91})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": '{"status":"insufficient_evidence","statements":[]}'}
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "modelVersion": "gemini-3.8-flash",
                "usageMetadata": {
                    "promptTokenCount": 91,
                    "candidatesTokenCount": 14,
                    "thoughtsTokenCount": 3,
                },
            },
        )

    async def run() -> None:
        provider = GeminiProvider("synthetic-test-key")
        transport = httpx.MockTransport(handle)
        provider._client = genai.Client(
            api_key="synthetic-test-key",
            http_options=types.HttpOptions(
                api_version="v1",
                retry_options=types.HttpRetryOptions(attempts=1),
                httpx_async_client=httpx.AsyncClient(transport=transport),
            ),
        )
        prompt = GroundedPrompt("stage10.grounded-prompt.v1", "Where?", ())
        try:
            counted = await provider.count_prompt(prompt, time.monotonic() + 5)
            collector = Collector()
            with observing(collector):
                generated = await provider.generate(
                    prompt, GenerationPolicy.for_provider("gemini"), time.monotonic() + 5
                )
        finally:
            await provider.close()
        assert counted.input_tokens == generated.input_tokens == 91
        assert generated.output_tokens == 17
        assert generated.state == "completed"
        assert generated.returned_model == "gemini-3.8-flash"
        assert len(requests) == 2
        count_body, generate_body = requests[0][1], requests[1][1]
        assert count_body["contents"] == generate_body["contents"]
        serialized = json.dumps(count_body["contents"])
        assert "Where?" in serialized
        assert "BuildLens answer rules" in serialized
        assert "additionalProperties" in serialized
        assert generate_body["generationConfig"]["responseMimeType"] == "application/json"
        assert (
            generate_body["generationConfig"]["responseJsonSchema"]
            == _prepared(prompt)["response_json_schema"]
        )
        assert cast(dict[str, Any], collector.values["generation_settings"])["thinking_level"] == {
            "requested": "medium",
            "sent": False,
            "transmitted_value": None,
            "effective": None,
            "effective_provenance": "provider-default-unspecified",
        }
        assert (
            cast(dict[str, Any], collector.values["generation_settings"])["max_output_tokens"][
                "transmitted_value"
            ]
            == generate_body["generationConfig"]["maxOutputTokens"]
        )
        assert "thinkingConfig" not in generate_body["generationConfig"]
        assert "tools" not in generate_body

    asyncio.run(run())


def test_selected_provider_status_never_falls_back() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://test@127.0.0.1:1/test",
        ask_enabled=True,
        generation_provider="gemini",
        gemini_api_key=None,
        openai_api_key="synthetic-openai-key",
    )
    with TestClient(create_app(settings)) as client:
        status = client.get("/api/v1/generation")
        assert status.status_code == 200
        body = status.json()
        assert body["provider"] == "gemini"
        assert body["model_id"] == "gemini-3.8-flash"
        assert body["configured"] is False
        assert body["key_present"] is False
        assert "synthetic-openai-key" not in status.text

    openai_settings = Settings(
        database_url="postgresql+psycopg://test@127.0.0.1:1/test",
        ask_enabled=True,
        generation_provider="openai",
        gemini_api_key="synthetic-gemini-key",
        openai_api_key=None,
    )
    with TestClient(create_app(openai_settings)) as client:
        status = client.get("/api/v1/generation")
        assert status.status_code == 200
        body = status.json()
        assert body["provider"] == "openai"
        assert body["model_id"] == "gpt-6-luna"
        assert body["configured"] is False
        assert body["key_present"] is False
        assert "synthetic-gemini-key" not in status.text


@pytest.mark.parametrize(
    ("code", "expected"),
    [(401, GenerationConfigurationInvalidError), (429, GenerationRateLimitedError)],
)
def test_gemini_errors_are_sanitized(code: int, expected: type[Exception]) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(code, json={"error": {"code": code, "message": "private body"}})

    async def run() -> None:
        provider = GeminiProvider("synthetic-test-key")
        provider._client = genai.Client(
            api_key="synthetic-test-key",
            http_options=types.HttpOptions(
                api_version="v1",
                retry_options=types.HttpRetryOptions(attempts=1),
                httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            ),
        )
        try:
            with pytest.raises(expected) as caught:
                await provider.generate(
                    GroundedPrompt("stage10.grounded-prompt.v1", "Question", ()),
                    GenerationPolicy.for_provider("gemini"),
                    time.monotonic() + 5,
                )
            assert "private body" not in str(caught.value)
        finally:
            await provider.close()

    asyncio.run(run())
    assert calls == 1
