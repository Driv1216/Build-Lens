"""Stage 14 owner-declared architecture rules (intent) and conformance results (C-4, F9)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from buildlens.api.architecture_routes import edge
from buildlens.api.architecture_schemas import EdgeResponse
from buildlens.api.dependencies import get_architecture_rules
from buildlens.application.architecture_rules import ArchitectureRulesService, RuleResults
from buildlens.domain.architecture_rules import RuleSet, RuleValidationError

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["architecture rules"])


class RuleSetRequest(BaseModel):
    """Owner-declared rules only; repository files never supply rules."""

    model_config = ConfigDict(extra="forbid")
    rules: list[dict[str, Any]] = Field(max_length=200)


class IntentRuleSetResponse(BaseModel):
    """Declared intent: a version, a digest and rules. It has no evidence tier."""

    rule_set_id: UUID
    project_id: UUID
    version: int
    contract: str
    digest: str
    created_at: datetime
    rules: list[dict[str, Any]]


class RuleSetHistoryResponse(BaseModel):
    items: list[IntentRuleSetResponse]


class ConformanceResultResponse(BaseModel):
    rule: dict[str, Any]
    outcome: Literal[
        "not_satisfied",
        "satisfied_in_observed_graph",
        "not_determinable",
        "selector_matches_nothing",
    ]
    witnesses: list[EdgeResponse]
    witness_total: int
    witness_min_tier: str | None
    classes_evaluated: list[str]
    coverage_qualifier: list[str]
    matched: dict[str, int]


class RuleResultsResponse(BaseModel):
    run_id: UUID
    freshness_reasons: list[str]
    verification: str = "verified"
    rule_set_version: int
    rule_set_digest: str
    result_key: str
    results: list[ConformanceResultResponse]


def intent(value: RuleSet) -> IntentRuleSetResponse:
    return IntentRuleSetResponse(
        rule_set_id=value.id,
        project_id=value.project_id,
        version=value.version,
        contract=value.contract,
        digest=value.digest,
        created_at=value.created_at,
        rules=[rule.as_json() for rule in value.rules],
    )


def results(value: RuleResults) -> RuleResultsResponse:
    return RuleResultsResponse(
        run_id=value.run.id,
        freshness_reasons=list(value.freshness_reasons),
        rule_set_version=value.rule_set.version,
        rule_set_digest=value.rule_set.digest,
        result_key=value.result_key,
        results=[
            ConformanceResultResponse(
                rule=item.rule.as_json(),
                outcome=item.outcome,
                witnesses=[edge(witness) for witness in item.witnesses],
                witness_total=item.witness_total,
                witness_min_tier=item.witness_min_tier,
                classes_evaluated=list(item.classes_evaluated),
                coverage_qualifier=list(item.coverage_qualifier),
                matched=item.matched,
            )
            for item in value.results
        ],
    )


@router.post("/architecture-rules", response_model=IntentRuleSetResponse, status_code=201)
def declare(
    project_id: UUID,
    body: RuleSetRequest,
    service: ArchitectureRulesService = Depends(get_architecture_rules),
) -> IntentRuleSetResponse:
    try:
        return intent(service.declare(project_id, body.rules))
    except RuleValidationError as error:
        raise HTTPException(
            422, {"code": "ARCHITECTURE_RULES_INVALID", "message": str(error)}
        ) from None


@router.get("/architecture-rules", response_model=IntentRuleSetResponse)
def latest(
    project_id: UUID,
    version: int | None = Query(None, ge=1),
    service: ArchitectureRulesService = Depends(get_architecture_rules),
) -> IntentRuleSetResponse:
    return intent(service.rule_set(project_id, version))


@router.get("/architecture-rules/versions", response_model=RuleSetHistoryResponse)
def versions(
    project_id: UUID, service: ArchitectureRulesService = Depends(get_architecture_rules)
) -> RuleSetHistoryResponse:
    return RuleSetHistoryResponse(items=[intent(item) for item in service.versions(project_id)])


@router.get(
    "/snapshots/{snapshot_id}/architecture/rule-results", response_model=RuleResultsResponse
)
def rule_results(
    project_id: UUID,
    snapshot_id: UUID,
    version: int | None = Query(None, ge=1),
    run_id: UUID | None = None,
    service: ArchitectureRulesService = Depends(get_architecture_rules),
) -> RuleResultsResponse:
    return results(service.evaluate(project_id, snapshot_id, run_id, version))
