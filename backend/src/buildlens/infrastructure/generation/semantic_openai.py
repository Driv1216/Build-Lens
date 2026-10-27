"""OpenAI Responses adapter for semantic generation (separate from, and not changing, Ask).

``max_retries=0``, ``store=False``, ``stream=False``, ``background=False`` and no tools. The
client is constructed lazily; construction, ``config`` and ``render`` make no request.
"""

from __future__ import annotations

from typing import Any

import httpx

from buildlens.application.outbound_gate import (
    CanonicalOutbound,
    GateTicket,
    ProviderCallError,
    ProviderResult,
)
from buildlens.domain.semantic_generation import SCHEMA_VERSION, ProviderConfig
from buildlens.infrastructure.generation.semantic_common import (
    SCHEMA_NAME,
    UNTRUSTED_PREFIX,
    generation_policy,
    package_version,
    retry_after,
)

ADAPTER_VERSION = "semantic-openai-responses.v1"
BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-6-luna"


class OpenAISemanticGenerator:
    def __init__(
        self,
        key: str | None,
        model: str,
        max_output_tokens: int,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._key, self._model, self._max_output_tokens = key, model, max_output_tokens
        self._http_client, self._base_url = http_client, base_url
        self._client: Any = None

    @property
    def config(self) -> ProviderConfig:
        return ProviderConfig(
            "openai",
            self._model,
            generation_policy(self._max_output_tokens, reasoning_effort="none", store=False),
            ADAPTER_VERSION,
            {"openai": package_version("openai")},
            None,
        )

    def status(self) -> dict[str, object]:
        return {
            "dependency_installed": package_version("openai") is not None,
            "key_present": bool(self._key),
            "configured": bool(self._key) and package_version("openai") is not None,
        }

    def render(
        self, instructions: str, data: str, schema: dict[str, Any], max_output_tokens: int
    ) -> CanonicalOutbound:
        return CanonicalOutbound(
            "openai",
            self._model,
            {
                "input": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": UNTRUSTED_PREFIX + data},
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": SCHEMA_NAME,
                        "description": SCHEMA_VERSION,
                        "schema": schema,
                        "strict": True,
                    }
                },
                "reasoning": {"effort": "none"},
                "temperature": 0,
                "truncation": "disabled",
                "tools": [],
            },
            {
                "max_output_tokens": max_output_tokens,
                "store": False,
                "stream": False,
                "background": False,
            },
        )

    def _get_client(self) -> Any:
        if self._client is None:
            if not self._key or package_version("openai") is None:
                raise ProviderCallError("configuration_invalid")
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._key,
                base_url=self._base_url,
                max_retries=0,
                timeout=60,
                http_client=self._http_client,
            )
        return self._client

    async def send(
        self, canonical: CanonicalOutbound, ticket: GateTicket, timeout_seconds: float
    ) -> ProviderResult:
        ticket.redeem(canonical)
        client = self._get_client()
        try:
            result = await client.responses.create(
                model=canonical.model,
                **canonical.body,
                **canonical.settings,
                timeout=timeout_seconds,
            )
        except Exception as error:
            raise self._translate(error) from error
        output = getattr(result, "output", ()) or ()
        refused = any(
            getattr(part, "type", None) == "refusal"
            for message in output
            for part in (getattr(message, "content", ()) or ())
        )
        usage = getattr(result, "usage", None)
        return ProviderResult(
            getattr(result, "output_text", "") or "",
            "refused"
            if refused
            else "completed"
            if getattr(result, "status", None) == "completed"
            else "incomplete",
            getattr(result, "model", None),
            getattr(usage, "input_tokens", None) if usage else None,
            getattr(usage, "output_tokens", None) if usage else None,
        )

    @staticmethod
    def _translate(error: Exception) -> ProviderCallError:
        from openai import APIConnectionError, APIStatusError, APITimeoutError

        if isinstance(error, APITimeoutError):
            return ProviderCallError("timeout", error=error)
        status = None
        if isinstance(error, APIStatusError):
            headers = dict(error.response.headers) if error.response is not None else None
            status = error.status_code
            seen = {"status": status, "error": error}
            if status == 429:
                return ProviderCallError("rate_limited", retry_after(headers), **seen)
            if status == 503:
                return ProviderCallError("provider_unavailable", retry_after(headers), **seen)
            if status in (400, 413):
                return ProviderCallError("context_rejected", **seen)
            if status in (401, 403, 404):
                return ProviderCallError("configuration_invalid", **seen)
        if isinstance(error, APIConnectionError):
            return ProviderCallError("provider_unavailable", error=error)
        return ProviderCallError("provider_unavailable", status=status, error=error)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
