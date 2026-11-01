"""Stage 15 Milestone B: the flow model (``flow-model.v1``) — vocabulary, records, integrity.

A flow model run is a pure projection of PostgreSQL rows: one flow evidence run, the semantic
catalog run and architecture run built over the *same* structural analysis, plus the
structural import/export rows. It never reads source and contains no AI text.

Every element carries evidence digests and an element digest over all of its semantic fields
(``flow-integrity.v1``). Edges carry a tier (``observed``, ``derived``, ``inferred``,
``unknown``), the versioned rule that produced them and named conditions — never a number.
No edge is ever produced by string or name similarity across files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from buildlens.domain.flow_evidence import flow_evidence_contract
from buildlens.domain.source import digest

FLOW_MODEL_VERSION = "flow-model.v2"  # v2: entry points and scope identities (Milestone C)
FLOW_INTEGRITY_VERSION = "flow-integrity.v1"
RULES = {
    "py_resolution": "py-call-resolution.v1",
    "ts_resolution": "ts-call-resolution.v1",
    "py_construction": "py-construction.v1",
    "ts_construction": "ts-construction.v1",
    "py_async": "py-async-semantics.v2",
    "js_async": "js-async-semantics.v2",
    "fastapi": "fastapi-semantics.v1",
    "fastapi_responses": "fastapi-responses.v1",
    "react": "react-semantics.v2",
    "tanstack": "tanstack-query-semantics.v1",
    "callbacks": "external-callback-contracts.v1",
    "sqlalchemy": "sqlalchemy-ops.v2",
    "ts_aliases": "ts-path-aliases.v1",
    "ts_workspaces": "ts-workspace-packages.v1",
    "descriptors": "flow-request-descriptors.v1",
    "stage14_stitching": "stage14-span-stitching.v1",
    "entry_points": "flow-entry-points.v1",
}
# The Stage 14 API-matching evaluation has no human labels yet (D1): every HTTP hop says so.
API_MATCHING_EVALUATION = "not_human_evaluated"
HTTP_CAVEAT = "HTTP_LINK_INDEPENDENT_EVALUATION_PENDING"

# Placeholder limits (D7), recorded in the contract; the build fails closed above them.
MAX_NODES = 500_000
MAX_EDGES = 1_500_000
FANOUT = 8

Tier = Literal["observed", "derived", "inferred", "unknown"]
TIERS: tuple[str, ...] = ("observed", "derived", "inferred", "unknown")

NodeKind = Literal[
    "function",
    "scope",
    "callsite",
    "http_request",
    "api_endpoint",
    "response_outcome",
    "db_operation",
    "db_table",
    "transaction_boundary",
    "state_update",
    "navigation",
    "storage_effect",
    "integration_boundary",
    "scheduler",
]
# Four edge families, never collapsed into one another (plan §2.5).
FAMILIES: dict[str, str] = {
    # resolution
    "resolves_to": "resolution",
    "dispatch_candidate": "resolution",
    "handled_by": "resolution",
    "calls_endpoint": "resolution",
    "issues_request": "resolution",
    "unresolved": "resolution",
    # invocation semantics
    "contains_callsite": "invocation",
    "invokes": "invocation",
    "creates_coroutine": "invocation",
    "creates_generator": "invocation",
    "creates_async_generator": "invocation",
    "invokes_until_first_await": "invocation",
    "awaits": "invocation",
    "iterates": "invocation",
    "resumes": "invocation",
    "throws_into": "invocation",
    "registers": "invocation",
    "scheduled_by": "invocation",
    "injects": "invocation",
    "invokes_callback": "invocation",
    "constructs": "invocation",
    "binds_prop_callback": "invocation",
    "continues_with": "invocation",
    "enters": "invocation",
    # effects
    "performs": "effect",
    "on_table": "effect",
    "in_transaction": "effect",
    "schedules_render": "effect",
    "may_rerun_effect": "effect",
    "navigates": "effect",
    "may_refetch": "effect",
    "crosses_boundary": "effect",
    # response
    "exits_with": "response",
    "responds": "response",
    "response_boundary": "response",
    "raises_to": "response",
}
RELATIONS: tuple[str, ...] = tuple(FAMILIES)


def flow_model_contract() -> dict[str, Any]:
    return {
        "flow_model": FLOW_MODEL_VERSION,
        "integrity": FLOW_INTEGRITY_VERSION,
        "evidence": digest(flow_evidence_contract()),
        "rules": dict(RULES),
        "api_matching_evaluation": API_MATCHING_EVALUATION,
        "limits": {"max_nodes": MAX_NODES, "max_edges": MAX_EDGES, "fanout": FANOUT},
    }


# ----------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class FlowEvidenceItem:
    """Cited rows (with ids for re-verification) or a derivation record.

    ``refs`` entries: ``{"table", "ids", "row_digest", "locator"}`` and, where the row digest
    itself covers database ids (Stage 14 elements), ``"portable"``. The portable projection
    drops ``ids`` and uses ``portable`` in place of ``row_digest`` when present, so two
    databases holding the same bytes agree.
    """

    kind: str
    refs: tuple[dict[str, Any], ...]
    derived: dict[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return str(digest({"kind": self.kind, "refs": list(self.refs), "derived": self.derived}))

    @property
    def portable_digest(self) -> str:
        refs = [
            {
                k: v
                for k, v in ref.items()
                if k != "ids" and not (k == "row_digest" and "portable" in ref)
            }
            for ref in self.refs
        ]
        return str(digest({"kind": self.kind, "refs": refs, "derived": self.derived}))


@dataclass(frozen=True, slots=True)
class FlowNode:
    key: str
    kind: str
    label: str
    locator: str | None
    attributes: dict[str, Any]
    evidence: tuple[str, ...] = ()

    def fields(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "label": self.label,
            "locator": self.locator,
            "attributes": self.attributes,
        }

    def element_digest(self, evidence: tuple[str, ...] | None = None) -> str:
        return str(digest({**self.fields(), "evidence": sorted(evidence or self.evidence)}))

    def portable_digest(self, evidence: tuple[str, ...]) -> str:
        """Without database-assigned attributes (the stable symbol identity)."""
        fields = self.fields()
        fields["attributes"] = {
            k: v for k, v in self.attributes.items() if k not in NON_PORTABLE_ATTRIBUTES
        }
        return str(digest({**fields, "evidence": sorted(evidence)}))


# Node attributes that carry database-assigned ids (excluded from the portable digest).
NON_PORTABLE_ATTRIBUTES = frozenset({"identity"})


@dataclass(frozen=True, slots=True)
class FlowEdge:
    key: str
    relation: str
    source_key: str
    target_key: str | None
    tier: Tier
    rule: str
    conditions: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()

    @property
    def family(self) -> str:
        return FAMILIES[self.relation]

    def fields(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "relation": self.relation,
            "family": self.family,
            "source": self.source_key,
            "target": self.target_key,
            "tier": self.tier,
            "rule": self.rule,
            "conditions": sorted(self.conditions),
            "attributes": self.attributes,
        }

    def element_digest(self, evidence: tuple[str, ...] | None = None) -> str:
        return str(digest({**self.fields(), "evidence": sorted(evidence or self.evidence)}))


@dataclass(frozen=True, slots=True)
class FlowDiagnostic:
    code: str
    subject: str | None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return str(
            digest({"code": self.code, "subject": self.subject, "attributes": self.attributes})
        )


@dataclass(frozen=True, slots=True)
class FlowBuild:
    nodes: tuple[FlowNode, ...]
    edges: tuple[FlowEdge, ...]
    evidence: tuple[FlowEvidenceItem, ...]
    diagnostics: tuple[FlowDiagnostic, ...]
    coverage: dict[str, Any]
    graph_digest: str
    portable_digest: str
    outcome: Literal["complete", "partial"]


@dataclass(frozen=True, slots=True)
class FlowRun:
    id: UUID
    project_id: UUID
    project_version_id: UUID
    revision: int
    started_at: datetime
    completed_at: datetime
    outcome: Literal["complete", "partial", "failed"]
    flow_evidence_run_id: UUID
    evidence_digest: str
    catalog_run_id: UUID
    catalog_digest: str
    architecture_run_id: UUID
    architecture_graph_digest: str
    structural_analysis_id: UUID
    contract: dict[str, Any]
    input_authority_digest: str
    graph_digest: str | None
    portable_digest: str | None
    coverage: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    superseded: bool = False
    captured_active_run_id: UUID | None = None
    captured_latest_revision: int = 0
    failure_code: str | None = None


def graph_digest_of(
    nodes: tuple[FlowNode, ...],
    edges: tuple[FlowEdge, ...],
    diagnostics: tuple[FlowDiagnostic, ...],
    evidence: dict[str, FlowEvidenceItem],
    *,
    portable: bool = False,
) -> str:
    """The run's graph digest; ``portable`` replaces evidence digests by id-free ones."""

    def ev(digests: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(evidence[d].portable_digest for d in digests)) if portable else digests

    return str(
        digest(
            {
                "contract": FLOW_MODEL_VERSION,
                "nodes": sorted(
                    [n.key, n.portable_digest(ev(n.evidence)) if portable else n.element_digest()]
                    for n in nodes
                ),
                "edges": sorted([e.key, e.element_digest(ev(e.evidence))] for e in edges),
                "diagnostics": sorted(d.digest for d in diagnostics),
            }
        )
    )


def tier_rank(tier: str) -> int:
    return TIERS.index(tier)


def weakest(tiers: list[str]) -> str:
    """Path tier = weakest step tier (observed > derived > inferred > unknown)."""
    return max(tiers, key=tier_rank) if tiers else "observed"
