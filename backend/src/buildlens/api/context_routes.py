"""Stage 9 bounded context and source-free reranker inspection routes."""

from dataclasses import asdict
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends

from buildlens.api.context_schemas import (
    ContextBudgetResponse,
    ContextDecisionResponse,
    ContextEvidenceResponse,
    ContextMeasurementResponse,
    ContextPolicyResponse,
    ContextRequest,
    ContextResponse,
    ContextRetrievalResponse,
    RerankerContractResponse,
    RerankerStatusResponse,
)
from buildlens.api.dependencies import get_context_service
from buildlens.api.embedding_routes import contract as embedding_contract
from buildlens.api.embedding_routes import run as embedding_run
from buildlens.api.processing_routes import span
from buildlens.api.retrieval_routes import hit
from buildlens.application.context import ContextService
from buildlens.domain.context import ContextResult, RerankerContract
from buildlens.domain.retrieval import RetrievalFilters

router = APIRouter(prefix="/api/v1/projects/{project_id}/snapshots/{snapshot_id}", tags=["context"])
runtime_router = APIRouter(prefix="/api/v1", tags=["context"])


def _contract(value: RerankerContract) -> RerankerContractResponse:
    return RerankerContractResponse(**asdict(value))


def response(value: ContextResult) -> ContextResponse:
    budget = value.budget
    items = []
    for item in value.items:
        chunk = item.chunk
        items.append(
            ContextEvidenceResponse(
                artifact_processing_id=chunk.artifact_processing_id,
                artifact_id=cast(UUID, chunk.artifact_id),
                locator=cast(str, chunk.locator),
                chunk_id=chunk.id,
                stable_key=chunk.stable_key,
                content_hash=chunk.content_hash,
                kind=chunk.kind,
                ordinal=chunk.ordinal,
                unit_anchor=chunk.unit_anchor,
                part_index=chunk.part_index,
                span=span(chunk.span),
                symbol_id=chunk.symbol_id,
                symbol_identity_id=chunk.symbol_identity_id,
                heading_path=list(chunk.heading_path),
                json_path=chunk.json_path,
                content=chunk.content,
                retrieval=(
                    hit(item.retrieval, value.retrieval.executed_mode == "hybrid")
                    if item.retrieval
                    else None
                ),
                retrieved_directly=item.retrieved_directly,
                selected_as_seed=item.selected_as_seed,
                expanded_from=list(item.expanded_from),
                rerank_score=item.rerank_score,
                rerank_rank=item.rerank_rank,
                rerank_window_count=item.rerank_window_count,
                rerank_winning_window=item.rerank_winning_window,
                context_cost=item.context_cost,
            )
        )
    return ContextResponse(
        project_id=value.project_id,
        snapshot_id=value.snapshot_id,
        query=value.query,
        ranking_policy=value.ranking_policy,
        policy=ContextPolicyResponse(**asdict(value.policy)),
        processing_run_id=value.processing_run.id,
        processing_revision=value.processing_run.revision,
        processing_contract=value.processing_run.processing_contract,
        embedding_run_id=value.embedding_run.id if value.embedding_run else None,
        embedding_run=embedding_run(value.embedding_run) if value.embedding_run else None,
        embedding_contract=(
            embedding_contract(value.embedding_run.contract) if value.embedding_run else None
        ),
        retrieval=ContextRetrievalResponse(**asdict(value.retrieval)),
        reranker_state=value.reranker_state,
        reranker_contract=_contract(value.reranker_contract) if value.reranker_contract else None,
        item_count=len(value.items),
        items=items,
        decisions=[ContextDecisionResponse(**asdict(item)) for item in value.decisions],
        budget=ContextBudgetResponse(
            measurement=ContextMeasurementResponse(**asdict(budget.measurement)),
            limit=budget.limit,
            used=budget.used,
            framing_cost=budget.framing_cost,
            remaining=budget.remaining,
            per_item_costs=dict(budget.per_item_costs),
            generation_token_count=None,
        ),
    )


@router.post("/context", response_model=ContextResponse)
def build_context(
    project_id: UUID,
    snapshot_id: UUID,
    request: ContextRequest,
    service: ContextService = Depends(get_context_service),
) -> ContextResponse:
    selected = request.filters
    value = service.build(
        project_id,
        snapshot_id,
        request.query,
        request.retrieval_mode,
        request.ranking_policy,
        RetrievalFilters(
            locator_prefix=selected.locator_prefix if selected else None,
            language=selected.language if selected else None,
            kind=selected.kind if selected else None,
        ),
    )
    return response(value)


@runtime_router.get("/reranker", response_model=RerankerStatusResponse)
def reranker_status(
    service: ContextService = Depends(get_context_service),
) -> RerankerStatusResponse:
    status, contract = service.status()
    return RerankerStatusResponse(
        available=status.available,
        path=status.path,
        reason=status.reason,
        contract=_contract(contract),
    )
