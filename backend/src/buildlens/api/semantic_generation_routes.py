"""Controlled semantic generation HTTP contracts (Stage 13 Milestone C).

Plans are persisted preflights that send nothing. ``POST /generation`` executes one plan only
with ``{plan_id, plan_digest, confirm: true}``. Model text is returned as plain strings only (F7).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from buildlens.api.dependencies import get_semantic_generation_service
from buildlens.api.semantic_schemas import (
    AttemptResponse,
    CitationResponse,
    ClaimResponse,
    EntryHistoryResponse,
    EntryResponse,
    ExecuteRequest,
    GenerationRunHistoryResponse,
    GenerationRunResponse,
    InterpretationResponse,
    LocatorResponse,
    PlanItemResponse,
    PlanRequest,
    PlanResponse,
)
from buildlens.application.semantic_generation import PlanSelection, SemanticGenerationService
from buildlens.domain.semantic_generation import (
    Attempt,
    EntryView,
    GenerationPlan,
    GenerationRun,
    Interpretation,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/snapshots/{snapshot_id}/semantic",
    tags=["semantic-generation"],
)
runtime_router = APIRouter(prefix="/api/v1", tags=["semantic-generation"])


def plan(x: GenerationPlan) -> PlanResponse:
    return PlanResponse(
        plan_id=x.id,
        snapshot_id=x.snapshot_id,
        plan_nonce=x.plan_nonce,
        plan_digest=x.plan_digest,
        issued_at=x.issued_at,
        expires_at=x.expires_at,
        mode=x.mode,
        accept_profiles=x.accept_profiles,
        selection=x.selection,
        provider_config=x.provider_config,
        provider_config_digest=x.provider_config_digest,
        generator_profile_digest=x.generator_profile_digest,
        sharing_class=x.sharing_class,
        retention_note=x.retention_note,
        catalog_run_id=x.catalog_run_id,
        structural_analysis_id=x.structural_analysis_id,
        processing_run_id=x.processing_run_id,
        index_run_id=x.index_run_id,
        caps=x.caps,
        totals=x.totals,
        contributing_locators=x.contributing_locators,
        items=[
            PlanItemResponse(
                ordinal=item.ordinal,
                entity_key=item.entity_key,
                disposition=item.disposition,
                reason=item.reason,
                context_coverage=item.context_coverage,
                package_digest=item.package_digest,
                input_key=item.input_key,
                request_digest=item.request_digest,
                request_bytes=item.request_bytes,
                contributing_locators=[
                    LocatorResponse(
                        artifact_identity_id=value.artifact_identity_id,
                        locator=value.locator,
                        role=value.role,
                        bytes=value.bytes,
                    )
                    for value in item.contributing_locators
                ],
                reuse_interpretation_id=item.reuse_interpretation_id,
                reuse_profile_digest=item.reuse_profile_digest,
                reuse_provider=item.reuse_provider,
                reuse_returned_model=item.reuse_returned_model,
            )
            for item in x.items
        ],
        run_id=x.run_id,
    )


def attempt(x: Attempt) -> AttemptResponse:
    return AttemptResponse(
        attempt_id=x.id,
        entity_key=x.entity_key,
        attempt_no=x.attempt_no,
        request_digest=x.request_digest,
        started_at=x.started_at,
        outcome=x.outcome,
        returned_model=x.returned_model,
        input_tokens=x.input_tokens,
        output_tokens=x.output_tokens,
        latency_ms=x.latency_ms,
        rejection_codes=list(x.rejection_codes),
        late_receipts=x.late_receipts,
    )


def run(x: GenerationRun, attempts: list[Attempt] | None = None) -> GenerationRunResponse:
    return GenerationRunResponse(
        run_id=x.id,
        snapshot_id=x.snapshot_id,
        plan_id=x.plan_id,
        plan_digest=x.plan_digest,
        state=x.state,
        catalog_run_id=x.catalog_run_id,
        captured_index_run_id=x.captured_index_run_id,
        caps=x.caps,
        attempts_started=x.attempts_started,
        started_at=x.started_at,
        completed_at=x.completed_at,
        deadline_at=x.deadline_at,
        totals=x.totals,
        diagnostics=list(x.diagnostics),
        attempts=[attempt(item) for item in attempts or []],
    )


def interpretation(x: Interpretation) -> InterpretationResponse:
    errors: list[str] = []
    claims: list[ClaimResponse] = []
    for claim in x.claims:
        failed = [c.verification for c in claim.citations if c.verification != "verified"]
        errors += [f"claim{claim.ordinal}:{code}" for code in failed if code]
        claims.append(
            ClaimResponse(
                ordinal=claim.ordinal,
                category=claim.category,
                text=None if failed else claim.text,
                status=claim.status,
                basis=claim.basis,
                basis_label=claim.basis_label,
                assertion=claim.assertion,
                assertion_outcome=claim.assertion_outcome,
                scope_warning=claim.scope_warning,
                citations=[
                    CitationResponse(
                        evidence_id=c.evidence_id,
                        kind=c.kind,
                        basis=c.basis,
                        locator=c.locator,
                        start_line=c.start_line,
                        end_line=c.end_line,
                        stable_key=c.stable_key,
                        fact_digest=c.fact_digest,
                        start=c.start,
                        end=c.end,
                        verification=c.verification,
                        quote=c.quote if c.verification == "verified" else None,
                    )
                    for c in claim.citations
                ],
            )
        )
    return InterpretationResponse(
        interpretation_id=x.id,
        entity_key=x.entity_key,
        snapshot_id=x.snapshot_id,
        run_id=x.run_id,
        attempt_id=x.attempt_id,
        input_key=x.input_key,
        generator_profile_digest=x.generator_profile_digest,
        provider=x.provider,
        requested_model=x.requested_model,
        returned_model=x.returned_model,
        prompt_version=x.prompt_version,
        schema_version=x.schema_version,
        validator_version=x.validator_version,
        status=x.status,
        created_at=x.created_at,
        unknowns=list(x.unknowns),
        context_coverage=str(x.manifest.get("context_coverage"))
        if x.manifest.get("context_coverage")
        else None,
        claims=claims,
        integrity_errors=errors,
    )


def entry(key: str, x: EntryView) -> EntryResponse:
    return EntryResponse(
        entity_key=key,
        state=x.state,
        stale_reasons=list(x.stale_reasons),
        mode=x.entry.mode if x.entry else None,
        reused_from_profile=x.entry.reused_from_profile if x.entry else None,
        index_run_id=x.index_run_id,
        interpretation=interpretation(x.interpretation) if x.interpretation else None,
    )


@runtime_router.get("/semantic-generation")
def configuration(
    service: SemanticGenerationService = Depends(get_semantic_generation_service),
) -> dict[str, object]:
    """Configuration only; never contacts a provider."""
    return service.configuration()


@router.post("/generation/plan", response_model=PlanResponse)
def create_plan(
    project_id: UUID,
    snapshot_id: UUID,
    body: PlanRequest,
    service: SemanticGenerationService = Depends(get_semantic_generation_service),
) -> PlanResponse:
    selection = PlanSelection(
        tuple(body.entity_keys),
        body.locator_prefix,
        tuple(body.kinds),
        body.stale_only,
        body.mode,
        tuple(body.accept_profiles)
        if isinstance(body.accept_profiles, list)
        else body.accept_profiles,
    )
    return plan(service.plan(project_id, snapshot_id, selection))


@router.get("/generation/plans/{plan_id}", response_model=PlanResponse)
def get_plan(
    project_id: UUID,
    snapshot_id: UUID,
    plan_id: UUID,
    service: SemanticGenerationService = Depends(get_semantic_generation_service),
) -> PlanResponse:
    return plan(service.get_plan(project_id, snapshot_id, plan_id))


@router.post("/generation", response_model=GenerationRunResponse)
def execute(
    project_id: UUID,
    snapshot_id: UUID,
    body: ExecuteRequest,
    service: SemanticGenerationService = Depends(get_semantic_generation_service),
) -> GenerationRunResponse:
    value = service.execute(project_id, snapshot_id, body.plan_id, body.plan_digest, body.confirm)
    selected, attempts = service.run(project_id, snapshot_id, value.id)
    return run(selected, attempts)


@router.get("/generation/runs", response_model=GenerationRunHistoryResponse)
def runs(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    service: SemanticGenerationService = Depends(get_semantic_generation_service),
) -> GenerationRunHistoryResponse:
    values, total = service.runs(project_id, snapshot_id, limit, offset)
    return GenerationRunHistoryResponse(
        items=[run(item) for item in values], total=total, limit=limit, offset=offset
    )


@router.get("/generation/runs/{run_id}", response_model=GenerationRunResponse)
def get_run(
    project_id: UUID,
    snapshot_id: UUID,
    run_id: UUID,
    service: SemanticGenerationService = Depends(get_semantic_generation_service),
) -> GenerationRunResponse:
    selected, attempts = service.run(project_id, snapshot_id, run_id)
    return run(selected, attempts)


@router.get("/entities/{entity_key}/interpretation", response_model=EntryResponse)
def get_interpretation(
    project_id: UUID,
    snapshot_id: UUID,
    entity_key: str,
    service: SemanticGenerationService = Depends(get_semantic_generation_service),
) -> EntryResponse:
    return entry(entity_key, service.entry(project_id, snapshot_id, entity_key))


@router.get("/entities/{entity_key}/history", response_model=EntryHistoryResponse)
def get_history(
    project_id: UUID,
    snapshot_id: UUID,
    entity_key: str,
    service: SemanticGenerationService = Depends(get_semantic_generation_service),
) -> EntryHistoryResponse:
    return EntryHistoryResponse(
        entity_key=entity_key,
        items=[
            entry(entity_key, item) for item in service.history(project_id, snapshot_id, entity_key)
        ],
    )


@router.get("/interpretations/{interpretation_id}", response_model=InterpretationResponse)
def get_interpretation_by_id(
    project_id: UUID,
    snapshot_id: UUID,
    interpretation_id: UUID,
    service: SemanticGenerationService = Depends(get_semantic_generation_service),
) -> InterpretationResponse:
    return interpretation(service.interpretation(project_id, snapshot_id, interpretation_id))
