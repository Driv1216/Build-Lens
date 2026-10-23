from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Literal
from uuid import UUID

import pytest

from buildlens.application.ask import (
    AskBudgetExceededError,
    AskService,
    GenerationCitationInvalidError,
    GenerationOutputInvalidError,
)
from buildlens.application.context import ContextConflictError
from buildlens.application.evaluation import Collector
from buildlens.application.grounded_prompt import (
    CitationInvalidError,
    OutputInvalidError,
    validate_output,
)
from buildlens.application.observation import observing
from buildlens.domain.ask import (
    AskRequest,
    GenerationPolicy,
    GenerationResponse,
    GroundedAskResult,
    GroundedPrompt,
    ModelContract,
    PromptMeasurement,
)
from buildlens.domain.context import ContextEvidenceItem, ContextResult
from tests.unit.test_context import FakeReranker, chunk, service


class FakeProvider:
    contract = ModelContract("fake", "gpt-6-luna", "fake.v1", "0", "fake.full-request", True)

    def __init__(
        self,
        text: str = (
            '{"status":"answered","statements":[{"text":"Shown.",'
            '"support":"direct","evidence_ids":["E1"]}]}'
        ),
        *,
        item_cost: int = 100,
    ) -> None:
        self.text = text
        self.item_cost = item_cost
        self.counts: list[GroundedPrompt] = []
        self.generations: list[GroundedPrompt] = []
        self.state: Literal["completed", "incomplete", "refused"] = "completed"

    def status(self) -> dict[str, object]:
        return {"dependency_installed": True, "key_present": True}

    async def count_prompt(self, prompt: GroundedPrompt, deadline: float) -> PromptMeasurement:
        self.counts.append(prompt)
        return PromptMeasurement(
            str(len(self.counts)),
            100 + (10 if prompt.question else 0) + self.item_cost * len(prompt.evidence),
            self.contract.counting_method,
            True,
            self.contract.model_id,
            self.contract.adapter_version,
        )

    async def generate(
        self, prompt: GroundedPrompt, policy: GenerationPolicy, deadline: float
    ) -> GenerationResponse:
        self.generations.append(prompt)
        return GenerationResponse(self.text, self.state, self.contract.model_id, 120, 30)

    async def close(self) -> None:
        pass


def question(
    provider: FakeProvider, contents: list[str], policy: GenerationPolicy | None = None
) -> GroundedAskResult:
    chunks = [chunk(i, content, start=i * 100) for i, content in enumerate(contents, 1)]
    context = service(chunks, FakeReranker())
    ask = AskService(context, provider, enabled=True, policy=policy)
    return asyncio.run(
        ask.ask(
            AskRequest(UUID(int=500), UUID(int=400), "What is shown?", "lexical", "retrieval_order")
        )
    )


def test_empty_context_never_contacts_provider() -> None:
    provider = FakeProvider()
    result = question(provider, [])
    assert result.status == "insufficient_evidence"
    assert result.reason == "no_usable_evidence"
    assert not provider.counts and not provider.generations


def test_answered_and_abstention_are_distinct() -> None:
    provider = FakeProvider()
    result = question(provider, ["Shown."])
    assert result.status == "answered"
    assert result.citations[0].evidence_id == "E1"
    assert result.citations[0].item.chunk.content == "Shown."
    assert result.prompt and result.prompt.input_tokens == 210
    abstaining = FakeProvider('{"status":"insufficient_evidence","statements":[]}')
    result = question(abstaining, ["Unrelated."])
    assert result.status == "insufficient_evidence"
    assert result.reason == "evidence_does_not_support_answer"
    assert not result.citations


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{",
        '{"status":"answered","status":"answered","statements":[]}',
        '{"status":"answered","statements":[],"answer":"invented"}',
        '{"status":"answered","statements":[{"text":"x","support":"direct","evidence_ids":[]}]}',
    ],
)
def test_invalid_output_rejected(raw: str) -> None:
    provider = FakeProvider(raw)
    with pytest.raises((GenerationOutputInvalidError, GenerationCitationInvalidError)):
        question(provider, ["Evidence."])


@pytest.mark.parametrize("ids", [["E9"], ["E1", "E1"]])
def test_invalid_citations_rejected(ids: list[str]) -> None:
    provider = FakeProvider(
        json.dumps(
            {
                "status": "answered",
                "statements": [{"text": "Claim", "support": "direct", "evidence_ids": ids}],
            }
        )
    )
    with pytest.raises(GenerationCitationInvalidError):
        question(provider, ["Evidence."])


