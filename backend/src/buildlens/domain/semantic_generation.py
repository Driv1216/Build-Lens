"""Stage 13 controlled semantic generation (Milestone C): contracts and immutable records.

AI output is never a fact. An interpretation is a set of cited claims plus unknowns, each claim
validated deterministically against the exact package that was authorised and sent. Claims are
``cited`` (passed every check; not a statement of truth) or ``conflicts_with_fact``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, get_args
from uuid import UUID

from buildlens.domain.source import digest

PROMPT_VERSION = "semantic-prompt.v1"
SCHEMA_VERSION = "semantic-claims.v1"
VALIDATOR_VERSION = "semantic-validator.v1"
OUTBOUND_VERSION = "buildlens.semantic.outbound.v1"
# Conservative byte-admission allowance: tokens <= UTF-8 bytes + this (not a tokenizer guarantee).
TEMPLATE_TOKEN_ALLOWANCE = 512

ClaimCategory = Literal[
    "purpose", "responsibility", "behavior", "side_effect", "dependency", "usage_note"
]
AssertionKind = Literal[
    "is_async",
    "calls",
    "raises",
    "awaits",
    "returns_value",
    "inherits",
    "defines_route",
    "maps_table",
    "renders",
    "uses_hook",
]
AssertionOutcome = Literal["matches_fact", "conflicts_with_fact", "not_checkable"]
ClaimStatus = Literal["cited", "conflicts_with_fact"]
ClaimBasis = Literal["code", "documentation", "facts"]
InterpretationStatus = Literal[
    "accepted", "accepted_with_conflicts", "rejected", "insufficient_evidence"
]
Disposition = Literal["generate", "reuse", "ineligible"]
PlanMode = Literal["generate", "reuse_only"]
RunState = Literal["executing", "complete", "partial", "failed", "superseded", "abandoned"]
AttemptOutcomeCode = Literal[
    "accepted",
    "accepted_with_conflicts",
    "insufficient_evidence",
    "rejected",
    "rate_limited",
    "provider_unavailable",
    "timeout",
    "context_rejected",
    "refused",
    "token_bound_violated",
    "reused_concurrently",
    "outcome_unknown",
    "not_sent",
    "configuration_invalid",
]
EntryMode = Literal["generated", "reused", "retained"]
EntryState = Literal["current", "stale", "absent"]
SharingClass = Literal["cloud", "loopback"]

CLAIM_CATEGORIES: tuple[str, ...] = get_args(ClaimCategory)
ASSERTION_KINDS: tuple[str, ...] = get_args(AssertionKind)
RUN_STATES: tuple[str, ...] = get_args(RunState)
OUTCOME_CODES: tuple[str, ...] = get_args(AttemptOutcomeCode)
CANDIDATE_OUTCOMES = frozenset({"accepted", "accepted_with_conflicts", "insufficient_evidence"})


def input_key(content_digest: str) -> str:
    """The source-freshness key: independent of provider, model and snapshot row ids.

    It is derived from the entity's package *content* projection, so an unchanged entity in a
    new snapshot has the same key (§4.2, §5.3)."""
    return digest(
        {
            "domain": "buildlens.semantic.input.v1",
            "package": content_digest,
            "prompt": PROMPT_VERSION,
            "schema": SCHEMA_VERSION,
            "validator": VALIDATOR_VERSION,
        }
    )


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Everything about the outbound path that a plan authorises (§11.1)."""

    provider: Literal["local", "gemini", "openai"]
    model: str
    policy: dict[str, Any]
    adapter_version: str
    sdk_versions: dict[str, str | None]
    base_url: str | None = None
    retry_policy: str = "none"

    @property
    def sharing_class(self) -> SharingClass:
        return "loopback" if self.provider == "local" else "cloud"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "policy": dict(self.policy),
            "adapter_version": self.adapter_version,
            "sdk_versions": dict(self.sdk_versions),
            "base_url": self.base_url,
            "retry_policy": self.retry_policy,
        }

    @property
    def digest(self) -> str:
        return digest(self.as_dict())

    @property
    def profile_digest(self) -> str:
        """Generator profile: provider, requested model and generation policy only (§4.2)."""
        return digest({"provider": self.provider, "model": self.model, "policy": self.policy})


