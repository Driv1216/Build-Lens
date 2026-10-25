"""PostgreSQL coordination for semantic generation: plans, fenced runs, candidates, index.

All coordination uses rows, row locks and database time (``clock_timestamp()`` after a lock is
acquired). There is no job platform: one ``executing`` run per snapshot is enforced by a unique
partial index, a plan by ``UNIQUE plan_id``, the budget in T1 under the run lock, and every
write by the fencing predicate (§11.3).
"""

from __future__ import annotations

import hashlib
import secrets
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from buildlens.application.semantic_generation import (
    BudgetExhaustedError,
    GenerationConflictError,
    NewPlan,
    OutcomeRecord,
    PlanAlreadyUsedError,
    PlanChangedError,
    PlanConfigChangedError,
    PlanExpiredError,
    PlanInvalidError,
    PlanNotFoundError,
    Reusable,
    RunFencedError,
    RunResults,
)
from buildlens.domain.semantic_generation import (
    CANDIDATE_OUTCOMES,
    Attempt,
    Citation,
    Claim,
    GenerationPlan,
    GenerationRun,
    IndexEntry,
    Interpretation,
    LocatorContribution,
    PlanItem,
    RunState,
)
from buildlens.domain.source import digest
from buildlens.infrastructure.persistence.models import (
    ArtifactProcessingRecord,
    ChunkRecord,
    ProjectVersionRecord,
    SemanticCatalogRunRecord,
    SemanticClaimRecord,
    SemanticFactRecord,
    SemanticGenerationAttemptRecord,
    SemanticGenerationLateReceiptRecord,
    SemanticGenerationOutcomeRecord,
    SemanticGenerationPlanItemRecord,
    SemanticGenerationPlanRecord,
    SemanticGenerationRunRecord,
    SemanticIndexEntryRecord,
    SemanticInterpretationRecord,
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _id(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def plan_material(
    plan: SemanticGenerationPlanRecord, items: Sequence[SemanticGenerationPlanItemRecord]
) -> dict[str, Any]:
    """Canonical serialisation of the *stored* plan and its items, nonce included (§11.1)."""
    return {
        "id": str(plan.id),
        "project_id": str(plan.project_id),
        "snapshot_id": str(plan.project_version_id),
        "plan_nonce": plan.plan_nonce,
        "issued_at": _iso(plan.issued_at),
        "expires_at": _iso(plan.expires_at),
        "selection": plan.selection,
        "mode": plan.mode,
        "accept_profiles": plan.accept_profiles,
        "provider_config": plan.provider_config,
        "provider_config_digest": plan.provider_config_digest,
        "generator_profile_digest": plan.generator_profile_digest,
        "sharing_class": plan.sharing_class,
        "catalog_run_id": str(plan.catalog_run_id),
        "structural_analysis_id": str(plan.structural_analysis_id),
        "processing_run_id": str(plan.processing_run_id),
        "index_run_id": _id(plan.index_run_id),
        "caps": plan.caps,
        "totals": plan.totals,
        "contributing_locators": plan.contributing_locators,
        "retention_note": plan.retention_note,
        "items": [
            {
                "ordinal": item.ordinal,
                "entity_key": item.entity_key,
                "disposition": item.disposition,
                "reason": item.reason,
                "context_coverage": item.context_coverage,
                "package_digest": item.package_digest,
                "input_key": item.input_key,
                "request_digest": item.request_digest,
                "request_bytes": item.request_bytes,
                "contributing_locators": item.contributing_locators,
                "reuse_interpretation_id": _id(item.reuse_interpretation_id),
                "reuse_profile_digest": item.reuse_profile_digest,
                "reuse_provider": item.reuse_provider,
                "reuse_returned_model": item.reuse_returned_model,
            }
            for item in sorted(items, key=lambda value: value.ordinal)
        ],
    }


def _item(x: SemanticGenerationPlanItemRecord) -> PlanItem:
    return PlanItem(
        x.ordinal,
        x.entity_key,
        cast(Any, x.disposition),
        x.reason,
        x.context_coverage,
        x.package_digest,
        x.input_key,
        x.request_digest,
        x.request_bytes,
        tuple(
            LocatorContribution(
                cast(str | None, value.get("artifact_identity_id")),
                str(value["locator"]),
                cast(Any, value["role"]),
                int(cast(int, value["bytes"])),
            )
            for value in x.contributing_locators
        ),
        x.reuse_interpretation_id,
        x.reuse_profile_digest,
        x.reuse_provider,
        x.reuse_returned_model,
    )


def _plan(
    x: SemanticGenerationPlanRecord,
    items: Sequence[SemanticGenerationPlanItemRecord],
    run_id: UUID | None,
) -> GenerationPlan:
    return GenerationPlan(
        x.id,
        x.project_id,
        x.project_version_id,
        x.plan_nonce,
        x.issued_at,
        x.expires_at,
        dict(x.selection),
        cast(Any, x.mode),
        cast(Any, x.accept_profiles),
        dict(x.provider_config),
        x.provider_config_digest,
        x.generator_profile_digest,
        cast(Any, x.sharing_class),
        x.catalog_run_id,
        x.structural_analysis_id,
        x.processing_run_id,
        x.index_run_id,
        dict(x.caps),
        dict(x.totals),
        list(x.contributing_locators),
        x.plan_digest,
        tuple(_item(item) for item in sorted(items, key=lambda value: value.ordinal)),
        run_id,
        x.retention_note,
    )


def _run(x: SemanticGenerationRunRecord) -> GenerationRun:
    return GenerationRun(
        x.id,
        x.project_id,
        x.project_version_id,
        x.plan_id,
        x.plan_digest,
        cast(Any, x.state),
        x.catalog_run_id,
        x.structural_analysis_id,
        x.processing_run_id,
        x.captured_index_run_id,
        dict(x.caps),
        x.attempts_started,
        x.lease_token,
        x.lease_expires_at,
        x.deadline_at,
        x.started_at,
        x.completed_at,
        dict(x.totals),
        tuple(x.diagnostics),
    )


def _citation_json(value: Citation) -> dict[str, Any]:
    return {
        "id": value.evidence_id,
        "kind": value.kind,
        "quote_sha256": value.quote_sha256,
        "start": value.start,
        "end": value.end,
        "basis": value.basis,
        "stable_key": value.stable_key,
        "content_hash": value.content_hash,
        "fact_digest": value.fact_digest,
        "locator": value.locator,
        "start_line": value.start_line,
        "end_line": value.end_line,
    }


def _citation(value: dict[str, Any]) -> Citation:
    return Citation(
        value["id"],
        value["kind"],
        value["quote_sha256"],
        value["start"],
        value["end"],
        value["basis"],
        value.get("stable_key"),
        value.get("content_hash"),
        value.get("fact_digest"),
        value.get("locator"),
        value.get("start_line"),
        value.get("end_line"),
    )


def _claim(x: SemanticClaimRecord) -> Claim:
    return Claim(
        x.ordinal,
        cast(Any, x.category),
        x.text,
        cast(Any, x.status),
        cast(Any, x.basis),
        cast(Any, x.basis_label),
        cast(dict[str, Any] | None, x.assertion),
        cast(Any, x.assertion_outcome),
        x.scope_warning,
        tuple(_citation(cast(dict[str, Any], item)) for item in x.citations),
    )


def _interpretation(
    x: SemanticInterpretationRecord, claims: Sequence[SemanticClaimRecord] = ()
) -> Interpretation:
    return Interpretation(
        x.id,
        x.project_id,
        x.project_version_id,
        x.run_id,
        x.attempt_id,
        x.entity_key,
        x.input_key,
        dict(x.manifest),
        x.generator_profile_digest,
        x.provider,
        x.requested_model,
        x.returned_model,
        x.prompt_version,
        x.schema_version,
        x.validator_version,
        cast(Any, x.status),
        tuple(x.unknowns),
        x.created_at,
        tuple(_claim(claim) for claim in sorted(claims, key=lambda value: value.ordinal)),
    )


class SqlAlchemyGenerationStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _now(session: Session) -> datetime:
        return cast(datetime, session.scalar(select(func.clock_timestamp())))

    @staticmethod
    def _version(
        session: Session, project_id: UUID, snapshot_id: UUID, mode: str
    ) -> ProjectVersionRecord:
        query = select(ProjectVersionRecord).where(
            ProjectVersionRecord.id == snapshot_id, ProjectVersionRecord.project_id == project_id
        )
        query = query.with_for_update(read=mode == "share")
        version = session.scalar(query)
        if version is None:
            raise PlanChangedError("Snapshot disappeared.")
        return cast(ProjectVersionRecord, version)

    @staticmethod
    def _locked_run(session: Session, run_id: UUID) -> SemanticGenerationRunRecord:
        record = session.scalar(
            select(SemanticGenerationRunRecord)
            .where(SemanticGenerationRunRecord.id == run_id)
            .with_for_update()
        )
        if record is None:
            raise RunFencedError("Run disappeared.")
        return cast(SemanticGenerationRunRecord, record)

    @staticmethod
    def _fenced(record: SemanticGenerationRunRecord, lease_token: UUID, now: datetime) -> bool:
        """The §11.3 fencing predicate, evaluated under the run row lock with DB time."""
        return not (
            record.state == "executing"
            and record.lease_token == lease_token
            and record.lease_expires_at > now
            and now < record.deadline_at
        )

    @staticmethod
    def _abandon(session: Session, run_ids: Sequence[UUID]) -> None:
        if not run_ids:
            return
        attempts = session.scalars(
            select(SemanticGenerationAttemptRecord.id).where(
                SemanticGenerationAttemptRecord.run_id.in_(list(run_ids)),
                ~SemanticGenerationAttemptRecord.id.in_(
                    select(SemanticGenerationOutcomeRecord.attempt_id)
                ),
            )
        ).all()
        if attempts:
            session.execute(
                insert(SemanticGenerationOutcomeRecord),
                [
                    {
                        "id": uuid4(),
                        "attempt_id": attempt,
                        "outcome": "outcome_unknown",
                        "rejection_codes": ["RUN_ABANDONED"],
                    }
                    for attempt in attempts
                ],
            )

    # ----------------------------------------------------------------- plans

    def active_index_run_id(self, project_id: UUID, snapshot_id: UUID) -> UUID | None:
        with self._session_factory() as session:
            return cast(
                UUID | None,
                session.scalar(
                    select(ProjectVersionRecord.active_semantic_index_run_id).where(
                        ProjectVersionRecord.id == snapshot_id,
                        ProjectVersionRecord.project_id == project_id,
                    )
                ),
            )

    def reusable(self, project_id: UUID, input_keys: Sequence[str]) -> dict[str, list[Reusable]]:
        if not input_keys:
            return {}
        values: dict[str, list[Reusable]] = defaultdict(list)
        with self._session_factory() as session:
            for record in session.scalars(
                select(SemanticInterpretationRecord)
                .where(
                    SemanticInterpretationRecord.project_id == project_id,
                    SemanticInterpretationRecord.input_key.in_(list(input_keys)),
                )
                .order_by(SemanticInterpretationRecord.created_at.desc())
            ):
                values[record.input_key].append(
                    Reusable(
                        record.id,
                        record.generator_profile_digest,
                        record.provider,
                        record.returned_model,
                    )
                )
        return values

    def insert_plan(self, plan: NewPlan) -> GenerationPlan:
        with self._session_factory.begin() as session:
            now = self._now(session)
            record = SemanticGenerationPlanRecord(
                id=uuid4(),
                project_id=plan.project_id,
                project_version_id=plan.snapshot_id,
                plan_nonce=secrets.token_hex(32),
                issued_at=now,
                expires_at=now + timedelta(seconds=plan.ttl_seconds),
                selection=plan.selection,
                mode=plan.mode,
                accept_profiles=plan.accept_profiles,
                provider_config=plan.config.as_dict(),
                provider_config_digest=plan.config.digest,
                generator_profile_digest=plan.config.profile_digest,
                sharing_class=plan.config.sharing_class,
                catalog_run_id=plan.catalog_run_id,
                structural_analysis_id=plan.structural_analysis_id,
                processing_run_id=plan.processing_run_id,
                index_run_id=plan.index_run_id,
                caps=plan.caps,
                totals=plan.totals,
                contributing_locators=plan.contributing_locators,
                retention_note=plan.retention_note,
                plan_digest="0" * 64,
            )
            items = [
                SemanticGenerationPlanItemRecord(
                    id=uuid4(),
                    plan_id=record.id,
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
                        {
                            "artifact_identity_id": value.artifact_identity_id,
                            "locator": value.locator,
                            "role": value.role,
                            "bytes": value.bytes,
                        }
                        for value in item.contributing_locators
                    ],
                    reuse_interpretation_id=item.reuse_interpretation_id,
                    reuse_profile_digest=item.reuse_profile_digest,
                    reuse_provider=item.reuse_provider,
                    reuse_returned_model=item.reuse_returned_model,
                )
                for item in plan.items
            ]
            session.add(record)
            session.flush()
            session.add_all(items)
            session.flush()
            session.expire_all()
            stored = session.get(SemanticGenerationPlanRecord, record.id)
            assert stored is not None
            stored_items = self._items(session, stored.id)
            stored.plan_digest = digest(plan_material(stored, stored_items))
            session.flush()
            return _plan(stored, stored_items, None)

    @staticmethod
    def _items(session: Session, plan_id: UUID) -> list[SemanticGenerationPlanItemRecord]:
        return list(
            session.scalars(
                select(SemanticGenerationPlanItemRecord)
                .where(SemanticGenerationPlanItemRecord.plan_id == plan_id)
                .order_by(SemanticGenerationPlanItemRecord.ordinal)
            )
        )

    def plan(self, project_id: UUID, snapshot_id: UUID, plan_id: UUID) -> GenerationPlan | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(SemanticGenerationPlanRecord).where(
                    SemanticGenerationPlanRecord.id == plan_id,
                    SemanticGenerationPlanRecord.project_id == project_id,
                    SemanticGenerationPlanRecord.project_version_id == snapshot_id,
                )
            )
            if record is None:
                return None
            run_id = session.scalar(
                select(SemanticGenerationRunRecord.id).where(
                    SemanticGenerationRunRecord.plan_id == plan_id
                )
            )
            return _plan(record, self._items(session, plan_id), run_id)

    # ----------------------------------------------------------------- T0 admit

    def admit(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        plan_id: UUID,
        plan_digest: str,
        config_digest: str,
        lease_seconds: float,
    ) -> tuple[GenerationRun, datetime]:
        with self._session_factory.begin() as session:
            version = self._version(session, project_id, snapshot_id, "update")
            plan = session.scalar(
                select(SemanticGenerationPlanRecord).where(
                    SemanticGenerationPlanRecord.id == plan_id,
                    SemanticGenerationPlanRecord.project_version_id == snapshot_id,
                    SemanticGenerationPlanRecord.project_id == project_id,
                )
            )
            if plan is None:
                raise PlanNotFoundError("Plan was not found.")
            recomputed = digest(plan_material(plan, self._items(session, plan.id)))
            if recomputed != plan.plan_digest or plan_digest != plan.plan_digest:
                raise PlanInvalidError("The plan or its digest does not verify.")
            if session.scalar(
                select(SemanticGenerationRunRecord.id).where(
                    SemanticGenerationRunRecord.plan_id == plan.id
                )
            ):
                raise PlanAlreadyUsedError("The plan was already consumed.")
            now = self._now(session)
            if now >= plan.expires_at:
                raise PlanExpiredError("The plan has expired.")
            if config_digest != plan.provider_config_digest:
                raise PlanConfigChangedError("Provider configuration changed since the plan.")
            if (
                version.active_semantic_catalog_run_id != plan.catalog_run_id
                or version.active_analysis_id != plan.structural_analysis_id
                or version.active_processing_run_id != plan.processing_run_id
                or version.active_semantic_index_run_id != plan.index_run_id
            ):
                raise PlanChangedError("Authority changed since the plan.")
            expired = session.scalars(
                update(SemanticGenerationRunRecord)
                .where(
                    SemanticGenerationRunRecord.project_version_id == snapshot_id,
                    SemanticGenerationRunRecord.state == "executing",
                    SemanticGenerationRunRecord.lease_expires_at <= now,
                )
                .values(state="abandoned", completed_at=now)
                .returning(SemanticGenerationRunRecord.id)
            ).all()
            self._abandon(session, expired)
            record = SemanticGenerationRunRecord(
                id=uuid4(),
                project_id=project_id,
                project_version_id=snapshot_id,
                plan_id=plan.id,
                plan_digest=plan.plan_digest,
                state="executing",
                catalog_run_id=plan.catalog_run_id,
                structural_analysis_id=plan.structural_analysis_id,
                processing_run_id=plan.processing_run_id,
                captured_index_run_id=plan.index_run_id,
                caps=plan.caps,
                attempts_started=0,
                lease_token=uuid4(),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                deadline_at=now
                + timedelta(seconds=float(cast(float, plan.caps["deadline_seconds"]))),
                started_at=now,
                totals={},
                diagnostics=[],
            )
            try:
                with session.begin_nested():
                    session.add(record)
                    session.flush()
            except IntegrityError as error:
                constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", "")
                if constraint == "uq_semantic_generation_runs_plan":
                    raise PlanAlreadyUsedError("The plan was already consumed.") from error
                if constraint == "uq_semantic_generation_runs_one_executing":
                    raise GenerationConflictError("Another run is executing.") from error
                raise
            return _run(record), now

    # ----------------------------------------------------------------- T1, recheck, T2

    def begin_attempt(self, run: GenerationRun, item: PlanItem, lease_seconds: float) -> Attempt:
        if item.disposition != "generate" or not item.request_digest:
            raise ValueError("Only planned generate items may start an attempt.")
        with self._session_factory.begin() as session:
            record = self._locked_run(session, run.id)
            now = self._now(session)
            if self._fenced(record, run.lease_token, now):
                raise RunFencedError("The run is fenced.")
            caps = record.caps
            if record.attempts_started >= int(cast(int, caps["max_outbound_requests"])):
                raise BudgetExhaustedError("The outbound request budget is exhausted.")
            previous = int(
                session.scalar(
                    select(func.count())
                    .select_from(SemanticGenerationAttemptRecord)
                    .where(
                        SemanticGenerationAttemptRecord.run_id == run.id,
                        SemanticGenerationAttemptRecord.entity_key == item.entity_key,
                    )
                )
                or 0
            )
            if previous >= int(cast(int, caps["max_attempts_per_entity"])):
                raise BudgetExhaustedError("The per-entity attempt budget is exhausted.")
            attempt = SemanticGenerationAttemptRecord(
                id=uuid4(),
                run_id=run.id,
                entity_key=item.entity_key,
                attempt_no=previous + 1,
                package_digest=item.package_digest or "",
                request_digest=item.request_digest or "",
                input_key=item.input_key or "",
                started_at=now,
            )
            session.add(attempt)
            record.attempts_started += 1
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            session.flush()
            return Attempt(
                attempt.id,
                run.id,
                attempt.entity_key,
                attempt.attempt_no,
                attempt.package_digest,
                attempt.request_digest,
                attempt.input_key,
                now,
            )

    def authority_unchanged(self, run: GenerationRun) -> bool:
        with self._session_factory.begin() as session:
            version = self._version(session, run.project_id, run.snapshot_id, "share")
            return bool(
                version.active_semantic_catalog_run_id == run.catalog_run_id
                and version.active_analysis_id == run.structural_analysis_id
                and version.active_processing_run_id == run.processing_run_id
                and version.active_semantic_index_run_id == run.captured_index_run_id
            )

    def existing_candidate(
        self, project_id: UUID, input_key: str, profile_digest: str
    ) -> UUID | None:
        with self._session_factory() as session:
            return cast(
                UUID | None,
                session.scalar(
                    select(SemanticInterpretationRecord.id)
                    .where(
                        SemanticInterpretationRecord.project_id == project_id,
                        SemanticInterpretationRecord.input_key == input_key,
                        SemanticInterpretationRecord.generator_profile_digest == profile_digest,
                    )
                    .order_by(SemanticInterpretationRecord.created_at.desc())
                    .limit(1)
                ),
            )

    def record_outcome(
        self,
        run: GenerationRun,
        attempt: Attempt,
        outcome: OutcomeRecord,
        interpretation: Interpretation | None,
        lease_seconds: float,
    ) -> None:
        reason: str | None = None
        with self._session_factory.begin() as session:
            record = self._locked_run(session, run.id)
            now = self._now(session)
            if self._fenced(record, run.lease_token, now):
                reason = "RUN_TERMINAL" if record.state != "executing" else "RUN_FENCED"
            else:
                session.add(
                    SemanticGenerationOutcomeRecord(
                        id=uuid4(),
                        attempt_id=attempt.id,
                        outcome=outcome.outcome,
                        returned_model=outcome.returned_model,
                        input_tokens=outcome.input_tokens,
                        output_tokens=outcome.output_tokens,
                        latency_ms=outcome.latency_ms,
                        response_digest=outcome.response_digest,
                        rejection_codes=list(outcome.rejection_codes),
                        retained_output=outcome.retained_output,
                        recorded_at=now,
                    )
                )
                if interpretation is not None and outcome.outcome in CANDIDATE_OUTCOMES:
                    self._insert_interpretation(session, interpretation, now)
                record.lease_expires_at = now + timedelta(seconds=lease_seconds)
                session.flush()
        if reason is None:
            return
        if outcome.response_digest is not None:
            # A response arrived after fencing: record it honestly, never as a candidate.
            with self._session_factory.begin() as session:
                session.add(
                    SemanticGenerationLateReceiptRecord(
                        id=uuid4(),
                        attempt_id=attempt.id,
                        response_digest=outcome.response_digest,
                        input_tokens=outcome.input_tokens,
                        output_tokens=outcome.output_tokens,
                        reason=reason,
                    )
                )
        raise RunFencedError("The run was fenced before the outcome could be recorded.")

    @staticmethod
    def _insert_interpretation(session: Session, value: Interpretation, now: datetime) -> None:
        session.add(
            SemanticInterpretationRecord(
                id=value.id,
                project_id=value.project_id,
                project_version_id=value.snapshot_id,
                run_id=value.run_id,
                attempt_id=value.attempt_id,
                entity_key=value.entity_key,
                input_key=value.input_key,
                manifest=value.manifest,
                generator_profile_digest=value.generator_profile_digest,
                provider=value.provider,
                requested_model=value.requested_model,
                returned_model=value.returned_model,
                prompt_version=value.prompt_version,
                schema_version=value.schema_version,
                validator_version=value.validator_version,
                status=value.status,
                unknowns=list(value.unknowns),
                created_at=now,
            )
        )
        session.flush()
        if value.claims:
            session.execute(
                insert(SemanticClaimRecord),
                [
                    {
                        "id": uuid4(),
                        "interpretation_id": value.id,
                        "ordinal": claim.ordinal,
                        "category": claim.category,
                        "text": claim.text,
                        "status": claim.status,
                        "basis": claim.basis,
                        "basis_label": claim.basis_label,
                        "assertion": claim.assertion,
                        "assertion_outcome": claim.assertion_outcome,
                        "scope_warning": claim.scope_warning,
                        "citations": [_citation_json(item) for item in claim.citations],
                    }
                    for claim in value.claims
                ],
            )

    # ----------------------------------------------------------------- T4 and recovery

    def finish(self, run: GenerationRun, state: RunState, results: RunResults) -> GenerationRun:
        with self._session_factory.begin() as session:
            version = self._version(session, run.project_id, run.snapshot_id, "update")
            record = self._locked_run(session, run.id)
            now = self._now(session)
            if self._fenced(record, run.lease_token, now):
                raise RunFencedError("The run is fenced; it cannot publish.")
            totals = {
                "generated": results.generated,
                "reused": results.reused,
                "failed": results.failed,
                "not_attempted": results.not_attempted,
                "attempts_started": record.attempts_started,
            }
            diagnostics = [results.stop_reason] if results.stop_reason else []
            unchanged = (
                version.active_semantic_catalog_run_id == record.catalog_run_id
                and version.active_analysis_id == record.structural_analysis_id
                and version.active_processing_run_id == record.processing_run_id
                and version.active_semantic_index_run_id == record.captured_index_run_id
            )
            final: RunState = state if unchanged else "superseded"
            if final in {"complete", "partial"}:
                entries: dict[str, dict[str, Any]] = {}
                if version.active_semantic_index_run_id is not None:
                    for previous in session.scalars(
                        select(SemanticIndexEntryRecord).where(
                            SemanticIndexEntryRecord.run_id == version.active_semantic_index_run_id
                        )
                    ):
                        entries[previous.entity_key] = {
                            "interpretation_id": previous.interpretation_id,
                            "mode": "retained",
                            "reused_from_profile": previous.reused_from_profile,
                        }
                for key, entry in results.entries.items():
                    entries[key] = {
                        "interpretation_id": entry.interpretation_id,
                        "mode": entry.mode,
                        "reused_from_profile": entry.reused_from_profile,
                    }
                if entries:
                    session.execute(
                        insert(SemanticIndexEntryRecord),
                        [
                            {
                                "id": uuid4(),
                                "run_id": record.id,
                                "project_id": record.project_id,
                                "entity_key": key,
                                **value,
                            }
                            for key, value in sorted(entries.items())
                        ],
                    )
            changed = session.execute(
                update(SemanticGenerationRunRecord)
                .where(
                    SemanticGenerationRunRecord.id == record.id,
                    SemanticGenerationRunRecord.state == "executing",
                )
                .values(state=final, completed_at=now, totals=totals, diagnostics=diagnostics)
            ).rowcount
            if changed != 1:
                raise RunFencedError("The run left the executing state.")
            if final in {"complete", "partial"}:
                version.active_semantic_index_run_id = record.id
            session.flush()
            session.refresh(record)
            return _run(record)

    def abandon_expired(self, project_id: UUID, snapshot_id: UUID) -> list[UUID]:
        with self._session_factory.begin() as session:
            self._version(session, project_id, snapshot_id, "update")
            now = self._now(session)
            expired = list(
                session.scalars(
                    update(SemanticGenerationRunRecord)
                    .where(
                        SemanticGenerationRunRecord.project_version_id == snapshot_id,
                        SemanticGenerationRunRecord.state == "executing",
                        SemanticGenerationRunRecord.lease_expires_at <= now,
                    )
                    .values(state="abandoned", completed_at=now)
                    .returning(SemanticGenerationRunRecord.id)
                ).all()
            )
            self._abandon(session, expired)
            return expired

    def self_abandon(self, run: GenerationRun) -> bool:
        """A fenced process abandons its own run once its lease or deadline has passed."""
        with self._session_factory.begin() as session:
            record = self._locked_run(session, run.id)
            now = self._now(session)
            if (
                record.state != "executing"
                or record.lease_token != run.lease_token
                or (record.lease_expires_at > now and now < record.deadline_at)
            ):
                return False
            record.state, record.completed_at = "abandoned", now
            session.flush()
            self._abandon(session, [record.id])
            return True

    # ----------------------------------------------------------------- reads

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> GenerationRun | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(SemanticGenerationRunRecord).where(
                    SemanticGenerationRunRecord.id == run_id,
                    SemanticGenerationRunRecord.project_id == project_id,
                    SemanticGenerationRunRecord.project_version_id == snapshot_id,
                )
            )
            return _run(record) if record else None

    def runs(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[GenerationRun], int]:
        with self._session_factory() as session:
            scope = (
                SemanticGenerationRunRecord.project_id == project_id,
                SemanticGenerationRunRecord.project_version_id == snapshot_id,
            )
            total = session.scalar(
                select(func.count()).select_from(SemanticGenerationRunRecord).where(*scope)
            )
            records = session.scalars(
                select(SemanticGenerationRunRecord)
                .where(*scope)
                .order_by(SemanticGenerationRunRecord.started_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return [_run(record) for record in records], int(total or 0)

    def attempts(self, run_id: UUID) -> list[Attempt]:
        with self._session_factory() as session:
            rows = session.execute(
                select(SemanticGenerationAttemptRecord, SemanticGenerationOutcomeRecord)
                .outerjoin(
                    SemanticGenerationOutcomeRecord,
                    SemanticGenerationOutcomeRecord.attempt_id
                    == SemanticGenerationAttemptRecord.id,
                )
                .where(SemanticGenerationAttemptRecord.run_id == run_id)
                .order_by(SemanticGenerationAttemptRecord.started_at)
            ).all()
            receipts = dict(
                session.execute(
                    select(
                        SemanticGenerationLateReceiptRecord.attempt_id,
                        func.count(SemanticGenerationLateReceiptRecord.id),
                    )
                    .join(
                        SemanticGenerationAttemptRecord,
                        SemanticGenerationAttemptRecord.id
                        == SemanticGenerationLateReceiptRecord.attempt_id,
                    )
                    .where(SemanticGenerationAttemptRecord.run_id == run_id)
                    .group_by(SemanticGenerationLateReceiptRecord.attempt_id)
                ).all()
            )
            return [
                Attempt(
                    attempt.id,
                    attempt.run_id,
                    attempt.entity_key,
                    attempt.attempt_no,
                    attempt.package_digest,
                    attempt.request_digest,
                    attempt.input_key,
                    attempt.started_at,
                    outcome.outcome if outcome else None,
                    outcome.returned_model if outcome else None,
                    outcome.input_tokens if outcome else None,
                    outcome.output_tokens if outcome else None,
                    outcome.latency_ms if outcome else None,
                    tuple(outcome.rejection_codes) if outcome else (),
                    int(receipts.get(attempt.id, 0)),
                )
                for attempt, outcome in rows
            ]

    def index_entries(self, index_run_id: UUID) -> dict[str, tuple[IndexEntry, Interpretation]]:
        with self._session_factory() as session:
            rows = session.execute(
                select(SemanticIndexEntryRecord, SemanticInterpretationRecord)
                .join(
                    SemanticInterpretationRecord,
                    SemanticInterpretationRecord.id == SemanticIndexEntryRecord.interpretation_id,
                )
                .where(SemanticIndexEntryRecord.run_id == index_run_id)
            ).all()
            return {
                entry.entity_key: (
                    IndexEntry(
                        entry.entity_key,
                        entry.interpretation_id,
                        cast(Any, entry.mode),
                        entry.reused_from_profile,
                    ),
                    _interpretation(interpretation),
                )
                for entry, interpretation in rows
            }

    def interpretation(self, project_id: UUID, interpretation_id: UUID) -> Interpretation | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(SemanticInterpretationRecord).where(
                    SemanticInterpretationRecord.id == interpretation_id,
                    SemanticInterpretationRecord.project_id == project_id,
                )
            )
            if record is None:
                return None
            claims = session.scalars(
                select(SemanticClaimRecord).where(
                    SemanticClaimRecord.interpretation_id == record.id
                )
            ).all()
            return _interpretation(record, claims)

    def history(self, project_id: UUID, entity_key: str) -> list[Interpretation]:
        with self._session_factory() as session:
            return [
                _interpretation(record)
                for record in session.scalars(
                    select(SemanticInterpretationRecord)
                    .where(
                        SemanticInterpretationRecord.project_id == project_id,
                        SemanticInterpretationRecord.entity_key == entity_key,
                    )
                    .order_by(SemanticInterpretationRecord.created_at.desc())
                    .limit(100)
                )
            ]

    def resolve_citations(self, project_id: UUID, interpretation: Interpretation) -> Interpretation:
        """Hash-verify every citation at read time (F2); quote text is never stored."""
        if not interpretation.claims:
            loaded = self.interpretation(project_id, interpretation.id)
            interpretation = loaded or interpretation
        citations = [item for claim in interpretation.claims for item in claim.citations]
        pairs = {
            (item.stable_key, item.content_hash)
            for item in citations
            if item.kind == "chunk" and item.stable_key and item.content_hash
        }
        digests = {
            item.fact_digest for item in citations if item.kind == "fact" and item.fact_digest
        }
        chunks: dict[tuple[str, str], str] = {}
        facts: dict[str, SemanticFactRecord] = {}
        with self._session_factory() as session:
            if pairs:
                for record in session.scalars(
                    select(ChunkRecord)
                    .join(
                        ArtifactProcessingRecord,
                        ArtifactProcessingRecord.id == ChunkRecord.artifact_processing_id,
                    )
                    .join(
                        ProjectVersionRecord,
                        ProjectVersionRecord.id == ArtifactProcessingRecord.project_version_id,
                    )
                    .where(
                        ProjectVersionRecord.project_id == project_id,
                        tuple_(ChunkRecord.stable_key, ChunkRecord.content_hash).in_(
                            [(a, b) for a, b in pairs if a and b]
                        ),
                    )
                ):
                    chunks.setdefault((record.stable_key, record.content_hash), record.content)
            if digests:
                for record in session.scalars(
                    select(SemanticFactRecord)
                    .join(
                        SemanticCatalogRunRecord,
                        SemanticCatalogRunRecord.id == SemanticFactRecord.catalog_run_id,
                    )
                    .where(
                        SemanticCatalogRunRecord.project_id == project_id,
                        SemanticFactRecord.fact_digest.in_(list(digests)),
                    )
                ):
                    facts.setdefault(record.fact_digest, record)
        from buildlens.infrastructure.persistence.semantic import (
            SqlAlchemySemanticCatalogStore,
            _fact,
        )

        verified_facts = SqlAlchemySemanticCatalogStore(self._session_factory).verify_facts(
            [_fact(record) for record in facts.values()]
        )

        def resolve(item: Citation) -> Citation:
            if item.kind == "chunk":
                content = chunks.get((item.stable_key or "", item.content_hash or ""))
                if (
                    content is None
                    or hashlib.sha256(content.encode()).hexdigest() != item.content_hash
                ):
                    return replace(item, verification="CITATION_SOURCE_MISSING")
                quote = content[item.start : item.end]
            else:
                fact = facts.get(item.fact_digest or "")
                if fact is None or verified_facts.get(fact.fact_digest) != "verified":
                    return replace(item, verification="CITATION_SOURCE_MISSING")
                quote = fact.rendered_text[item.start : item.end]
            if hashlib.sha256(quote.encode("utf-8")).hexdigest() != item.quote_sha256:
                return replace(item, verification="CITATION_MISMATCH")
            return replace(item, verification="verified", quote=quote)

        return replace(
            interpretation,
            claims=tuple(
                replace(claim, citations=tuple(resolve(item) for item in claim.citations))
                for claim in interpretation.claims
            ),
        )