def test_repack_keeps_ids_and_skips_nonfitting_items() -> None:
    provider = FakeProvider(
        '{"status":"answered","statements":[{"text":"Shown.","support":"direct","evidence_ids":["E1"]}]}'
    )
    policy = GenerationPolicy(combined_limit=340, max_output_tokens=100)
    result = question(provider, ["One", "Two", "Three"], policy)
    assert [item.evidence_id for item in result.evidence] == ["E1"]
    assert [item.reason for item in result.generation_decisions] == [
        "generation_budget",
        "generation_budget",
    ]
    assert len(provider.counts) == 6
    assert len(provider.generations) == 1


def test_repacked_out_id_is_rejected() -> None:
    provider = FakeProvider(
        '{"status":"answered","statements":[{"text":"Shown.","support":"direct","evidence_ids":["E2"]}]}'
    )
    policy = GenerationPolicy(combined_limit=340, max_output_tokens=100)
    with pytest.raises(GenerationCitationInvalidError):
        question(provider, ["One", "Two"], policy)


def test_no_whole_item_fit_is_budget_failure() -> None:
    provider = FakeProvider()
    policy = GenerationPolicy(combined_limit=250, max_output_tokens=100)
    with pytest.raises(AskBudgetExceededError):
        question(provider, ["Evidence"], policy)
    assert not provider.generations


@pytest.mark.parametrize("verification_call", [1, 2, 3])
def test_authority_change_discards_work(verification_call: int) -> None:
    provider = FakeProvider()
    context = service([chunk(1, "Evidence")], FakeReranker())
    original = context.verify_result
    calls = 0

    def verify(
        result: ContextResult,
        items: tuple[ContextEvidenceItem, ...] | None = None,
        *,
        deadline: float | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == verification_call:
            raise ContextConflictError("Synthetic authority change.")
        original(result, items, deadline=deadline)

    context.verify_result = verify  # type: ignore[method-assign]
    ask = AskService(context, provider, enabled=True)
    collector = Collector()
    with observing(collector), pytest.raises(ContextConflictError):
        asyncio.run(
            ask.ask(
                AskRequest(
                    UUID(int=500), UUID(int=400), "What is shown?", "lexical", "retrieval_order"
                )
            )
        )
    assert ("validated_answer" in collector.values) == (verification_call == 3)
    assert ("generation_response_metadata" in collector.values) == (verification_call == 3)
    assert len(provider.generations) == (1 if verification_call == 3 else 0)
    assert len(provider.counts) == (0 if verification_call == 1 else 3)


def test_repack_drops_neighbour_when_its_seed_does_not_survive() -> None:
    first = chunk(1, "One", part=0, start=0)
    second = replace(chunk(2, "Two", part=0, start=100), unit_anchor="other")
    neighbour = chunk(3, "Neighbour", part=1, start=3)
    context = service([first, second], FakeReranker(), neighbours=(neighbour,))
    built = context.build(UUID(int=500), UUID(int=400), "question", "lexical", "retrieval_order")
    assert [item.chunk.id for item in built.items] == [first.id, second.id, neighbour.id]

    async def fits(items: tuple[ContextEvidenceItem, ...]) -> bool:
        return first.id not in {item.chunk.id for item in items}

    packed, decisions = asyncio.run(context.repack(built, fits))
    assert [item.chunk.id for item in packed] == [second.id]
    assert [(item.chunk_id, item.reason) for item in decisions] == [
        (first.id, "generation_budget"),
        (neighbour.id, "parent_not_included"),
    ]


def test_output_validator_preserves_statement_order_and_normalizes_citations() -> None:
    raw = json.dumps(
        {
            "status": "answered",
            "statements": [
                {"text": "Inferred", "support": "inference", "evidence_ids": ["E3", "E1"]},
                {"text": "Direct", "support": "direct", "evidence_ids": ["E1"]},
            ],
        }
    )
    status, statements = validate_output(raw, ("E1", "E3"))
    assert status == "answered"
    assert statements[0].evidence_ids == ("E1", "E3")
    assert statements[1].evidence_ids == ("E1",)
    with pytest.raises(OutputInvalidError):
        validate_output('{"status":"answered","status":"answered","statements":[]}', ("E1",))
    with pytest.raises(CitationInvalidError):
        validate_output(
            '{"status":"answered","statements":[{"text":"x","support":"direct","evidence_ids":["E2"]}]}',
            ("E1",),
        )
