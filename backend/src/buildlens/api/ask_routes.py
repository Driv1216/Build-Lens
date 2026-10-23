"""Local-only single-turn Ask and configuration inspection routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, field_validator

from buildlens.api.context_routes import response as context_response
from buildlens.api.context_schemas import (
    ContextDecisionResponse,
    ContextEvidenceResponse,
    ContextResponse,
)
from buildlens.api.dependencies import get_ask_service
from buildlens.api.retrieval_schemas import RetrievalFiltersRequest
from buildlens.application.ask import AskService
from buildlens.domain.ask import AskRequest, GroundedAskResult, ResolvedCitation
from buildlens.domain.retrieval import RetrievalFilters

router = APIRouter(prefix="/api/v1/projects/{project_id}/snapshots/{snapshot_id}", tags=["ask"])
runtime_router = APIRouter(prefix="/api/v1", tags=["ask"])


class AskRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str
    retrieval_mode: Literal["exact", "lexical", "semantic", "hybrid"] = "hybrid"
    ranking_policy: Literal["local_cross_encoder", "retrieval_order"] = "local_cross_encoder"
    filters: RetrievalFiltersRequest | None = None

    @field_validator("question")
    @classmethod
    def valid_question(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > 4096:
            raise ValueError("Question must be nonblank and at most 4096 UTF-8 bytes.")
        return value


class AskEvidenceResponse(BaseModel):
    evidence_id: str
    project_id: UUID
    snapshot_id: UUID
    processing_run_id: UUID
    processing_revision: int
    embedding_run_id: UUID | None
    item: ContextEvidenceResponse
    coordinate_convention: str = "decoded-unicode-half-open.v1"


class AskStatementResponse(BaseModel):
    text: str
    support: Literal["direct", "inference"]
    evidence_ids: list[str]


class AskAnswerResponse(BaseModel):
    text: str
    statements: list[AskStatementResponse]


class AskResponse(BaseModel):
    request_id: UUID
    status: Literal["answered", "insufficient_evidence"]
    reason: Literal["no_usable_evidence", "evidence_does_not_support_answer"] | None
    answer: AskAnswerResponse
    citations: list[AskEvidenceResponse]
    evidence: list[AskEvidenceResponse]
    context: ContextResponse
    generation_decisions: list[ContextDecisionResponse]
    generation_policy: dict[str, object]
    model_contract: dict[str, object]
    returned_model: str | None
    generation_state: Literal["completed", "skipped_no_evidence"]
    original_context_digest: str
    evidence_digest: str
    prompt_version: str = "stage10.grounded-prompt.v1"
    output_version: str = "stage10.cited-statements.v1"
    packing_version: str = "stage10.generation-packing.v1"
    fixed_tokens: int | None
    question_tokens: int | None
    prompt_measurement: dict[str, object] | None
    usage_input_tokens: int | None
    usage_output_tokens: int | None
    context_seconds: float
    counting_seconds: float
    generation_seconds: float
    total_seconds: float


def ask_response(value: GroundedAskResult) -> AskResponse:
    context = context_response(value.context)
    by_id = {item.chunk_id: item for item in context.items}

    def citation(source: ResolvedCitation) -> AskEvidenceResponse:
        return AskEvidenceResponse(
            evidence_id=source.evidence_id,
            project_id=value.context.project_id,
            snapshot_id=value.context.snapshot_id,
            processing_run_id=value.context.processing_run.id,
            processing_revision=value.context.processing_run.revision,
            embedding_run_id=value.context.embedding_run.id
            if value.context.embedding_run
            else None,
            item=by_id[source.item.chunk.id],
        )

    return AskResponse(
        request_id=value.request_id,
        status=value.status,
        reason=value.reason,
        answer=AskAnswerResponse(
            text=value.answer_text,
            statements=[
                AskStatementResponse(**asdict(statement)) for statement in value.statements
            ],
        ),
        citations=[citation(item) for item in value.citations],
        evidence=[citation(item) for item in value.evidence],
        context=context,
        generation_decisions=[
            ContextDecisionResponse(**asdict(item)) for item in value.generation_decisions
        ],
        generation_policy=asdict(value.policy),
        model_contract=asdict(value.model),
        returned_model=value.returned_model,
        generation_state=value.generation_state,
        original_context_digest=value.original_context_digest,
        evidence_digest=value.evidence_digest,
        fixed_tokens=value.fixed_tokens,
        question_tokens=value.question_tokens,
        prompt_measurement=asdict(value.prompt) if value.prompt else None,
        usage_input_tokens=value.usage_input_tokens,
        usage_output_tokens=value.usage_output_tokens,
        context_seconds=value.context_seconds,
        counting_seconds=value.counting_seconds,
        generation_seconds=value.generation_seconds,
        total_seconds=value.total_seconds,
    )


@router.post("/ask", response_model=AskResponse)
async def ask(
    project_id: UUID,
    snapshot_id: UUID,
    body: AskRequestBody,
    response: Response,
    service: AskService = Depends(get_ask_service),
) -> AskResponse:
    response.headers["Cache-Control"] = "no-store"
    selected = body.filters
    request = AskRequest(
        project_id,
        snapshot_id,
        body.question,
        body.retrieval_mode,
        body.ranking_policy,
        RetrievalFilters(
            selected.locator_prefix if selected else None,
            selected.language if selected else None,
            selected.kind if selected else None,
        ),
    )
    return ask_response(await service.ask(request))


@runtime_router.get("/generation")
def generation_status(service: AskService = Depends(get_ask_service)) -> dict[str, object]:
    return service.status()
