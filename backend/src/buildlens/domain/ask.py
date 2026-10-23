"""Provider-independent single-turn grounded answer contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias
from uuid import UUID

from buildlens.domain.context import (
    ContextDecision,
    ContextEvidenceItem,
    ContextResult,
    RankingPolicy,
)
from buildlens.domain.retrieval import RetrievalFilters, RetrievalMode

AnswerStatus: TypeAlias = Literal["answered", "insufficient_evidence"]
SupportKind: TypeAlias = Literal["direct", "inference"]


@dataclass(frozen=True, slots=True)
class AskRequest:
    project_id: UUID
    snapshot_id: UUID
    question: str
    retrieval_mode: RetrievalMode = "hybrid"
    ranking_policy: RankingPolicy = "local_cross_encoder"
    filters: RetrievalFilters = RetrievalFilters()


@dataclass(frozen=True, slots=True)
class GenerationPolicy:
    version: str = "stage10.generation.v1"
    model_id: str = "gpt-6-luna"
    context_limit: int = 1_050_000
    combined_limit: int = 32_768
    max_output_tokens: int = 2_048
    reasoning_effort: Literal["none"] | None = "none"
    temperature: int | None = 0
    thinking_level: Literal["medium"] | None = None
    truncation: Literal["disabled"] = "disabled"
    provider_store: bool = False
    automatic_retries: int = 0
    streaming: bool = False
    tools_enabled: bool = False
    ask_timeout_seconds: float = 240
    counting_timeout_seconds: float = 30
    count_call_timeout_seconds: float = 5
    generation_timeout_seconds: float = 60
    verification_timeout_seconds: float = 10

    @classmethod
    def for_provider(cls, provider: str) -> GenerationPolicy:
        if provider == "openai":
            return cls()
        if provider == "gemini":
            return cls(
                version="stage10.gemini-generation.v1",
                model_id="gemini-3.8-flash",
                context_limit=1_048_576,
                reasoning_effort=None,
                temperature=None,
                thinking_level="medium",
            )
        raise ValueError("Unknown generation provider")

    @property
    def input_limit(self) -> int:
        return min(self.context_limit, self.combined_limit) - self.max_output_tokens


@dataclass(frozen=True, slots=True)
class ModelContract:
    provider: str
    model_id: str
    adapter_version: str
    sdk_version: str | None
    counting_method: str
    counting_exact: bool
    context_limit: int = 1_050_000
    maximum_model_output_tokens: int = 128_000
    strict_json_schema: bool = True


@dataclass(frozen=True, slots=True)
class GroundedPrompt:
    version: str
    question: str
    evidence: tuple[tuple[str, ContextEvidenceItem], ...]


@dataclass(frozen=True, slots=True)
class PromptMeasurement:
    prepared_request_digest: str
    input_tokens: int
    counting_method: str
    counting_exact: bool
    model_id: str
    adapter_version: str
    tokenizer_revision: None = None


@dataclass(frozen=True, slots=True)
class GenerationResponse:
    raw_text: str
    state: Literal["completed", "incomplete", "refused"]
    returned_model: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class AnswerStatement:
    text: str
    support: SupportKind
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedCitation:
    evidence_id: str
    item: ContextEvidenceItem


@dataclass(frozen=True, slots=True)
class GroundedAskResult:
    request_id: UUID
    status: AnswerStatus
    reason: Literal["no_usable_evidence", "evidence_does_not_support_answer"] | None
    answer_text: str
    statements: tuple[AnswerStatement, ...]
    citations: tuple[ResolvedCitation, ...]
    evidence: tuple[ResolvedCitation, ...]
    context: ContextResult
    generation_decisions: tuple[ContextDecision, ...]
    policy: GenerationPolicy
    model: ModelContract
    returned_model: str | None
    generation_state: Literal["completed", "skipped_no_evidence"]
    original_context_digest: str
    evidence_digest: str
    fixed_tokens: int | None
    question_tokens: int | None
    prompt: PromptMeasurement | None
    usage_input_tokens: int | None
    usage_output_tokens: int | None
    context_seconds: float
    counting_seconds: float
    generation_seconds: float
    total_seconds: float
