"""Stage 15 Milestone E persistence: explanation plans, runs, attempts, outcomes and claims.

Writes only ``flow_explanation_*`` tables (INV-3). Nothing here writes flow nodes or edges,
and no other stage reads these tables (INV-8: explanations are never Ask evidence).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from buildlens.domain.flow_explanation import ExplanationClaim, ExplanationPlan, ExplanationRun
from buildlens.infrastructure.persistence.models import (
    FlowExplanationAttemptRecord,
    FlowExplanationClaimRecord,
    FlowExplanationItemRecord,
    FlowExplanationOutcomeRecord,
    FlowExplanationPlanRecord,
    FlowExplanationRunRecord,
)


class ExplanationPlanError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _plan(r: FlowExplanationPlanRecord) -> ExplanationPlan:
    return ExplanationPlan(
        r.id,
        r.project_id,
        r.project_version_id,
        r.flow_run_id,
        r.trace_digest,
        r.package_digest,
        r.request_digest,
        r.request_bytes,
        r.config_digest,
        r.provider,
        r.model,
        r.retention_note,
        r.plan_digest,
        r.created_at,
        r.expires_at,
        r.state,
    )


class SqlAlchemyFlowExplanationStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def insert_plan(
        self, plan: ExplanationPlan, package: dict[str, Any], contract: dict[str, Any]
    ) -> None:
        with self._session_factory.begin() as session:
            session.add(
                FlowExplanationPlanRecord(
                    id=plan.id,
                    project_id=plan.project_id,
                    project_version_id=plan.project_version_id,
                    flow_run_id=plan.flow_run_id,
                    trace_digest=plan.trace_digest,
                    package_digest=plan.package_digest,
                    request_digest=plan.request_digest,
                    request_bytes=plan.request_bytes,
                    config_digest=plan.config_digest,
                    provider=plan.provider,
                    model=plan.model,
                    retention_note=plan.retention_note,
                    plan_digest=plan.plan_digest,
                    contract=contract,
                    created_at=plan.created_at,
                    expires_at=plan.expires_at,
                    state="planned",
                )
            )
            session.flush()
            session.add(
                FlowExplanationItemRecord(
                    plan_id=plan.id,
                    ordinal=0,
                    trace_digest=plan.trace_digest,
                    package=package,
                    package_digest=plan.package_digest,
                )
            )

    def plan(
        self, project_id: UUID, snapshot_id: UUID, plan_id: UUID
    ) -> tuple[ExplanationPlan, dict[str, Any]] | None:
        with self._session_factory() as session:
            r = session.get(FlowExplanationPlanRecord, plan_id)
            if r is None or r.project_id != project_id or r.project_version_id != snapshot_id:
                return None
            item = session.get(FlowExplanationItemRecord, (plan_id, 0))
            assert item is not None
            return _plan(r), dict(item.package)

    def admit(self, plan_id: UUID, plan_digest: str, lease_seconds: float) -> ExplanationRun:
        """Use the plan exactly once (row lock + unique run per plan)."""
        with self._session_factory.begin() as session:
            r = session.scalar(
                select(FlowExplanationPlanRecord)
                .where(FlowExplanationPlanRecord.id == plan_id)
                .with_for_update()
            )
            if r is None:
                raise ExplanationPlanError("PLAN_NOT_FOUND")
            if r.plan_digest != plan_digest:
                raise ExplanationPlanError("PLAN_DIGEST_MISMATCH")
            if r.state != "planned":
                raise ExplanationPlanError("PLAN_ALREADY_USED")
            now = session.scalar(select(func.now()))
            assert now is not None
            if r.expires_at <= now:
                raise ExplanationPlanError("PLAN_EXPIRED")
            r.state = "used"
            run = FlowExplanationRunRecord(
                id=uuid4(),
                plan_id=plan_id,
                project_version_id=r.project_version_id,
                lease_token=uuid4(),
                state="running",
                started_at=now,
                deadline_at=now + timedelta(seconds=lease_seconds),
            )
            session.add(run)
            return ExplanationRun(
                run.id,
                plan_id,
                r.project_version_id,
                r.trace_digest,
                r.package_digest,
                "running",
                now,
                None,
            )

    def lease_token(self, run_id: UUID) -> UUID:
        with self._session_factory() as session:
            r = session.get(FlowExplanationRunRecord, run_id)
            assert r is not None
            return UUID(str(r.lease_token))

    def begin_attempt(self, run_id: UUID, request_digest: str) -> UUID:
        with self._session_factory.begin() as session:
            ordinal = session.scalar(
                select(func.count()).where(FlowExplanationAttemptRecord.run_id == run_id)
            )
            attempt = FlowExplanationAttemptRecord(
                id=uuid4(),
                run_id=run_id,
                ordinal=int(ordinal or 0),
                request_digest=request_digest,
                started_at=datetime.now(UTC),
            )
            session.add(attempt)
            return UUID(str(attempt.id))

    def record(
        self,
        run_id: UUID,
        attempt_id: UUID,
        outcome: dict[str, Any],
        claims: tuple[ExplanationClaim, ...],
        state: str,
    ) -> None:
        with self._session_factory.begin() as session:
            session.add(FlowExplanationOutcomeRecord(attempt_id=attempt_id, **outcome))
            session.flush()
            for claim in claims:
                session.add(
                    FlowExplanationClaimRecord(
                        attempt_id=attempt_id,
                        ordinal=claim.ordinal,
                        level=claim.level,
                        text=claim.text,
                        steps=list(claim.steps),
                        citations=list(claim.citations),
                        claim_digest=claim.claim_digest,
                    )
                )
            run = session.get(FlowExplanationRunRecord, run_id)
            assert run is not None
            run.state = state
            run.completed_at = datetime.now(UTC)

    def finish(self, run_id: UUID, state: str) -> None:
        with self._session_factory.begin() as session:
            run = session.get(FlowExplanationRunRecord, run_id)
            assert run is not None
            run.state = state
            run.completed_at = datetime.now(UTC)

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> ExplanationRun | None:
        with self._session_factory() as session:
            r = session.get(FlowExplanationRunRecord, run_id)
            if r is None or r.project_version_id != snapshot_id:
                return None
            plan = session.get(FlowExplanationPlanRecord, r.plan_id)
            if plan is None or plan.project_id != project_id:
                return None
            return self._run(session, r, plan)

    def runs(self, project_id: UUID, snapshot_id: UUID, trace_digest: str) -> list[ExplanationRun]:
        with self._session_factory() as session:
            rows = session.execute(
                select(FlowExplanationRunRecord, FlowExplanationPlanRecord)
                .join(
                    FlowExplanationPlanRecord,
                    FlowExplanationPlanRecord.id == FlowExplanationRunRecord.plan_id,
                )
                .where(
                    FlowExplanationPlanRecord.project_id == project_id,
                    FlowExplanationPlanRecord.project_version_id == snapshot_id,
                    FlowExplanationPlanRecord.trace_digest == trace_digest,
                )
                .order_by(FlowExplanationRunRecord.started_at.desc())
            ).all()
            return [self._run(session, r, p) for r, p in rows]

    def _run(
        self, session: Session, r: FlowExplanationRunRecord, plan: FlowExplanationPlanRecord
    ) -> ExplanationRun:
        attempts = session.scalars(
            select(FlowExplanationAttemptRecord)
            .where(FlowExplanationAttemptRecord.run_id == r.id)
            .order_by(FlowExplanationAttemptRecord.ordinal)
        ).all()
        outcome = session.get(FlowExplanationOutcomeRecord, attempts[-1].id) if attempts else None
        claims = (
            session.scalars(
                select(FlowExplanationClaimRecord)
                .where(FlowExplanationClaimRecord.attempt_id == attempts[-1].id)
                .order_by(FlowExplanationClaimRecord.ordinal)
            ).all()
            if attempts
            else []
        )
        state = r.state
        if state == "running" and r.deadline_at <= datetime.now(UTC):
            state = "abandoned"
        return ExplanationRun(
            r.id,
            r.plan_id,
            r.project_version_id,
            plan.trace_digest,
            plan.package_digest,
            state,
            r.started_at,
            r.completed_at,
            outcome.outcome if outcome else None,
            outcome.returned_model if outcome else None,
            tuple(outcome.rejection_codes) if outcome else (),
            tuple(outcome.unknowns) if outcome else (),
            tuple(
                ExplanationClaim(
                    c.ordinal, c.level, c.text, tuple(c.steps), tuple(c.citations), c.claim_digest
                )
                for c in claims
            ),
            len(attempts),
        )
