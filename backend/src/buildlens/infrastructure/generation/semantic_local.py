"""Loopback endpoint adapter (OpenAI-compatible chat completions on 127.0.0.1 or ::1 only).

It is labelled "loopback endpoint": BuildLens cannot prove that a model behind it is local.
No redirects, no proxy environment, no retries, no health or ``/models`` probe, and a capped
streamed response. Construction and rendering make no request.
"""

from __future__ import annotations

import json
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from buildlens.application.outbound_gate import (
    CanonicalOutbound,
    GateTicket,
    ProviderCallError,
    ProviderResult,
    canonical_json,
)
from buildlens.domain.semantic_generation import ProviderConfig
from buildlens.infrastructure.generation.semantic_common import (
    SCHEMA_NAME,
    UNTRUSTED_PREFIX,
    generation_policy,
    package_version,
    retry_after,
)

ADAPTER_VERSION = "semantic-loopback-chat-completions.v1"
RESPONSE_CAP_BYTES = 1_048_576
_PATH = "/v1/chat/completions"


def normalize_base_url(value: str | None) -> str:
    """Accept only ``http://127.0.0.1:<port>`` or ``http://[::1]:<port>`` (no userinfo,
    query, fragment or path)."""
    if not value:
        raise ValueError("A loopback base URL is required.")
    parts = urlsplit(value.strip())
    if parts.scheme != "http" or parts.username or parts.password or "@" in parts.netloc:
        raise ValueError("The loopback base URL must be plain http without userinfo.")
    if parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise ValueError("The loopback base URL must not carry a path, query or fragment.")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("The loopback base URL has an invalid port.") from error
    host = parts.hostname
    if port is None or host not in {"127.0.0.1", "::1"}:
        raise ValueError("Only 127.0.0.1 or [::1] with an explicit port are allowed.")
    return f"http://{'[::1]' if host == '::1' else host}:{port}"


class LoopbackGenerator:
    def __init__(
        self,
        base_url: str | None,
        model: str,
        max_output_tokens: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._error: str | None = None
        try:
            self._base_url: str | None = normalize_base_url(base_url)
        except ValueError as error:
            self._base_url, self._error = None, str(error)
        self._model, self._max_output_tokens = model, max_output_tokens
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def config(self) -> ProviderConfig:
        return ProviderConfig(
            "local",
            self._model,
            generation_policy(self._max_output_tokens),
            ADAPTER_VERSION,
            {"httpx": package_version("httpx")},
            self._base_url,
        )

    def status(self) -> dict[str, object]:
        return {
            "label": "loopback endpoint",
            "base_url": self._base_url,
            "configured": self._base_url is not None and bool(self._model),
            "reason": self._error,
        }

    def render(
        self, instructions: str, data: str, schema: dict[str, Any], max_output_tokens: int
    ) -> CanonicalOutbound:
        return CanonicalOutbound(
            "local",
            self._model,
            {
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": UNTRUSTED_PREFIX + data},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema},
                },
            },
            {"temperature": 0, "max_tokens": max_output_tokens, "stream": False},
        )

    def _get_client(self, timeout: float) -> httpx.AsyncClient:
        if self._base_url is None:
            raise ProviderCallError("configuration_invalid")
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                trust_env=False,
                follow_redirects=False,
                transport=self._transport or httpx.AsyncHTTPTransport(retries=0),
                timeout=timeout,
            )
        return self._client

    async def send(
        self, canonical: CanonicalOutbound, ticket: GateTicket, timeout_seconds: float
    ) -> ProviderResult:
        ticket.redeem(canonical)
        client = self._get_client(timeout_seconds)
        payload = canonical_json({"model": canonical.model, **canonical.body, **canonical.settings})
        try:
            async with client.stream(
                "POST",
                _PATH,
                content=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout_seconds,
            ) as response:
                status = response.status_code
                headers = response.headers
                if status == 429:
                    raise ProviderCallError("rate_limited", retry_after(headers), status=status)
                if status == 503:
                    raise ProviderCallError(
                        "provider_unavailable", retry_after(headers), status=status
                    )
                if status in (400, 413):
                    raise ProviderCallError("context_rejected", status=status)
                if status in (401, 403, 404):
                    raise ProviderCallError("configuration_invalid", status=status)
                if status != 200:
                    raise ProviderCallError("provider_unavailable", status=status)
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > RESPONSE_CAP_BYTES:
                        raise ProviderCallError("provider_unavailable", status=status)
        except httpx.TimeoutException as error:
            raise ProviderCallError("timeout", error=error) from error
        except httpx.HTTPError as error:
            raise ProviderCallError("provider_unavailable", error=error) from error
        try:
            parsed = json.loads(bytes(body))
            choice = parsed["choices"][0]
            text = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ProviderCallError("provider_unavailable", status=200, error=error) from error
        finish = choice.get("finish_reason")
        usage = parsed.get("usage") or {}
        state: Literal["completed", "incomplete", "refused"] = (
            "completed"
            if finish == "stop"
            else "refused"
            if finish == "content_filter"
            else "incomplete"
        )
        return ProviderResult(
            text if isinstance(text, str) else "",
            state,
            parsed.get("model") if isinstance(parsed.get("model"), str) else None,
            usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None,
            usage.get("completion_tokens")
            if isinstance(usage.get("completion_tokens"), int)
            else None,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
