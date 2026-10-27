"""Structural grounding metrics and hard gates for semantic generation (§8.4, §8.5).

These measure grounding and authorization, never quality: a claim that passes every check is
``cited``, not true. Quality is measured only by pre-registered human review.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Protocol
from uuid import UUID

from buildlens.application.semantic_validation import static_claim_problem
from buildlens.domain.semantic_generation import EntryView, Interpretation

METRICS_VERSION = "semantic-grounding-metrics.v1"


class MetricsSource(Protocol):
    def entry_views(
        self, project_id: UUID, snapshot_id: UUID, keys: Any = None
    ) -> tuple[Any, dict[str, EntryView]]: ...
    def resolve(self, project_id: UUID, interpretation: Interpretation) -> Interpretation: ...
    def outbound_audit(self, snapshot_id: UUID) -> dict[str, Any]: ...
    def eligible_count(self, catalog_run_id: UUID) -> int: ...


def grounding_metrics(source: MetricsSource, project_id: UUID, snapshot_id: UUID) -> dict[str, Any]:
    catalog, views = source.entry_views(project_id, snapshot_id)
    states = Counter(view.state for view in views.values())
    statuses: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    bases: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    assertions: Counter[str] = Counter()
    claim_status: Counter[str] = Counter()
    failing: list[dict[str, Any]] = []
    citations = scope = claims = interpretations = 0
    for key, view in sorted(views.items()):
        if view.interpretation is None:
            continue
        interpretation = source.resolve(project_id, view.interpretation)
        interpretations += 1
        statuses[interpretation.status] += 1
        for claim in interpretation.claims:
            claims += 1
            categories[claim.category] += 1
            bases[claim.basis] += 1
            labels[claim.basis_label] += 1
            claim_status[claim.status] += 1
            assertions[claim.assertion_outcome or "none"] += 1
            scope += claim.scope_warning
            citations += len(claim.citations)
            problems = [c.verification for c in claim.citations if c.verification != "verified"]
            static = static_claim_problem(claim.text)
            if problems or static:
                failing.append(
                    {"entity_key": key, "claim": claim.ordinal, "codes": [*problems, static][:4]}
                )
    audit = source.outbound_audit(snapshot_id)
    outside = (
        audit["attempts_outside_plans"]
        + audit["runs_over_request_cap"]
        + audit["entities_over_attempt_cap"]
    )
    return {
        "version": METRICS_VERSION,
        "snapshot_id": str(snapshot_id),
        "catalog_run_id": str(catalog.id),
        "coverage": {
            "eligible_entities": source.eligible_count(catalog.id),
            "entries": dict(sorted(states.items())),
        },
        "interpretations": {"total": interpretations, "status": dict(sorted(statuses.items()))},
        "claims": {
            "total": claims,
            "per_interpretation": round(claims / interpretations, 3) if interpretations else 0,
            "citations_per_claim": round(citations / claims, 3) if claims else 0,
            "category": dict(sorted(categories.items())),
            "basis": dict(sorted(bases.items())),
            "label": dict(sorted(labels.items())),
            "status": dict(sorted(claim_status.items())),
            "assertion_outcome": dict(sorted(assertions.items())),
            "scope_warnings": scope,
        },
        "outbound": audit,
        "hard_gates": {
            "H1_published_claims_failing_revalidation": len(failing),
            "H2_outbound_payloads_outside_plans": outside,
            "passed": not failing and outside == 0,
            "failing_claims": failing[:50],
        },
        "note": "Grounding and authorization only; quality requires pre-registered human review.",
    }
