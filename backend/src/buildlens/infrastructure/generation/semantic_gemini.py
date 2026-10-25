"""Gemini Developer API adapter for semantic generation (separate from, and not changing, Ask).

``HttpRetryOptions(attempts=1)``, automatic function calling disabled, no tools, and no
``count_tokens``, model-listing or metadata calls. Thinking configuration is not sent (the
provider default applies), as in Ask. The client is constructed lazily.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from buildlens.application.outbound_gate import (
    CanonicalOutbound,
    GateTicket,
    ProviderCallError,
    ProviderResult,
)
from buildlens.domain.semantic_generation import ProviderConfig
from buildlens.infrastructure.generation.semantic_common import (
    UNTRUSTED_PREFIX,
    generation_policy,
    package_version,
    retry_after,
)

ADAPTER_VERSION = "semantic-gemini-generate-content.v1"
DEFAULT_MODEL = "gemini-3.8-flash"


class GeminiSemanticGenerator:
    def __init__(
        self,
        key: str | None,
        model: str,
        max_output_tokens: int,
        http_client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
    ) -> None:
        self._key, self._model, self._max_output_tokens = key, model, max_output_tokens
        self._http_client, self._base_url = http_client, base_url
        self._client: Any = None

    @property
    def config(self) -> ProviderConfig:
        return ProviderConfig(
            "gemini",
            self._model,
            generation_policy(self._max_output_tokens, thinking="provider_default"),
            ADAPTER_VERSION,
            {"google-genai": package_version("google-genai")},
            None,
        )

    def status(self) -> dict[str, object]:
        return {
            "dependency_installed": package_version("google-genai") is not None,
            "key_present": bool(self._key),
            "configured": bool(self._key) and package_version("google-genai") is not None,
        }

    def render(
        self, instructions: str, data: str, schema: dict[str, Any], max_output_tokens: int
    ) -> CanonicalOutbound:
        schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return CanonicalOutbound(
            "gemini",
            self._model,
            {
                "contents": [
                    instructions + "\nOutput JSON schema:\n" + schema_text,
                    UNTRUSTED_PREFIX + data,
                ]
            },
            {
                "response_mime_type": "application/json",
                "response_json_schema": schema,
                "max_output_tokens": max_output_tokens,
                "automatic_function_calling": "disabled",
            },
        )

    def _get_client(self, timeout_seconds: float) -> Any:
        if self._client is None:
            if not self._key or package_version("google-genai") is None:
                raise ProviderCallError("configuration_invalid")
            from google import genai
            from google.genai import types

            options: dict[str, Any] = {
                "api_version": "v1",
                "timeout": int(timeout_seconds * 1000),
                "retry_options": types.HttpRetryOptions(attempts=1),
            }
            if self._base_url:
                options["base_url"] = self._base_url
            if self._http_client is not None:
                options["httpx_async_client"] = self._http_client
            self._client = genai.Client(
                api_key=self._key, vertexai=False, http_options=types.HttpOptions(**options)
            )
        return self._client

    async def send(
        self, canonical: CanonicalOutbound, ticket: GateTicket, timeout_seconds: float
    ) -> ProviderResult:
        ticket.redeem(canonical)
        from google.genai import types

        client = self._get_client(timeout_seconds)
        settings = canonical.settings
        config = types.GenerateContentConfig(
            response_mime_type=settings["response_mime_type"],
            response_json_schema=settings["response_json_schema"],
            max_output_tokens=settings["max_output_tokens"],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            result = await client.aio.models.generate_content(
                model=canonical.model, contents=canonical.body["contents"], config=config
            )
        except Exception as error:
            raise self._translate(error) from error
        candidates = getattr(result, "candidates", None) or ()
        candidate = next(iter(candidates), None) if len(candidates) == 1 else None
        finish = getattr(getattr(candidate, "finish_reason", None), "name", None)
        parts = getattr(getattr(candidate, "content", None), "parts", None) or ()
        text = "".join(part.text for part in parts if isinstance(getattr(part, "text", None), str))
        usage = getattr(result, "usage_metadata", None)
        return ProviderResult(
            text,
            "completed"
            if finish == "STOP"
            else "refused"
            if finish in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION"}
            else "incomplete",
            getattr(result, "model_version", None),
            getattr(usage, "prompt_token_count", None) if usage else None,
            getattr(usage, "candidates_token_count", None) if usage else None,
        )

    @staticmethod
    def _translate(error: Exception) -> ProviderCallError:
        from google.genai.errors import APIError

        if isinstance(error, APIError):
            response = getattr(error, "response", None)
            headers = getattr(response, "headers", None)
            if error.code == 429:
                return ProviderCallError("rate_limited", retry_after(headers))
            if error.code == 503:
                return ProviderCallError("provider_unavailable", retry_after(headers))
            if error.code in (400, 413):
                return ProviderCallError("context_rejected")
            if error.code in (401, 403, 404):
                return ProviderCallError("configuration_invalid")
            if error.code in (408, 504):
                return ProviderCallError("timeout")
        if isinstance(error, httpx.TimeoutException):
            return ProviderCallError("timeout")
        return ProviderCallError("provider_unavailable")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aio.aclose()
            self._client = None