@dataclass(frozen=True, slots=True)
class LocatorContribution:
    artifact_identity_id: str | None
    locator: str
    role: Literal["own_source", "neighbor_facts"]
    bytes: int


@dataclass(frozen=True, slots=True)
class PlanItem:
    ordinal: int
    entity_key: str
    disposition: Disposition
    reason: str | None
    context_coverage: str
    package_digest: str | None
    input_key: str | None
    request_digest: str | None
    request_bytes: int
    contributing_locators: tuple[LocatorContribution, ...] = ()
    reuse_interpretation_id: UUID | None = None
    reuse_profile_digest: str | None = None
    reuse_provider: str | None = None
    reuse_returned_model: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationPlan:
    id: UUID
    project_id: UUID
    snapshot_id: UUID
    plan_nonce: str
    issued_at: datetime
    expires_at: datetime
    selection: dict[str, Any]
    mode: PlanMode
    accept_profiles: list[str] | Literal["any"] | None
    provider_config: dict[str, Any]
    provider_config_digest: str
    generator_profile_digest: str
    sharing_class: SharingClass
    catalog_run_id: UUID
    structural_analysis_id: UUID
    processing_run_id: UUID
    index_run_id: UUID | None
    caps: dict[str, Any]
    totals: dict[str, Any]
    contributing_locators: list[dict[str, Any]]
    plan_digest: str
    items: tuple[PlanItem, ...] = ()
    run_id: UUID | None = None
    retention_note: str = ""


@dataclass(frozen=True, slots=True)
class GenerationRun:
    id: UUID
    project_id: UUID
    snapshot_id: UUID
    plan_id: UUID
    plan_digest: str
    state: RunState
    catalog_run_id: UUID
    structural_analysis_id: UUID
    processing_run_id: UUID
    captured_index_run_id: UUID | None
    caps: dict[str, Any]
    attempts_started: int
    lease_token: UUID
    lease_expires_at: datetime
    deadline_at: datetime
    started_at: datetime
    completed_at: datetime | None
    totals: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Attempt:
    id: UUID
    run_id: UUID
    entity_key: str
    attempt_no: int
    package_digest: str
    request_digest: str
    input_key: str
    started_at: datetime
    outcome: str | None = None
    returned_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    rejection_codes: tuple[str, ...] = ()
    late_receipts: int = 0


@dataclass(frozen=True, slots=True)
class Citation:
    """Coordinates and hashes of one quoted evidence span; quote text is not stored."""

    evidence_id: str
    kind: Literal["chunk", "fact"]
    quote_sha256: str
    start: int
    end: int
    basis: ClaimBasis | Literal["string", "unclassified"]
    stable_key: str | None = None
    content_hash: str | None = None
    fact_digest: str | None = None
    locator: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    # Read-time resolution: ``verified`` or an integrity code; ``None`` until checked.
    verification: str | None = None
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    ordinal: int
    category: ClaimCategory
    text: str
    status: ClaimStatus
    basis: ClaimBasis
    basis_label: Literal["cited", "per_documentation", "from_declarations_only"]
    assertion: dict[str, Any] | None
    assertion_outcome: AssertionOutcome | None
    scope_warning: bool
    citations: tuple[Citation, ...]


@dataclass(frozen=True, slots=True)
class Interpretation:
    id: UUID
    project_id: UUID
    snapshot_id: UUID
    run_id: UUID
    attempt_id: UUID
    entity_key: str
    input_key: str
    manifest: dict[str, Any]
    generator_profile_digest: str
    provider: str
    requested_model: str
    returned_model: str | None
    prompt_version: str
    schema_version: str
    validator_version: str
    status: InterpretationStatus
    unknowns: tuple[str, ...]
    created_at: datetime
    claims: tuple[Claim, ...] = ()


@dataclass(frozen=True, slots=True)
class IndexEntry:
    entity_key: str
    interpretation_id: UUID
    mode: EntryMode
    reused_from_profile: str | None


@dataclass(frozen=True, slots=True)
class EntryView:
    """An index entry with its read-time state (§5.3) and immutable provenance (F9)."""

    entry: IndexEntry | None
    state: EntryState
    stale_reasons: tuple[str, ...]
    interpretation: Interpretation | None
    index_run_id: UUID | None
