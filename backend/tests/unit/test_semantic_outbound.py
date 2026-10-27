"""Stage 13 Milestone C: gate-only outbound requests and no implicit retries (§11.2, O1)."""

from __future__ import annotations

import ast
import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from buildlens.application.outbound_gate import (
    CanonicalOutbound,
    OutboundGate,
    OutboundRefusedError,
    ProviderCallError,
    StructuredGenerator,
)
from buildlens.application.semantic_prompt import FULL_INSTRUCTIONS, OUTPUT_SCHEMA
from buildlens.infrastructure.generation.semantic_local import LoopbackGenerator, normalize_base_url

SRC = Path(__file__).parents[2] / "src" / "buildlens"
CLAIMS = json.dumps({"claims": [], "unknowns": []})


class Counter:
    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def _chat(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "returned-model",
            "choices": [{"message": {"content": CLAIMS}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    )


def _openai_response(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_1",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": "gpt-returned",
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": CLAIMS, "annotations": []}],
                }
            ],
            "parallel_tool_calls": False,
            "tool_choice": "none",
            "tools": [],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "total_tokens": 12,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    )


def _gemini_response(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": CLAIMS}]}, "finishReason": "STOP"}
            ],
            "modelVersion": "gemini-returned",
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2},
        },
    )


def _rate_limited(_: httpx.Request) -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"message": "slow"}})


def _generators(
    respond: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> list[tuple[str, StructuredGenerator, Counter]]:
    values: list[tuple[str, StructuredGenerator, Counter]] = []
    counter = Counter(respond["local"])
    values.append(
        (
            "local",
            LoopbackGenerator(
                "http://127.0.0.1:9", "stub", 256, transport=httpx.MockTransport(counter)
            ),
            counter,
        )
    )
    pytest.importorskip("openai")
    from buildlens.infrastructure.generation.semantic_openai import OpenAISemanticGenerator

    counter = Counter(respond["openai"])
    values.append(
        (
            "openai",
            OpenAISemanticGenerator(
                "test-key",
                "gpt-6-luna",
                256,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(counter)),
            ),
            counter,
        )
    )
    pytest.importorskip("google.genai")
    from buildlens.infrastructure.generation.semantic_gemini import GeminiSemanticGenerator

    counter = Counter(respond["gemini"])
    values.append(
        (
            "gemini",
            GeminiSemanticGenerator(
                "test-key",
                "gemini-3.8-flash",
                256,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(counter)),
            ),
            counter,
        )
    )
    return values


def _canonical(generator: StructuredGenerator) -> CanonicalOutbound:
    return generator.render(FULL_INSTRUCTIONS, '{"entity":{}}', OUTPUT_SCHEMA, 256)


def test_construction_status_and_render_make_no_requests() -> None:
    for _, generator, counter in _generators(
        {"local": _chat, "openai": _openai_response, "gemini": _gemini_response}
    ):
        generator.status()
        assert len(generator.config.digest) == 64
        first, second = _canonical(generator), _canonical(generator)
        assert first.digest == second.digest and first.size == len(first.data)
        assert counter.requests == []


def test_one_ticket_is_exactly_one_request_and_is_single_use() -> None:
    async def scenario() -> None:
        for name, generator, counter in _generators(
            {"local": _chat, "openai": _openai_response, "gemini": _gemini_response}
        ):
            gate = OutboundGate(uuid4(), uuid4())
            canonical = _canonical(generator)
            ticket = gate.mint(uuid4(), canonical.digest)
            result = await gate.send(generator, canonical, ticket, 5)
            assert result.raw_text == CLAIMS, name
            assert result.returned_model and result.input_tokens == 10
            assert len(counter.requests) == 1, name
            with pytest.raises(OutboundRefusedError):
                await generator.send(canonical, ticket, 5)
            other = OutboundGate(uuid4(), uuid4()).mint(uuid4(), canonical.digest)
            with pytest.raises(OutboundRefusedError):
                await gate.send(generator, canonical, replace_gate(other, gate), 5)
            changed = generator.render(FULL_INSTRUCTIONS, "{}", OUTPUT_SCHEMA, 256)
            with pytest.raises(OutboundRefusedError):
                await generator.send(changed, gate.mint(uuid4(), canonical.digest), 5)
            assert len(counter.requests) == 1, name
            assert (gate.minted, gate.redeemed) == (2, 1)
            await generator.close()

    asyncio.run(scenario())


