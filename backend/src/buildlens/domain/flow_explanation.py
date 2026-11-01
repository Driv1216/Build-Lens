"""Stage 15 Milestone E: evidence-grounded explanations of a pinned ``FlowTrace`` (D3, INV-8).

An explanation is bound to one pinned trace (its ``trace_digest``), one ``FlowPackage`` (its
``package_digest``) and one confirmed plan (its ``plan_digest``). It never writes flow nodes or
edges and is never Ask evidence. Every claim is re-checked against the package by
``flow-validator.v1``; rejected claims are dropped and only their reason codes are kept. A
claim that passes is *consistent with the trace*: it is not a claim of runtime truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

FLOW_OUTBOUND_VERSION = "buildlens.flow.outbound.v1"
FLOW_PACKAGE_VERSION = "flow-package.v1"
FLOW_VALIDATOR_VERSION = "flow-validator.v1"
FLOW_EXPLANATION_SCHEMA = "buildlens_flow_explanation"

# Placeholder bounds (D7).
PACKAGE_STEPS = 160
PACKAGE_EXCERPTS = 8
EXCERPT_CHARS = 1_200
PLAN_TTL_SECONDS = 1_800

PlanState = Literal["planned", "used"]
RunState = Literal["running", "complete", "failed", "abandoned"]
OutcomeKind = Literal[
    "accepted",
    "accepted_with_rejections",
    "rejected",
    "insufficient_evidence",
    "provider_error",
    "incomplete",
    "refused",
]
ClaimLevel = Literal["summary", "resolution", "reachability", "path", "feasibility", "effect"]
CLAIM_LEVELS: tuple[str, ...] = (
    "summary",
    "resolution",
    "reachability",
    "path",
    "feasibility",
    "effect",
)


def explanation_contract() -> dict[str, Any]:
    return {
        "outbound": FLOW_OUTBOUND_VERSION,
        "package": FLOW_PACKAGE_VERSION,
        "validator": FLOW_VALIDATOR_VERSION,
        "schema": FLOW_EXPLANATION_SCHEMA,
        "bounds": {
            "package_steps": PACKAGE_STEPS,
            "package_excerpts": PACKAGE_EXCERPTS,
            "excerpt_chars": EXCERPT_CHARS,
            "plan_ttl_seconds": PLAN_TTL_SECONDS,
        },
        "evaluation": "real-model quality evaluation pre-registered and not run",
    }


@dataclass(frozen=True, slots=True)
class ExplanationPlan:
    id: UUID
    project_id: UUID
    project_version_id: UUID
    flow_run_id: UUID
    trace_digest: str
    package_digest: str
    request_digest: str
    request_bytes: int
    config_digest: str
    provider: str
    model: str
    retention_note: str
    plan_digest: str
    created_at: datetime
    expires_at: datetime
    state: PlanState


@dataclass(frozen=True, slots=True)
class ExplanationClaim:
    ordinal: int
    level: str
    text: str
    steps: tuple[str, ...]
    citations: tuple[dict[str, Any], ...]
    claim_digest: str


@dataclass(frozen=True, slots=True)
class ExplanationRun:
    id: UUID
    plan_id: UUID
    project_version_id: UUID
    trace_digest: str
    package_digest: str
    state: RunState
    started_at: datetime
    completed_at: datetime | None
    outcome: str | None = None
    returned_model: str | None = None
    rejection_codes: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    claims: tuple[ExplanationClaim, ...] = field(default_factory=tuple)
    requests_sent: int = 0
