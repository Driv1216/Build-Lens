"""Deterministic semantic catalog HTTP contracts (Stage 13 Milestone B; no AI text)."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from buildlens.api.dependencies import (
    get_semantic_catalog_service,
    get_semantic_generation_service,
)
from buildlens.api.semantic_schemas import (
    CatalogHistoryResponse,
    CatalogRunResponse,
    CatalogStatusResponse,
    EntityDetailResponse,
    EntityPageResponse,
    EntitySummaryResponse,
    EvidenceChunkResponse,
    FactResponse,
    OmittedRegionResponse,
    RelationResponse,
)
from buildlens.application.semantic_catalog import EntityFilters, SemanticCatalogService
from buildlens.application.semantic_generation import SemanticGenerationService
from buildlens.domain.semantic import (
    SemanticCatalogRun,
    SemanticEntity,
    SemanticFact,
    SemanticRelation,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/snapshots/{snapshot_id}/semantic", tags=["semantic"]
)


def run(x: SemanticCatalogRun) -> CatalogRunResponse:
    return CatalogRunResponse(
        run_id=x.id,
        snapshot_id=x.snapshot_id,
        revision=x.revision,
        started_at=x.started_at,
        completed_at=x.completed_at,
        outcome=x.outcome,
        structural_analysis_id=x.structural_analysis_id,
        structural_revision=x.structural_revision,
        processing_run_id=x.processing_run_id,
        processing_revision=x.processing_revision,
        input_authority_digest=x.input_authority_digest,
        catalog_digest=x.catalog_digest,
        portable_digest=x.portable_digest,
        catalog_contract=x.catalog_contract,
        coverage=x.coverage,
        diagnostics=list(x.diagnostics),
        superseded=x.superseded,
        quarantined=x.quarantined,
    )


def entity(x: SemanticEntity) -> EntitySummaryResponse:
    return EntitySummaryResponse(
        entity_key=x.entity_key,
        kind=x.kind,
        language=x.language,
        locator=x.locator,
        name=x.name,
        qualified_name=x.qualified_name,
        symbol_kind=x.symbol_kind,
        start_line=x.start_line,
        end_line=x.end_line,
        subject_entity_key=x.subject_entity_key,
        stored_coverage=x.stored_coverage,
        coverage_reason=x.coverage_reason,
        context_coverage=x.context_coverage,
        context_reason=x.context_reason,
        package_digest=x.package_digest,
        package_bytes=x.package_bytes,
        limitations=list(x.limitations),
    )


def fact(x: SemanticFact) -> FactResponse:
    basis: Literal["parsed", "pattern_with_local_evidence", "pattern_only"] = "parsed"
    if x.fact_kind == "recognizer_match":
        basis = (
            "pattern_with_local_evidence"
            if x.derived.get("strength") == "pattern_with_local_evidence"
            else "pattern_only"
        )
    return FactResponse(
        fact_digest=x.fact_digest,
        fact_kind=x.fact_kind,
        subject_entity_key=x.subject_entity_key,
        basis=basis,
        rendered_text=x.rendered_text,
        renderer_version=x.renderer_version,
        source_refs=[dict(ref) for ref in x.source_refs],
        derived=dict(x.derived),
        verification=x.verification,
    )


def relation(x: SemanticRelation) -> RelationResponse:
    return RelationResponse(
        kind=x.kind,
        source_entity_key=x.source_entity_key,
        target_entity_key=x.target_entity_key,
        target_text=x.target_text,
        resolution_status=x.resolution_status,
        fact_digest=x.fact_digest,
    )


def status(
    service: SemanticCatalogService, project_id: UUID, snapshot_id: UUID
) -> CatalogStatusResponse:
    value = service.status(project_id, snapshot_id)
    return CatalogStatusResponse(
        latest_attempt=run(value.latest_attempt) if value.latest_attempt else None,
        active_run=run(value.active_run) if value.active_run else None,
        freshness=value.freshness,
        freshness_reasons=list(value.freshness_reasons),
        contract=value.contract,
    )


@router.post("/catalog", response_model=CatalogStatusResponse)
def build(
    project_id: UUID,
    snapshot_id: UUID,
    service: SemanticCatalogService = Depends(get_semantic_catalog_service),
) -> CatalogStatusResponse:
    service.build(project_id, snapshot_id)
    return status(service, project_id, snapshot_id)


@router.get("/catalog", response_model=CatalogStatusResponse)
def get_status(
    project_id: UUID,
    snapshot_id: UUID,
    service: SemanticCatalogService = Depends(get_semantic_catalog_service),
) -> CatalogStatusResponse:
    return status(service, project_id, snapshot_id)


@router.get("/catalog/runs", response_model=CatalogHistoryResponse)
def history(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: SemanticCatalogService = Depends(get_semantic_catalog_service),
) -> CatalogHistoryResponse:
    values, total = service.history(project_id, snapshot_id, limit, offset)
    return CatalogHistoryResponse(
        items=[run(x) for x in values], total=total, limit=limit, offset=offset
    )


@router.get("/catalog/runs/{run_id}", response_model=CatalogRunResponse)
def get_run(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    service: SemanticCatalogService = Depends(get_semantic_catalog_service),
) -> CatalogRunResponse:
    return run(service.run(project_id, snapshot_id, run_id))


@router.get("/entities", response_model=EntityPageResponse)
def entities(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID | None = None,
    kind: Literal["file", "symbol", "component", "hook", "api_endpoint", "db_table"] | None = None,
    language: str | None = Query(None, max_length=32),
    locator_prefix: str | None = Query(None, max_length=1024),
    stored_coverage: Literal["complete", "incomplete", "unavailable", "coordinate_mismatch"]
    | None = None,
    context_coverage: Literal["full", "outline", "not_eligible"] | None = None,
    q: str | None = Query(None, max_length=200),
    state: Literal["current", "stale", "absent"] | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: SemanticCatalogService = Depends(get_semantic_catalog_service),
    generation: SemanticGenerationService = Depends(get_semantic_generation_service),
) -> EntityPageResponse:
    keys: tuple[str, ...] | None = None
    if state is not None and run_id is None:
        _, views = generation.entry_views(project_id, snapshot_id)
        keys = tuple(key for key, view in views.items() if view.state == state)
    filters = EntityFilters(
        kind, language, locator_prefix, stored_coverage, context_coverage, q, keys
    )
    selected, reasons, values, total = service.entity_page(
        project_id, snapshot_id, run_id, filters, limit, offset
    )
    states: dict[str, str] = {}
    if run_id is None and values:
        _, page_views = generation.entry_views(
            project_id, snapshot_id, [x.entity_key for x in values]
        )
        states = {key: view.state for key, view in page_views.items()}
    return EntityPageResponse(
        run_id=selected.id,
        freshness_reasons=list(reasons),
        total=total,
        limit=limit,
        offset=offset,
        items=[
            entity(x).model_copy(update={"interpretation_state": states.get(x.entity_key)})
            for x in values
        ],
    )


@router.get("/entities/{entity_key}", response_model=EntityDetailResponse)
def get_entity(
    project_id: UUID,
    snapshot_id: UUID,
    entity_key: str,
    run_id: UUID | None = None,
    service: SemanticCatalogService = Depends(get_semantic_catalog_service),
) -> EntityDetailResponse:
    value = service.entity(project_id, snapshot_id, entity_key, run_id)
    item = value.entity
    return EntityDetailResponse(
        run_id=value.run.id,
        freshness_reasons=list(value.freshness_reasons),
        entity=entity(item),
        gaps=dict(item.gaps),
        package_chunks=list(item.package_chunks),
        omitted=[
            OmittedRegionResponse(
                entity_key=x.entity_key, start_line=x.start_line, end_line=x.end_line
            )
            for x in item.omitted
        ],
        evidence=[
            EvidenceChunkResponse(
                chunk_id=x.chunk_id,
                stable_key=x.stable_key,
                content_hash=x.content_hash,
                start_offset=x.start_offset,
                end_offset=x.end_offset,
                start_line=x.start_line,
                end_line=x.end_line,
                symbol_identity_id=x.symbol_identity_id,
                content=x.content,
                verified=x.verified,
            )
            for x in item.evidence
        ],
        facts=[fact(x) for x in value.facts],
        neighbor_facts=[fact(x) for x in value.neighbor_facts],
        outgoing=[relation(x) for x in value.outgoing],
        incoming=[relation(x) for x in value.incoming],
        integrity_errors=list(value.integrity_errors),
        related=dict(value.related),
    )