def replace_gate(ticket: Any, gate: OutboundGate) -> Any:
    """A forged ticket: valid shape, minted by another gate."""
    from dataclasses import replace

    return replace(ticket, gate=gate)


def test_rate_limit_is_one_request_without_sdk_retry() -> None:
    async def scenario() -> None:
        for name, generator, counter in _generators(
            {"local": _rate_limited, "openai": _rate_limited, "gemini": _rate_limited}
        ):
            gate = OutboundGate(uuid4(), uuid4())
            canonical = _canonical(generator)
            with pytest.raises(ProviderCallError) as caught:
                await gate.send(generator, canonical, gate.mint(uuid4(), canonical.digest), 5)
            assert caught.value.code == "rate_limited", name
            if name != "gemini":
                assert caught.value.retry_after == 7, name
            assert len(counter.requests) == 1, name
            await generator.close()

    asyncio.run(scenario())


SECRET_ECHO = "sk-echoed-secret-in-provider-body"


def _failing(status: int) -> Callable[[httpx.Request], httpx.Response]:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"X-Echo": SECRET_ECHO},
            json={"error": {"code": status, "message": SECRET_ECHO, "status": "UNAVAILABLE"}},
        )

    return respond


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"cannot connect {SECRET_ECHO}", request=request)


def _diagnosed(respond: Callable[[httpx.Request], httpx.Response]) -> dict[str, ProviderCallError]:
    async def scenario() -> dict[str, ProviderCallError]:
        caught: dict[str, ProviderCallError] = {}
        for name, generator, counter in _generators(
            {"local": respond, "openai": respond, "gemini": respond}
        ):
            gate = OutboundGate(uuid4(), uuid4())
            canonical = _canonical(generator)
            with pytest.raises(ProviderCallError) as error:
                await gate.send(generator, canonical, gate.mint(uuid4(), canonical.digest), 5)
            assert len(counter.requests) == 1, name  # diagnostics never cost a request
            caught[name] = error.value
            await generator.close()
        return caught

    return asyncio.run(scenario())


@pytest.mark.parametrize("status", [503, 500, 429, 403])
def test_failures_record_http_status_and_class_but_no_content(status: int) -> None:
    expected = {503: "provider_unavailable", 500: "provider_unavailable",
                429: "rate_limited", 403: "configuration_invalid"}[status]  # fmt: skip
    for name, error in _diagnosed(_failing(status)).items():
        assert error.code == expected, name
        assert f"HTTP_{status}" in error.diagnostics, (name, error.diagnostics)
        if name != "local":  # SDK adapters also name the SDK exception class
            assert any(item.startswith("EXC_") for item in error.diagnostics), name
        rendered = " ".join(error.diagnostics) + str(error) + repr(error.args)
        assert SECRET_ECHO not in rendered and "UNAVAILABLE" not in rendered, name


def test_transport_failures_record_exception_classes_only() -> None:
    for name, error in _diagnosed(_unreachable).items():
        assert error.code == "provider_unavailable", name
        assert error.status is None, name
        assert any(item.startswith(("EXC_", "CAUSE_")) for item in error.diagnostics), name
        assert all(re.fullmatch(r"(EXC|CAUSE)_[A-Za-z0-9_]+", item) for item in error.diagnostics)
        assert SECRET_ECHO not in " ".join(error.diagnostics) + str(error), name


def test_diagnostics_are_bounded_tokens() -> None:
    weird = type("Bad Name!", (Exception,), {})("message with secret")
    error = ProviderCallError("provider_unavailable", status=99999, error=weird)
    assert error.diagnostics == ("EXC_UNNAMED",)
    assert ProviderCallError("timeout").diagnostics == ()


