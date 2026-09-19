"""Common Stage 8 retrieval HTTP contract."""

from typing import cast
from uuid import UUID

from fastapi import Depends

from buildlens.api.dependencies import get_retrieval_service
from buildlens.api.embedding_routes import contract, router, run
from buildlens.api.processing_routes import span
from buildlens.api.retrieval_schemas import (
    ComponentStatusResponse,
    ExactMatchResponse,
    HybridProvenanceResponse,
    LexicalProvenanceResponse,
    RetrievalComponentsResponse,
    RetrievalHitResponse,
    RetrievalPolicyResponse,
    RetrievalRequest,
    RetrievalResponse,
    SemanticProvenanceResponse,
)
from buildlens.application.retrieval import RetrievalService
from buildlens.domain.retrieval import (
    CANDIDATE_DEPTH,
    RETRIEVAL_POLICY_VERSION,
    RRF_CONSTANT,
    ComponentStatus,
    RetrievalHit,
    RetrievalResult,
)


def component(value: ComponentStatus) -> ComponentStatusResponse:
    return ComponentStatusResponse(
        state=value.state,
        candidate_count=value.candidate_count,
        truncated=value.truncated,
    )


def hit(value: RetrievalHit, hybrid: bool) -> RetrievalHitResponse:
    lexical = value.lexical
    semantic = value.semantic
    embedded = semantic.embedding if semantic else None
    return RetrievalHitResponse(
        rank=value.rank,
        score_kind=value.score_kind,
        score=value.score,
        exact_rank=value.exact_rank,
        exact_matches=[
            ExactMatchResponse(field=item.field, value=item.value) for item in value.exact_matches
        ],
        lexical=(
            LexicalProvenanceResponse(
                rank=lexical.rank,
                score=float(lexical.score or 0.0),
                field_scores=lexical.field_scores,
            )
            if lexical
            else None
        ),
        semantic=(
            SemanticProvenanceResponse(
                rank=semantic.rank,
                cosine_similarity=float(semantic.score or 0.0),
                embedding_id=embedded.id,
                embedding_run_id=embedded.embedding_run_id,
                logical_embedding_key=embedded.logical_embedding_key,
                input_hash=embedded.input_hash,
                contract_digest=embedded.contract_digest,
                window_count=embedded.window_count,
                execution_mode=embedded.execution_mode,
                reuse_source_embedding_id=embedded.reuse_source_embedding_id,
            )
            if semantic and embedded
            else None
        ),
        hybrid=(
            HybridProvenanceResponse(
                lexical_contribution=value.lexical_rrf_contribution,
                semantic_contribution=value.semantic_rrf_contribution,
            )
            if hybrid
            else None
        ),
        artifact_processing_id=value.chunk.artifact_processing_id,
        artifact_id=cast(UUID, value.chunk.artifact_id),
        locator=cast(str, value.chunk.locator),
        chunk_id=value.chunk.id,
        stable_key=value.chunk.stable_key,
        content_hash=value.chunk.content_hash,
        kind=value.chunk.kind,
        ordinal=value.chunk.ordinal,
        span=span(value.chunk.span),
        symbol_id=value.chunk.symbol_id,
        symbol_identity_id=value.chunk.symbol_identity_id,
        heading_path=list(value.chunk.heading_path),
        json_path=value.chunk.json_path,
        content=value.chunk.content,
    )


def response(value: RetrievalResult) -> RetrievalResponse:
    embedding_run = value.embedding_run
    return RetrievalResponse(
        requested_mode=value.requested_mode,
        executed_mode=value.executed_mode,
        policy=RetrievalPolicyResponse(
            version=RETRIEVAL_POLICY_VERSION,
            candidate_depth=CANDIDATE_DEPTH,
            rrf_constant=RRF_CONSTANT,
            exact_precedence=True,
        ),
        project_id=value.project_id,
        snapshot_id=value.snapshot_id,
        processing_run_id=value.processing_run.id,
        processing_revision=value.processing_run.revision,
        processing_coverage=value.processing_run.coverage,
        embedding_run=run(embedding_run) if embedding_run else None,
        embedding_contract=contract(embedding_run.contract) if embedding_run else None,
        components=RetrievalComponentsResponse(
            exact=component(value.exact),
            lexical=component(value.lexical),
            semantic=component(value.semantic),
        ),
        candidate_depth=CANDIDATE_DEPTH,
        candidates_truncated=(
            value.exact.truncated or value.lexical.truncated or value.semantic.truncated
        ),
        returned_count=value.returned_count,
        hits=[hit(item, value.executed_mode == "hybrid") for item in value.hits],
    )


@router.post("/search", response_model=RetrievalResponse, tags=["retrieval"])
def search(
    project_id: UUID,
    snapshot_id: UUID,
    request: RetrievalRequest,
    service: RetrievalService = Depends(get_retrieval_service),
) -> RetrievalResponse:
    selected = request.filters
    from buildlens.domain.retrieval import RetrievalFilters

    result = service.search(
        project_id,
        snapshot_id,
        request.mode,
        request.query,
        request.top_k,
        RetrievalFilters(
            locator_prefix=selected.locator_prefix if selected else None,
            language=selected.language if selected else None,
            kind=selected.kind if selected else None,
        ),
    )
    return response(result)
