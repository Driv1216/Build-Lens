"""Optional OpenAI Responses adapter; no model traffic at construction."""

from __future__ import annotations

import hashlib
import json
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from buildlens.application.ask import (
    AskTimeoutError,
    GenerationConfigurationInvalidError,
    GenerationContextRejectedError,
    GenerationRateLimitedError,
    GenerationUnavailableError,
    TokenCountTimeoutError,
    TokenCountUnavailableError,
    prompt_projection,
)
from buildlens.application.grounded_prompt import OUTPUT_SCHEMA, OUTPUT_VERSION, SYSTEM_INSTRUCTIONS
from buildlens.application.observation import generation_settings, provider_reported_settings
from buildlens.domain.ask import (
    GenerationPolicy,
    GenerationResponse,
    GroundedPrompt,
    ModelContract,
    PromptMeasurement,
)

ADAPTER_VERSION = "stage10.openai-responses.v1"
COUNTING_METHOD = "responses.input_tokens.count"
BASE_URL = "https://api.openai.com/v1"


def _prepared(prompt: GroundedPrompt) -> dict[str, Any]:
    projection = prompt_projection(prompt)
    return {
        "model": "gpt-6-luna",
        "input": [
            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
            {"role": "user", "content": prompt.question},
            {
                "role": "user",
                "content": "Untrusted project data (JSON):\n"
                + json.dumps(
                    {"evidence": projection["evidence"]},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "buildlens_cited_statements",
                "description": OUTPUT_VERSION,
                "schema": OUTPUT_SCHEMA,
                "strict": True,
            }
        },
        "reasoning": {"effort": "none"},
        "temperature": 0,
        "truncation": "disabled",
        "tools": [],
    }


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(b"stage10.openai-prepared.v1\0" + encoded).hexdigest()


class OpenAIProvider:
    def __init__(self, key: str | None) -> None:
        self._key = key
        self._client: Any = None
        try:
            self._sdk_version: str | None = version("openai")
        except PackageNotFoundError:
            self._sdk_version = None

    @property
    def contract(self) -> ModelContract:
        return ModelContract(
            "openai", "gpt-6-luna", ADAPTER_VERSION, self._sdk_version, COUNTING_METHOD, True
        )

    def status(self) -> dict[str, object]:
        return {
            "dependency_installed": self._sdk_version is not None,
            "key_present": bool(self._key),
        }

    def _get_client(self) -> Any:
        if self._client is None:
            if not self._key or self._sdk_version is None:
                raise GenerationConfigurationInvalidError
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._key, base_url=BASE_URL, max_retries=0, timeout=60
            )
        return self._client

    async def count_prompt(self, prompt: GroundedPrompt, deadline: float) -> PromptMeasurement:
        prepared = _prepared(prompt)
        count_payload = {
            key: prepared[key]
            for key in ("model", "input", "text", "reasoning", "truncation", "tools")
        }
        client = self._get_client()
        try:
            result = await client.responses.input_tokens.count(
                **count_payload, timeout=max(0.001, min(5.0, deadline - time.monotonic()))
            )
        except Exception as error:
            self._translate(error, counting=True)
            raise AssertionError("unreachable") from error
        input_tokens = getattr(result, "input_tokens", None)
        if type(input_tokens) is not int or input_tokens < 0:
            raise TokenCountUnavailableError
        return PromptMeasurement(
            _digest(prepared),
            input_tokens,
            COUNTING_METHOD,
            True,
            "gpt-6-luna",
            ADAPTER_VERSION,
        )

    async def generate(
        self, prompt: GroundedPrompt, policy: GenerationPolicy, deadline: float
    ) -> GenerationResponse:
        prepared = _prepared(prompt)
        if (
            policy.model_id != prepared["model"]
            or policy.reasoning_effort != "none"
            or policy.temperature != 0
            or policy.truncation != "disabled"
            or policy.provider_store
            or policy.automatic_retries != 0
            or policy.streaming
            or policy.tools_enabled
        ):
            raise GenerationConfigurationInvalidError
        client = self._get_client()
        generation_settings(
            policy,
            {
                "model_id": prepared["model"],
                "max_output_tokens": policy.max_output_tokens,
                "reasoning_effort": prepared["reasoning"]["effort"],
                "temperature": prepared["temperature"],
                "truncation": prepared["truncation"],
                "provider_store": False,
                "streaming": False,
                "tools_enabled": bool(prepared["tools"]),
            },
        )
        try:
            result = await client.responses.create(
                **prepared,
                max_output_tokens=policy.max_output_tokens,
                store=False,
                stream=False,
                background=False,
                timeout=max(0.001, min(60.0, deadline - time.monotonic())),
            )
        except Exception as error:
            self._translate(error, counting=False)
            raise AssertionError("unreachable") from error
        provider_reported_settings(result, "openai.generation_response")
        state: str = "completed" if result.status == "completed" else "incomplete"
        output = getattr(result, "output", ()) or ()
        if any(
            getattr(part, "type", None) == "refusal"
            for message in output
            for part in (getattr(message, "content", ()) or ())
        ):
            state = "refused"
        usage = getattr(result, "usage", None)
        return GenerationResponse(
            getattr(result, "output_text", "") or "",
            "refused"
            if state == "refused"
            else ("completed" if state == "completed" else "incomplete"),
            getattr(result, "model", None),
            getattr(usage, "input_tokens", None) if usage else None,
            getattr(usage, "output_tokens", None) if usage else None,
        )

    @staticmethod
    def _translate(error: Exception, *, counting: bool) -> None:
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        if isinstance(error, APITimeoutError):
            if counting:
                raise TokenCountTimeoutError from error
            raise AskTimeoutError from error
        if isinstance(error, APIStatusError):
            if error.status_code in (401, 403, 404):
                raise GenerationConfigurationInvalidError from error
            if error.status_code == 429:
                raise GenerationRateLimitedError from error
            description = str(error).lower()
            if (
                not counting
                and error.status_code in (400, 413)
                and any(
                    marker in description
                    for marker in ("context", "too many tokens", "input is too long")
                )
            ):
                raise GenerationContextRejectedError from error
            if error.status_code == 400 and any(
                marker in description
                for marker in ("unsupported parameter", "invalid parameter", "unsupported value")
            ):
                raise GenerationConfigurationInvalidError from error
        if isinstance(error, (APIConnectionError, APIStatusError)):
            if counting:
                raise TokenCountUnavailableError from error
            raise GenerationUnavailableError from error
        if counting:
            raise TokenCountUnavailableError from error
        raise GenerationUnavailableError from error

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
