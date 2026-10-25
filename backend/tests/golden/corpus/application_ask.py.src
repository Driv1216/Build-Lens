"""Single-turn, authority-checked grounded question answering."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import asdict
from typing import Protocol
from uuid import uuid4

from buildlens.application.context import ContextService
from buildlens.application.grounded_prompt import (
    PROMPT_VERSION,
    CitationInvalidError,
    OutputInvalidError,
    validate_output,
)
from buildlens.application.observation import capture, emit
from buildlens.domain.ask import (
    AskRequest,
    GenerationPolicy,
    GenerationResponse,
    GroundedAskResult,
    GroundedPrompt,
    ModelContract,
    PromptMeasurement,
    ResolvedCitation,
)
from buildlens.domain.context import ContextDecision, ContextEvidenceItem
from buildlens.domain.source import ContentRejectedError, screen_content


class AskError(Exception):
    code = "GENERATION_UNAVAILABLE"


class GenerationNotConfiguredError(AskError):
    code = "GENERATION_NOT_CONFIGURED"


class GenerationConfigurationInvalidError(AskError):
    code = "GENERATION_CONFIGURATION_INVALID"


class GenerationUnavailableError(AskError):
    code = "GENERATION_UNAVAILABLE"


class GenerationRateLimitedError(AskError):
    code = "GENERATION_RATE_LIMITED"


class AskTimeoutError(AskError):
    code = "ASK_TIMEOUT"


class TokenCountUnavailableError(AskError):
    code = "TOKEN_COUNT_UNAVAILABLE"


class TokenCountTimeoutError(AskError):
    code = "TOKEN_COUNT_TIMEOUT"


class AskBudgetExceededError(AskError):
    code = "ASK_BUDGET_EXCEEDED"


class GenerationContextRejectedError(AskError):
    code = "GENERATION_CONTEXT_REJECTED"


class GenerationOutputInvalidError(AskError):
    code = "GENERATION_OUTPUT_INVALID"


class GenerationCitationInvalidError(AskError):
    code = "GENERATION_CITATION_INVALID"


class GenerationRefusedError(AskError):
    code = "GENERATION_REFUSED"


class AskInputError(AskError):
    code = "ASK_INPUT_INVALID"


class ModelProvider(Protocol):
    @property
    def contract(self) -> ModelContract: ...
    def status(self) -> Mapping[str, object]: ...
    async def count_prompt(self, prompt: GroundedPrompt, deadline: float) -> PromptMeasurement: ...
    async def generate(
        self, prompt: GroundedPrompt, policy: GenerationPolicy, deadline: float
    ) -> GenerationResponse: ...
    async def close(self) -> None: ...


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(b"stage10.ask-digest.v1\0" + encoded).hexdigest()


def _item_projection(evidence_id: str, item: ContextEvidenceItem) -> dict[str, object]:
    chunk = item.chunk
    return {
        "evidence_id": evidence_id,
        "chunk_id": str(chunk.id),
        "stable_key": chunk.stable_key,
        "content_hash": chunk.content_hash,
        "artifact_id": str(chunk.artifact_id),
        "artifact_processing_id": str(chunk.artifact_processing_id),
        "locator": chunk.locator,
        "kind": chunk.kind,
        "ordinal": chunk.ordinal,
        "unit_anchor": chunk.unit_anchor,
        "part_index": chunk.part_index,
        "symbol_id": str(chunk.symbol_id) if chunk.symbol_id else None,
        "symbol_identity_id": str(chunk.symbol_identity_id) if chunk.symbol_identity_id else None,
        "heading_path": list(chunk.heading_path),
        "json_path": chunk.json_path,
        "span": asdict(chunk.span),
        "content": chunk.content,
    }


def prompt_projection(prompt: GroundedPrompt) -> dict[str, object]:
    return {
        "version": prompt.version,
        "question": prompt.question,
        "evidence": [_item_projection(key, item) for key, item in prompt.evidence],
    }


class AskService:
    def __init__(
        self,
        context: ContextService,
        provider: ModelProvider,
        *,
        enabled: bool,
        policy: GenerationPolicy | None = None,
    ) -> None:
        self._context = context
        self._provider = provider
        self._enabled = enabled
        self._policy = policy or GenerationPolicy()

    def status(self) -> dict[str, object]:
        local = self._provider.status()
        return {
            "enabled": self._enabled,
            "dependency_installed": bool(local["dependency_installed"]),
            "key_present": bool(local["key_present"]),
            "configured": self._enabled
            and bool(local["dependency_installed"])
            and bool(local["key_present"]),
            "provider": self._provider.contract.provider,
            "model_id": self._policy.model_id,
            "policy_version": self._policy.version,
            "prompt_version": PROMPT_VERSION,
            "guidance": (
                "Enable Ask and configure the server-side "
                f"{self._provider.contract.provider} key and generation extra."
            ),
        }

    async def _count(self, prompt: GroundedPrompt, deadline: float) -> PromptMeasurement:
        call_deadline = min(deadline, time.monotonic() + self._policy.count_call_timeout_seconds)
        try:
            async with asyncio.timeout_at(call_deadline):
                value = await self._provider.count_prompt(prompt, call_deadline)
        except TimeoutError as error:
            raise TokenCountTimeoutError from error
        if (
            type(value.input_tokens) is not int
            or value.input_tokens < 0
            or not value.counting_exact
            or value.model_id != self._policy.model_id
            or value.counting_method != self._provider.contract.counting_method
        ):
            raise TokenCountUnavailableError
        emit("count_measurement", value)
        return value

    async def ask(self, request: AskRequest) -> GroundedAskResult:
        started = time.monotonic()
        deadline = started + self._policy.ask_timeout_seconds
        if not self.status()["configured"]:
            raise GenerationNotConfiguredError
        if not request.question.strip() or len(request.question.encode()) > 4096:
            raise AskInputError
        try:
            screen_content(request.question)
        except ContentRejectedError as error:
            raise AskInputError from error
        try:
            async with asyncio.timeout_at(min(deadline, started + 120)):
                context = await asyncio.to_thread(
                    self._context.build,
                    request.project_id,
                    request.snapshot_id,
                    request.question,
                    request.retrieval_mode,
                    request.ranking_policy,
                    request.filters,
                )
        except TimeoutError as error:
            raise AskTimeoutError from error
        emit("ask_context", context)
        context_seconds = time.monotonic() - started
        original = tuple((f"E{index}", item) for index, item in enumerate(context.items, 1))
        original_digest = _digest(
            {
                "authority": str(context.processing_run.id),
                "embedding_authority": str(context.embedding_run.id)
                if context.embedding_run
                else None,
                "question": request.question,
                "retrieval": asdict(context.retrieval),
                "ranking_policy": context.ranking_policy,
                "evidence": [_item_projection(k, v) for k, v in original],
            }
        )
        request_id = uuid4()
        model = self._provider.contract
        if not original:
            return GroundedAskResult(
                request_id,
                "insufficient_evidence",
                "no_usable_evidence",
                "No usable evidence was found for this question.",
                (),
                (),
                (),
                context,
                (),
                self._policy,
                model,
                None,
                "skipped_no_evidence",
                original_digest,
                original_digest,
                None,
                None,
                None,
                None,
                None,
                context_seconds,
                0,
                0,
                time.monotonic() - started,
            )
        await self._verify(context, context.items, deadline)
        counting_started = time.monotonic()
        count_deadline = min(deadline, counting_started + self._policy.counting_timeout_seconds)
        fixed = await self._count(GroundedPrompt(PROMPT_VERSION, "", ()), count_deadline)
        base = await self._count(
            GroundedPrompt(PROMPT_VERSION, request.question, ()), count_deadline
        )
        if base.input_tokens > self._policy.input_limit:
            raise AskBudgetExceededError
        selected = original
        full_prompt = GroundedPrompt(PROMPT_VERSION, request.question, selected)
        measured = await self._count(full_prompt, count_deadline)
        decisions: tuple[ContextDecision, ...] = ()
        if measured.input_tokens > self._policy.input_limit:
            by_id = {item.chunk.id: key for key, item in original}
            accepted_measurement = base

            async def fits(items: tuple[ContextEvidenceItem, ...]) -> bool:
                nonlocal accepted_measurement
                proposed = GroundedPrompt(
                    PROMPT_VERSION,
                    request.question,
                    tuple((by_id[item.chunk.id], item) for item in items),
                )
                proposed_measurement = await self._count(proposed, count_deadline)
                accepted = proposed_measurement.input_tokens <= self._policy.input_limit
                if accepted:
                    accepted_measurement = proposed_measurement
                return accepted

            packed, decisions = await self._context.repack(context, fits)
            if not packed:
                raise AskBudgetExceededError
            selected = tuple((by_id[item.chunk.id], item) for item in packed)
            full_prompt = GroundedPrompt(PROMPT_VERSION, request.question, selected)
            measured = accepted_measurement
        if measured.input_tokens > self._policy.input_limit or (
            measured.input_tokens + self._policy.max_output_tokens > self._policy.context_limit
        ):
            raise AskBudgetExceededError
        counting_seconds = time.monotonic() - counting_started
        await self._verify(context, tuple(item for _, item in selected), deadline)
        emit("generation_evidence", full_prompt)
        emit("prompt_measurement", measured)
        emit("generation_decisions", decisions)
        generation_started = time.monotonic()
        try:
            async with asyncio.timeout_at(
                min(deadline, generation_started + self._policy.generation_timeout_seconds)
            ):
                generated = await self._provider.generate(
                    full_prompt,
                    self._policy,
                    min(deadline, generation_started + self._policy.generation_timeout_seconds),
                )
        except TimeoutError as error:
            raise AskTimeoutError from error
        generation_seconds = time.monotonic() - generation_started
        capture(
            "generation_response_metadata",
            lambda: {
                "state": generated.state,
                "returned_model": generated.returned_model,
                "input_tokens": generated.input_tokens,
                "output_tokens": generated.output_tokens,
                "generation_seconds": generation_seconds,
            },
        )
        if generated.state == "refused":
            raise GenerationRefusedError
        if generated.state != "completed":
            raise GenerationOutputInvalidError
        if any(
            value is not None and (type(value) is not int or value < 0)
            for value in (generated.input_tokens, generated.output_tokens)
        ):
            raise GenerationOutputInvalidError
        try:
            status, statements = validate_output(
                generated.raw_text, tuple(key for key, _ in selected)
            )
        except CitationInvalidError as error:
            raise GenerationCitationInvalidError from error
        except OutputInvalidError as error:
            raise GenerationOutputInvalidError from error
        evidence = tuple(ResolvedCitation(key, item) for key, item in selected)
        evidence_map = {citation.evidence_id: citation for citation in evidence}
        used = tuple(
            dict.fromkeys(key for statement in statements for key in statement.evidence_ids)
        )
        citations = tuple(evidence_map[key] for key in used)
        answer_text = (
            "\n".join(
                ("Inference: " if statement.support == "inference" else "") + statement.text
                for statement in statements
            )
            if statements
            else "The supplied evidence does not support an answer."
        )
        capture(
            "validated_answer",
            lambda: {
                "status": status,
                "answer_text": answer_text,
                "statements": statements,
                "citations": citations,
            },
        )
        await self._verify(context, tuple(item for _, item in selected), deadline)
        final_digest = _digest(
            {
                "authority": str(context.processing_run.id),
                "embedding_authority": str(context.embedding_run.id)
                if context.embedding_run
                else None,
                "question": request.question,
                "prompt_version": PROMPT_VERSION,
                "evidence": [_item_projection(k, v) for k, v in selected],
            }
        )
        return GroundedAskResult(
            request_id,
            "answered" if status == "answered" else "insufficient_evidence",
            "evidence_does_not_support_answer" if status == "insufficient_evidence" else None,
            answer_text,
            statements,
            citations,
            evidence,
            context,
            decisions,
            self._policy,
            model,
            generated.returned_model,
            "completed",
            original_digest,
            final_digest,
            fixed.input_tokens,
            base.input_tokens,
            measured,
            generated.input_tokens,
            generated.output_tokens,
            context_seconds,
            counting_seconds,
            generation_seconds,
            time.monotonic() - started,
        )

    async def _verify(
        self, context: object, items: tuple[ContextEvidenceItem, ...], deadline: float
    ) -> None:
        from buildlens.domain.context import ContextResult

        assert isinstance(context, ContextResult)
        try:
            async with asyncio.timeout_at(
                min(deadline, time.monotonic() + self._policy.verification_timeout_seconds)
            ):
                await asyncio.to_thread(
                    self._context.verify_result,
                    context,
                    items,
                    deadline=min(
                        deadline, time.monotonic() + self._policy.verification_timeout_seconds
                    ),
                )
        except TimeoutError as error:
            raise AskTimeoutError from error
