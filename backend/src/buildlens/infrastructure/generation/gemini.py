"""Gemini Developer API adapter for the provider-neutral grounded Ask service."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

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

MODEL_ID = "gemini-3.8-flash"
ADAPTER_VERSION = "stage10.gemini-generate-content.v1"
COUNTING_METHOD = "models.count_tokens"


def _prepared(prompt: GroundedPrompt) -> dict[str, Any]:
    # The Developer API count_tokens endpoint accepts contents, not generation
    # config. Include every prompt instruction and the output schema in those
    # same contents for both counting and generation.
    instructions = (
        SYSTEM_INSTRUCTIONS
        + "\nStructured response contract (JSON Schema):\n"
        + json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\nContract version: "
        + OUTPUT_VERSION
    )
    projection = prompt_projection(prompt)
    evidence = json.dumps(
        {"evidence": projection["evidence"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "model": MODEL_ID,
        "contents": [
            "BuildLens answer rules:\n" + instructions,
            "Question:\n" + prompt.question,
            "Untrusted project data (JSON):\n" + evidence,
        ],
        "response_json_schema": OUTPUT_SCHEMA,
        "response_mime_type": "application/json",
    }


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(b"stage10.gemini-prepared.v1\0" + encoded).hexdigest()


class GeminiProvider:
    def __init__(self, key: str | None) -> None:
        self._key = key
        self._client: Any = None
        try:
            self._sdk_version: str | None = version("google-genai")
        except PackageNotFoundError:
            self._sdk_version = None

    @property
    def contract(self) -> ModelContract:
        return ModelContract(
            "gemini",
            MODEL_ID,
            ADAPTER_VERSION,
            self._sdk_version,
            COUNTING_METHOD,
            True,
            context_limit=1_048_576,
            maximum_model_output_tokens=65_536,
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
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self._key,
                vertexai=False,
                enterprise=False,
                http_options=types.HttpOptions(
                    api_version="v1",
                    timeout=60_000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        return self._client

    async def count_prompt(self, prompt: GroundedPrompt, deadline: float) -> PromptMeasurement:
        prepared = _prepared(prompt)
        client = self._get_client()
        try:
            result = await client.aio.models.count_tokens(
                model=prepared["model"], contents=prepared["contents"]
            )
        except Exception as error:
            self._translate(error, counting=True)
            raise AssertionError("unreachable") from error
        input_tokens = getattr(result, "total_tokens", None)
        if type(input_tokens) is not int or input_tokens < 0:
            raise TokenCountUnavailableError
        return PromptMeasurement(
            _digest(prepared), input_tokens, COUNTING_METHOD, True, MODEL_ID, ADAPTER_VERSION
        )

    async def generate(
        self, prompt: GroundedPrompt, policy: GenerationPolicy, deadline: float
    ) -> GenerationResponse:
        prepared = _prepared(prompt)
        if (
            policy.model_id != MODEL_ID
            or policy.context_limit != 1_048_576
            or policy.temperature is not None
            or policy.reasoning_effort is not None
            or policy.thinking_level != "medium"
            or policy.provider_store
            or policy.automatic_retries
            or policy.streaming
            or policy.tools_enabled
        ):
            raise GenerationConfigurationInvalidError
        from google.genai import types

        client = self._get_client()
        config_arguments = {
            "response_mime_type": prepared["response_mime_type"],
            "response_json_schema": prepared["response_json_schema"],
            "max_output_tokens": policy.max_output_tokens,
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        config = types.GenerateContentConfig(**config_arguments)
        generation_settings(
            policy,
            {
                "model_id": prepared["model"],
                "max_output_tokens": config_arguments["max_output_tokens"],
            },
        )
        try:
            result = await client.aio.models.generate_content(
                model=prepared["model"],
                contents=prepared["contents"],
                config=config,
            )
        except Exception as error:
            self._translate(error, counting=False)
            raise AssertionError("unreachable") from error
        provider_reported_settings(result, "gemini.generation_response")
        candidates = getattr(result, "candidates", None) or ()
        candidate = next(iter(candidates), None) if len(candidates) == 1 else None
        finish = getattr(candidate, "finish_reason", None)
        finish_name = getattr(finish, "name", str(finish))
        if finish_name == "STOP":
            state: Literal["completed", "incomplete", "refused"] = "completed"
        elif finish_name in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION"}:
            state = "refused"
        else:
            state = "incomplete"
        parts = getattr(getattr(candidate, "content", None), "parts", None) or ()
        raw_text = "".join(
            part.text for part in parts if isinstance(getattr(part, "text", None), str)
        )
        usage = getattr(result, "usage_metadata", None)
        candidate_tokens = getattr(usage, "candidates_token_count", None) if usage else None
        thought_tokens = getattr(usage, "thoughts_token_count", None) if usage else None
        output_tokens = (
            candidate_tokens + (thought_tokens or 0)
            if type(candidate_tokens) is int and type(thought_tokens) in (int, type(None))
            else None
        )
        return GenerationResponse(
            raw_text,
            state,
            getattr(result, "model_version", None),
            getattr(usage, "prompt_token_count", None) if usage else None,
            output_tokens,
        )

    @staticmethod
    def _translate(error: Exception, *, counting: bool) -> None:
        from google.genai.errors import APIError

        if isinstance(error, APIError):
            code = error.code
            if code in (401, 403, 404):
                raise GenerationConfigurationInvalidError from error
            if code == 429:
                raise GenerationRateLimitedError from error
            if code in (408, 504):
                if counting:
                    raise TokenCountTimeoutError from error
                raise AskTimeoutError from error
            if code in (400, 413):
                # A rejected full prompt may indicate a provider-side context limit.
                if not counting:
                    raise GenerationContextRejectedError from error
                raise TokenCountUnavailableError from error
        if counting:
            raise TokenCountUnavailableError from error
        raise GenerationUnavailableError from error

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aio.aclose()
            self._client = None
