"""Persistent embedding preparation, history, metadata, and search HTTP contracts."""

from dataclasses import asdict
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from buildlens.api.dependencies import get_embedding_service, get_retrieval_service
from buildlens.api.embedding_schemas import (
    ChunkEmbeddingPageResponse,
    ChunkEmbeddingResponse,
    EmbeddingContractResponse,
    EmbeddingHistoryResponse,
    EmbeddingRunResponse,
    EmbeddingStatusResponse,
    ModelCacheResponse,
    PrepareEmbeddingsRequest,
    SemanticSearchHitResponse,
    SemanticSearchRequest,
    SemanticSearchResponse,
)
from buildlens.api.processing_routes import span
from buildlens.application.embeddings import EmbeddingService, EmbeddingStatus
from buildlens.application.retrieval import RetrievalService
from buildlens.domain.embeddings import (
    ChunkEmbedding,
    EmbeddingContract,
    EmbeddingRun,
)
from buildlens.domain.retrieval import RetrievalFilters

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/snapshots/{snapshot_id}", tags=["embeddings"]
)


def contract(value: EmbeddingContract) -> EmbeddingContractResponse:
    return EmbeddingContractResponse(digest=value.digest, **asdict(value))


def run(value: EmbeddingRun) -> EmbeddingRunResponse:
    return EmbeddingRunResponse(
        run_id=value.id,
        project_id=value.project_id,
        snapshot_id=value.snapshot_id,
        processing_run_id=value.processing_run_id,
        processing_revision=value.processing_revision,
        revision=value.revision,
        contract=contract(value.contract),
        membership_digest=value.membership_digest,
        coverage=value.coverage,
        limits=value.limits,
        diagnostics=list(value.diagnostics),
        chunk_count=value.chunk_count,
        started_at=value.started_at,
        completed_at=value.completed_at,
        outcome=value.outcome,
        superseded=value.superseded,
        quarantined=value.quarantined,
    )


def embedding(value: ChunkEmbedding) -> ChunkEmbeddingResponse:
    return ChunkEmbeddingResponse(
        id=value.id,
        embedding_run_id=value.embedding_run_id,
        project_id=value.project_id,
        snapshot_id=value.snapshot_id,
        processing_run_id=value.processing_run_id,
        artifact_processing_id=value.artifact_processing_id,
        chunk_id=value.chunk_id,
        stable_chunk_key=value.stable_chunk_key,
        input_hash=value.input_hash,
        logical_embedding_key=value.logical_embedding_key,
        contract_digest=value.contract_digest,
        dimensions=value.dimensions,
        window_count=value.window_count,
        execution_mode=value.execution_mode,
        reuse_source_embedding_id=value.reuse_source_embedding_id,
    )


def status(value: EmbeddingStatus) -> EmbeddingStatusResponse:
    return EmbeddingStatusResponse(
        model=ModelCacheResponse(
            available=value.model.available, path=value.model.path, reason=value.model.reason
        ),
        contract=contract(value.contract),
        prepared=value.prepared,
        search_available=value.search_available,
        active_processing_run_id=value.active_processing_run_id,
        freshness_reasons=list(value.freshness_reasons),
        latest_attempt=run(value.latest_attempt) if value.latest_attempt else None,
        active_run=run(value.active_run) if value.active_run else None,
    )


@router.get("/embeddings", response_model=EmbeddingStatusResponse)
def get_embeddings(
    project_id: UUID,
    snapshot_id: UUID,
    service: EmbeddingService = Depends(get_embedding_service),
) -> EmbeddingStatusResponse:
    return status(service.status(project_id, snapshot_id))


@router.post("/embeddings", response_model=EmbeddingStatusResponse)
def prepare_embeddings(
    project_id: UUID,
    snapshot_id: UUID,
    _request: PrepareEmbeddingsRequest,
    service: EmbeddingService = Depends(get_embedding_service),
) -> EmbeddingStatusResponse:
    service.prepare(project_id, snapshot_id)
    return status(service.status(project_id, snapshot_id))


@router.get("/embeddings/runs", response_model=EmbeddingHistoryResponse)
def history(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: EmbeddingService = Depends(get_embedding_service),
) -> EmbeddingHistoryResponse:
    values, total = service.history(project_id, snapshot_id, limit, offset)
    return EmbeddingHistoryResponse(
        items=[run(value) for value in values], total=total, limit=limit, offset=offset
    )


@router.get("/embeddings/runs/{run_id}", response_model=EmbeddingRunResponse)
def get_run(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    service: EmbeddingService = Depends(get_embedding_service),
) -> EmbeddingRunResponse:
    return run(service.run(project_id, snapshot_id, run_id))


@router.get("/embeddings/runs/{run_id}/rows", response_model=ChunkEmbeddingPageResponse)
def rows(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: EmbeddingService = Depends(get_embedding_service),
) -> ChunkEmbeddingPageResponse:
    values, total = service.rows(project_id, snapshot_id, run_id, limit, offset)
    return ChunkEmbeddingPageResponse(
        run_id=run_id,
        items=[embedding(value) for value in values],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/semantic-search", response_model=SemanticSearchResponse)
def semantic_search(
    project_id: UUID,
    snapshot_id: UUID,
    request: SemanticSearchRequest,
    service: RetrievalService = Depends(get_retrieval_service),
) -> SemanticSearchResponse:
    selected = request.filters
    filters = RetrievalFilters(
        locator_prefix=selected.locator_prefix if selected else None,
        language=selected.language if selected else None,
        kind=selected.kind if selected else None,
    )
    result = service.search(
        project_id, snapshot_id, "semantic", request.query, request.top_k, filters
    )
    selected_run = result.embedding_run
    if selected_run is None:
        raise RuntimeError("Semantic retrieval completed without embedding authority.")
    hits = [
        SemanticSearchHitResponse(
            score=cast(float, value.semantic.score),
            embedding_id=value.semantic.embedding.id,
            embedding_run_id=value.semantic.embedding.embedding_run_id,
            logical_embedding_key=value.semantic.embedding.logical_embedding_key,
            embedding_key=value.semantic.embedding.logical_embedding_key,
            input_hash=value.semantic.embedding.input_hash,
            contract_digest=value.semantic.embedding.contract_digest,
            window_count=value.semantic.embedding.window_count,
            execution_mode=value.semantic.embedding.execution_mode,
            reuse_source_embedding_id=value.semantic.embedding.reuse_source_embedding_id,
            project_id=selected_run.project_id,
            snapshot_id=selected_run.snapshot_id,
            processing_run_id=selected_run.processing_run_id,
            processing_revision=selected_run.processing_revision,
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
        for value in result.hits
        if value.semantic is not None and value.semantic.embedding is not None
    ]
    return SemanticSearchResponse(
        contract=contract(selected_run.contract), embedding_run=run(selected_run), hits=hits
    )