def test_loopback_transmits_exactly_the_canonical_bytes() -> None:
    counter = Counter(_chat)
    generator = LoopbackGenerator(
        "http://127.0.0.1:9", "stub", 256, transport=httpx.MockTransport(counter)
    )
    gate = OutboundGate(uuid4(), uuid4())
    canonical = _canonical(generator)
    asyncio.run(gate.send(generator, canonical, gate.mint(uuid4(), canonical.digest), 5))
    sent = json.loads(counter.requests[0].content)
    assert sent == {"model": canonical.model, **canonical.body, **canonical.settings}
    assert counter.requests[0].url.path == "/v1/chat/completions"
    assert canonical.settings["stream"] is False and canonical.settings["temperature"] == 0


def test_loopback_refuses_redirects_and_caps_responses() -> None:
    redirect = Counter(lambda _: httpx.Response(307, headers={"Location": "http://example.com/"}))
    huge = Counter(lambda _: httpx.Response(200, content=b"x" * 2_000_000))

    async def scenario(counter: Counter) -> str:
        generator = LoopbackGenerator(
            "http://127.0.0.1:9", "stub", 256, transport=httpx.MockTransport(counter)
        )
        gate = OutboundGate(uuid4(), uuid4())
        canonical = _canonical(generator)
        try:
            await gate.send(generator, canonical, gate.mint(uuid4(), canonical.digest), 5)
        except ProviderCallError as error:
            return error.code
        finally:
            await generator.close()
        return "sent"

    assert asyncio.run(scenario(redirect)) == "provider_unavailable"
    assert len(redirect.requests) == 1
    assert asyncio.run(scenario(huge)) == "provider_unavailable"


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8080",
        "http://10.0.0.5:8080",
        "https://127.0.0.1:8080",
        "http://user:pw@127.0.0.1:8080",
        "http://127.0.0.1:8080/v1",
        "http://127.0.0.1:8080?x=1",
        "http://127.0.0.1:8080#f",
        "http://127.0.0.1",
        "http://127.0.0.2:80",
        None,
    ],
)
def test_loopback_base_url_is_strict(value: str | None) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(value)
    generator = LoopbackGenerator(value, "stub", 256)
    assert generator.status()["configured"] is False


def test_loopback_base_url_normalisation_and_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert normalize_base_url("http://127.0.0.1:11434/") == "http://127.0.0.1:11434"
    assert normalize_base_url("http://[::1]:8000") == "http://[::1]:8000"
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    counter = Counter(_chat)
    generator = LoopbackGenerator(
        "http://127.0.0.1:9", "stub", 256, transport=httpx.MockTransport(counter)
    )
    gate = OutboundGate(uuid4(), uuid4())
    canonical = _canonical(generator)
    asyncio.run(gate.send(generator, canonical, gate.mint(uuid4(), canonical.digest), 5))
    assert counter.requests[0].url.host == "127.0.0.1" and counter.requests[0].url.port == 9


def _calls(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text())
    values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
            values.append((f"{name}.{node.func.attr}", node.lineno))
    return values


def test_architecture_semantic_code_reaches_providers_only_through_the_gate() -> None:
    # The service sends only via OutboundGate.send; nothing else calls a generator's send.
    service = SRC / "application" / "semantic_generation.py"
    sends = [call for call, _ in _calls(service) if call.endswith(".send")]
    assert sends == ["gate.send"]
    # Every adapter redeems its ticket before any other statement in send().
    for name in ("semantic_local.py", "semantic_openai.py", "semantic_gemini.py"):
        tree = ast.parse((SRC / "infrastructure" / "generation" / name).read_text())
        send = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "send"
        )
        first = send.body[0]
        assert isinstance(first, ast.Expr) and ast.unparse(first) == "ticket.redeem(canonical)"
    # Provider SDKs are imported only by adapters; semantic application code imports none.
    for path in [
        *(SRC / "application").glob("semantic*.py"),
        SRC / "application" / "outbound_gate.py",
    ]:
        for node in ast.walk(ast.parse(path.read_text())):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for module in modules:
                assert not module.startswith(("openai", "google", "httpx")), (path, module)
                assert "infrastructure.generation" not in module, (path, module)
    # Ask adapters are untouched by Stage 13 and share no code with semantic adapters.
    for name in ("gemini.py", "openai.py"):
        assert "semantic" not in (SRC / "infrastructure" / "generation" / name).read_text()
