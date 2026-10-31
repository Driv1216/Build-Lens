"""PostgreSQL persistence for Stage 15 flow evidence runs.

Publication follows the Stage 13/14 pattern: snapshot ``FOR UPDATE``, captured authority (the
active structural analysis must still be the one the run read), a ``superseded`` flag, and
retained failures. The only pre-existing table this store writes is ``project_versions``, and
only its Stage 15 column ``active_flow_evidence_run_id`` (INV-3).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from buildlens.application.flow_evidence import (
    ArtifactInput,
    FlowEvidenceConflictError,
    FlowEvidencePayload,
    OwnerKey,
    ReuseSource,
    SnapshotSource,
    StructuralInput,
    VersionFileInput,
)
from buildlens.domain.flow_evidence import (
    ArtifactFlowCoverage,
    DisplayText,
    FlowCallsite,
    FlowEvidenceRun,
    FlowExtraction,
    FlowJsxBinding,
    FlowScope,
    FrameworkFact,
    flow_pass_contract,
)
from buildlens.domain.source import digest, structural_contract
from buildlens.domain.structural import ParseDiagnostic, SourceSpan
from buildlens.infrastructure.persistence.models import (
    ArtifactAnalysisRecord,
    ArtifactRecord,
    FlowArtifactCoverageRecord,
    FlowCallsiteRecord,
    FlowDisplayTextRecord,
    FlowEvidenceRunRecord,
    FlowFrameworkFactRecord,
    FlowJsxBindingRecord,
    FlowScopeRecord,
    ProjectVersionRecord,
    StructuralAnalysisRecord,
    SymbolRecord,
)
from buildlens.infrastructure.structural.framework_versions import is_version_file

_BATCH = 1000


def _run(record: FlowEvidenceRunRecord) -> FlowEvidenceRun:
    return FlowEvidenceRun(
        record.id,
        record.project_id,
        record.project_version_id,
        record.revision,
        record.started_at,
        record.completed_at,
        record.outcome,
        record.structural_analysis_id,
        record.structural_revision,
        dict(record.contract),
        record.contract_digest,
        record.input_authority_digest,
        record.evidence_digest,
        dict(record.coverage),
        tuple(record.diagnostics),
        record.superseded,
        record.captured_active_flow_evidence_run_id,
        record.captured_latest_revision,
        record.failure_code,
    )


def _coverage(record: FlowArtifactCoverageRecord) -> ArtifactFlowCoverage:
    return ArtifactFlowCoverage(
        record.artifact_analysis_id,
        record.artifact_id,
        record.locator,
        record.language,
        record.state,
        record.extractor,
        record.contract_digest,
        record.input_fingerprint,
        record.execution_mode,
        record.reuse_source_run_id,
        record.evidence_digest,
        record.scope_count,
        record.callsite_count,
        tuple(record.diagnostics),
    )


def _span(record: Any) -> SourceSpan:
    return SourceSpan(record.start_line, record.end_line, record.start_column, record.end_column)


def _span_values(span: SourceSpan, start: int | None, end: int | None) -> dict[str, Any]:
    return {
        "start_line": span.start_line,
        "end_line": span.end_line,
        "start_column": span.start_column,
        "end_column": span.end_column,
        "start_offset": start,
        "end_offset": end,
    }


class SqlAlchemyFlowEvidenceStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ----------------------------------------------------------------- authority

    def snapshot_source(self, project_id: UUID, snapshot_id: UUID) -> SnapshotSource | None:
        with self._session_factory() as session:
            version = session.scalar(
                select(ProjectVersionRecord).where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                )
            )
            if version is None:
                return None
            observation = version.root_observation or {}
            expected = (
                (int(str(observation["device"])), int(str(observation["inode"])))
                if "device" in observation and "inode" in observation
                else None
            )
            return SnapshotSource(
                version.canonical_root,
                version.scan_status,
                expected,
                bool(version.legacy_unverified),
            )

    def active_structure(self, project_id: UUID, snapshot_id: UUID) -> tuple[UUID, int] | None:
        with self._session_factory() as session:
            row = session.execute(
                select(StructuralAnalysisRecord.id, StructuralAnalysisRecord.revision)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.active_analysis_id == StructuralAnalysisRecord.id,
                )
                .where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                    StructuralAnalysisRecord.quarantined.is_(False),
                    StructuralAnalysisRecord.producer_contract == structural_contract(),
                )
            ).first()
            return (row[0], row[1]) if row else None

    def structural_input(
        self, project_id: UUID, snapshot_id: UUID, analysis_id: UUID
    ) -> StructuralInput:
        with self._session_factory() as session:
            revision = session.scalar(
                select(StructuralAnalysisRecord.revision).where(
                    StructuralAnalysisRecord.id == analysis_id,
                    StructuralAnalysisRecord.project_version_id == snapshot_id,
                )
            )
            rows = session.execute(
                select(ArtifactAnalysisRecord, ArtifactRecord)
                .join(ArtifactRecord, ArtifactRecord.id == ArtifactAnalysisRecord.artifact_id)
                .where(ArtifactAnalysisRecord.structural_analysis_id == analysis_id)
                .order_by(ArtifactAnalysisRecord.locator)
            ).all()
            symbols: dict[UUID, list[SymbolRecord]] = defaultdict(list)
            for symbol in session.scalars(
                select(SymbolRecord).where(SymbolRecord.structural_analysis_id == analysis_id)
            ):
                symbols[symbol.artifact_analysis_id].append(symbol)
            artifacts: list[ArtifactInput] = []
            for analysis, artifact in rows:
                owned = sorted(symbols.get(analysis.id, []), key=lambda s: s.ordinal)
                if analysis.language == "Python":
                    tuples: tuple[tuple[Any, ...], ...] = tuple(
                        (s.id, s.start_line, s.start_column or 0, s.name) for s in owned
                    )
                else:
                    tuples = tuple(
                        (s.id, s.kind, s.start_offset, s.end_offset)
                        for s in owned
                        if s.start_offset is not None and s.end_offset is not None
                    )
                artifacts.append(
                    ArtifactInput(
                        analysis.id,
                        analysis.artifact_id,
                        analysis.locator,
                        analysis.language or artifact.language,
                        analysis.outcome,
                        analysis.input_fingerprint,
                        artifact.content_digest
                        if artifact.fingerprint_status == "fingerprinted"
                        else None,
                        artifact.size_bytes,
                        tuples,
                        {
                            s.id: OwnerKey(
                                s.stable_identity_id, (s.start_line, s.start_column, s.start_offset)
                            )
                            for s in owned
                        },
                    )
                )
            return StructuralInput(analysis_id, int(revision or 0), tuple(artifacts))

    def version_files(self, project_id: UUID, snapshot_id: UUID) -> list[VersionFileInput]:
        with self._session_factory() as session:
            records = session.scalars(
                select(ArtifactRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ArtifactRecord.project_version_id,
                )
                .where(
                    ArtifactRecord.project_version_id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                    ArtifactRecord.kind == "file",
                    ArtifactRecord.inventory_status == "included",
                    ArtifactRecord.pruned.is_not(True),
                    ArtifactRecord.exclusion_reasons == [],
                )
                .order_by(ArtifactRecord.locator)
            ).all()
            return [
                VersionFileInput(
                    record.id,
                    record.locator,
                    record.size_bytes,
                    record.content_digest if record.fingerprint_status == "fingerprinted" else None,
                )
                for record in records
                if is_version_file(record.locator)
            ]

    def reusable(
        self, project_id: UUID, input_fingerprint: str, contract_digest: str
    ) -> ReuseSource | None:
        with self._session_factory() as session:
            found = session.execute(
                select(FlowArtifactCoverageRecord)
                .join(
                    FlowEvidenceRunRecord,
                    FlowEvidenceRunRecord.id == FlowArtifactCoverageRecord.flow_evidence_run_id,
                )
                .where(
                    FlowEvidenceRunRecord.project_id == project_id,
                    FlowEvidenceRunRecord.outcome != "failed",
                    FlowArtifactCoverageRecord.input_fingerprint == input_fingerprint,
                    FlowArtifactCoverageRecord.contract_digest == contract_digest,
                    FlowArtifactCoverageRecord.state.in_(["extracted", "partial"]),
                )
                .order_by(FlowEvidenceRunRecord.completed_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if found is None or found.extractor is None or found.evidence_digest is None:
                return None
            contract = flow_pass_contract(found.extractor)
            if contract is None or digest(contract) != contract_digest:
                return None
            scopes = self._scopes(session, found.flow_evidence_run_id, found.artifact_analysis_id)
            texts = self._texts(session, found.flow_evidence_run_id, found.artifact_analysis_id)
            extraction = FlowExtraction(
                found.extractor,
                contract,
                contract_digest,
                "extracted" if found.state == "extracted" else "partial",
                input_fingerprint,
                tuple(
                    ParseDiagnostic(
                        str(d.get("code")),
                        "",
                        "error" if d.get("severity") == "error" else "warning",
                    )
                    for d in found.diagnostics
                ),
                tuple(scopes),
                tuple(texts),
            )
            # Reuse only rows that still reproduce the digest they were published with.
            if extraction.evidence_digest != found.evidence_digest:
                return None
            owner_ids = [s.owner_symbol_id for s in scopes if s.owner_symbol_id is not None]
            owners: dict[UUID, OwnerKey] = {}
            if owner_ids:
                for symbol in session.scalars(
                    select(SymbolRecord).where(SymbolRecord.id.in_(owner_ids))
                ):
                    owners[symbol.id] = OwnerKey(
                        symbol.stable_identity_id,
                        (symbol.start_line, symbol.start_column, symbol.start_offset),
                    )
            return ReuseSource(
                found.flow_evidence_run_id, found.artifact_analysis_id, extraction, owners
            )

    # ----------------------------------------------------------------- runs

    def _query(self, project_id: UUID, snapshot_id: UUID) -> Any:
        return select(FlowEvidenceRunRecord).where(
            FlowEvidenceRunRecord.project_id == project_id,
            FlowEvidenceRunRecord.project_version_id == snapshot_id,
        )

    def latest(self, project_id: UUID, snapshot_id: UUID) -> FlowEvidenceRun | None:
        with self._session_factory() as session:
            record = session.scalar(
                self._query(project_id, snapshot_id)
                .order_by(FlowEvidenceRunRecord.revision.desc())
                .limit(1)
            )
            return _run(record) if record else None

    def active(self, project_id: UUID, snapshot_id: UUID) -> FlowEvidenceRun | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(FlowEvidenceRunRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.active_flow_evidence_run_id == FlowEvidenceRunRecord.id,
                )
                .where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                )
            )
            return _run(record) if record else None

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> FlowEvidenceRun | None:
        with self._session_factory() as session:
            record = session.scalar(
                self._query(project_id, snapshot_id).where(FlowEvidenceRunRecord.id == run_id)
            )
            return _run(record) if record else None

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[FlowEvidenceRun], int]:
        with self._session_factory() as session:
            total = session.scalar(
                select(func.count())
                .select_from(FlowEvidenceRunRecord)
                .where(
                    FlowEvidenceRunRecord.project_id == project_id,
                    FlowEvidenceRunRecord.project_version_id == snapshot_id,
                )
            )
            records = session.scalars(
                self._query(project_id, snapshot_id)
                .order_by(FlowEvidenceRunRecord.revision.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return [_run(r) for r in records], int(total or 0)

    # ----------------------------------------------------------------- publication

    def _lock(self, session: Session, run: FlowEvidenceRun) -> tuple[ProjectVersionRecord, int]:
        version = session.scalar(
            select(ProjectVersionRecord)
            .where(
                ProjectVersionRecord.id == run.project_version_id,
                ProjectVersionRecord.project_id == run.project_id,
            )
            .with_for_update()
        )
        if version is None:
            raise FlowEvidenceConflictError("Snapshot disappeared before publication.")
        latest = session.scalar(
            select(func.max(FlowEvidenceRunRecord.revision)).where(
                FlowEvidenceRunRecord.project_version_id == run.project_version_id
            )
        )
        return version, int(latest or 0)

    @staticmethod
    def _record(run: FlowEvidenceRun, revision: int, superseded: bool) -> FlowEvidenceRunRecord:
        return FlowEvidenceRunRecord(
            id=run.id,
            project_id=run.project_id,
            project_version_id=run.project_version_id,
            revision=revision,
            started_at=run.started_at,
            completed_at=run.completed_at,
            outcome=run.outcome,
            structural_analysis_id=run.structural_analysis_id,
            structural_revision=run.structural_revision,
            contract=run.contract,
            contract_digest=digest(run.contract),
            input_authority_digest=run.input_authority_digest,
            evidence_digest=run.evidence_digest,
            coverage=run.coverage,
            diagnostics=list(run.diagnostics),
            failure_code=run.failure_code,
            superseded=superseded,
            captured_active_flow_evidence_run_id=run.captured_active_run_id,
            captured_latest_revision=run.captured_latest_revision,
        )

    def publish(self, run: FlowEvidenceRun, payload: FlowEvidencePayload) -> FlowEvidenceRun:
        with self._session_factory.begin() as session:
            version, latest = self._lock(session, run)
            structure = session.get(StructuralAnalysisRecord, run.structural_analysis_id)
            if (
                structure is None
                or structure.quarantined
                or version.active_analysis_id != run.structural_analysis_id
                or structure.revision != run.structural_revision
            ):
                raise FlowEvidenceConflictError("Flow evidence input authority changed.")
            superseded = (
                latest != run.captured_latest_revision
                or version.active_flow_evidence_run_id != run.captured_active_run_id
            )
            record = self._record(run, latest + 1, superseded)
            session.add(record)
            session.flush()
            self._insert(session, run, payload)
            if not superseded and run.outcome != "failed":
                version.active_flow_evidence_run_id = run.id
            return _run(record)

    def save_failure(self, run: FlowEvidenceRun, code: str) -> FlowEvidenceRun:
        with self._session_factory.begin() as session:
            _, latest = self._lock(session, run)
            failed = replace(
                run, outcome="failed", evidence_digest=None, diagnostics=(code,), failure_code=code
            )
            record = self._record(failed, latest + 1, False)
            session.add(record)
            session.flush()
            return _run(record)

    @staticmethod
    def _rows(session: Session, model: Any, rows: list[dict[str, Any]]) -> None:
        for start in range(0, len(rows), _BATCH):
            session.execute(insert(model), rows[start : start + _BATCH])

    def _insert(self, session: Session, run: FlowEvidenceRun, payload: FlowEvidencePayload) -> None:
        run_id = run.id
        self._rows(
            session,
            FlowArtifactCoverageRecord,
            [
                {
                    "flow_evidence_run_id": run_id,
                    "structural_analysis_id": run.structural_analysis_id,
                    "artifact_analysis_id": c.artifact_analysis_id,
                    "artifact_id": c.artifact_id,
                    "locator": c.locator,
                    "language": c.language,
                    "state": c.state,
                    "extractor": c.extractor,
                    "contract_digest": c.contract_digest,
                    "input_fingerprint": c.input_fingerprint,
                    "execution_mode": c.execution_mode,
                    "reuse_source_run_id": c.reuse_source_run_id,
                    "evidence_digest": c.evidence_digest,
                    "scope_count": c.scope_count,
                    "callsite_count": c.callsite_count,
                    "diagnostics": list(c.diagnostics),
                }
                for c in payload.coverage
            ],
        )
        scopes: list[dict[str, Any]] = []
        callsites: list[dict[str, Any]] = []
        jsx: list[dict[str, Any]] = []
        texts: list[dict[str, Any]] = []
        for artifact_analysis_id, extraction in payload.extractions.items():
            for scope in extraction.scopes:
                key = {
                    "flow_evidence_run_id": run_id,
                    "artifact_analysis_id": artifact_analysis_id,
                    "scope_key": scope.key,
                }
                scopes.append(
                    {
                        **key,
                        "parent_scope_key": scope.parent_key,
                        "owner_symbol_id": scope.owner_symbol_id,
                        "kind": scope.kind,
                        "callable_kind": scope.callable_kind,
                        "name": scope.name,
                        **_span_values(scope.span, scope.start_offset, scope.end_offset),
                        "truncated": scope.truncated,
                        "cfg": scope.cfg,
                        "summary": scope.summary,
                        "scope_digest": scope.digest,
                    }
                )
                for c in scope.callsites:
                    callsites.append(
                        {
                            **key,
                            "ordinal": c.ordinal,
                            "cfg_node": c.cfg_node,
                            "copies": list(c.copies),
                            "callee": c.callee,
                            "arguments": list(c.arguments),
                            "awaited": c.awaited,
                            **_span_values(c.span, c.start_offset, c.end_offset),
                            "text_key": c.text_key,
                        }
                    )
                for j in scope.jsx:
                    jsx.append(
                        {
                            **key,
                            "ordinal": j.ordinal,
                            "cfg_node": j.cfg_node,
                            "element": j.element,
                            "intrinsic": j.intrinsic,
                            "attribute": j.attribute,
                            "value": j.value,
                            **_span_values(j.span, j.start_offset, j.end_offset),
                        }
                    )
            seen: set[str] = set()
            for text in extraction.display_texts:
                if text.key in seen:
                    continue
                seen.add(text.key)
                texts.append(
                    {
                        "flow_evidence_run_id": run_id,
                        "artifact_analysis_id": artifact_analysis_id,
                        "text_key": text.key,
                        "text": text.text,
                        "flags": list(text.flags),
                    }
                )
        self._rows(session, FlowScopeRecord, scopes)
        self._rows(session, FlowCallsiteRecord, callsites)
        self._rows(session, FlowJsxBindingRecord, jsx)
        self._rows(session, FlowDisplayTextRecord, texts)
        self._rows(
            session,
            FlowFrameworkFactRecord,
            [
                {
                    "flow_evidence_run_id": run_id,
                    "ordinal": ordinal,
                    "ecosystem": f.ecosystem,
                    "package": f.package,
                    "version": f.version,
                    "version_constraint": f.constraint,
                    "source_kind": f.source_kind,
                    "locator": f.locator,
                    "scope": f.scope,
                    "input_fingerprint": f.input_fingerprint,
                }
                for ordinal, f in enumerate(payload.facts)
            ],
        )

    # ----------------------------------------------------------------- reads

    def coverage_page(
        self, run_id: UUID, limit: int, offset: int, state: str | None
    ) -> tuple[list[ArtifactFlowCoverage], int]:
        with self._session_factory() as session:
            query = select(FlowArtifactCoverageRecord).where(
                FlowArtifactCoverageRecord.flow_evidence_run_id == run_id
            )
            if state is not None:
                query = query.where(FlowArtifactCoverageRecord.state == state)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            records = session.scalars(
                query.order_by(FlowArtifactCoverageRecord.locator).limit(limit).offset(offset)
            ).all()
            return [_coverage(r) for r in records], int(total or 0)

    def _scopes(
        self, session: Session, run_id: UUID, artifact_analysis_id: UUID
    ) -> list[FlowScope]:
        where = (
            FlowScopeRecord.flow_evidence_run_id == run_id,
            FlowScopeRecord.artifact_analysis_id == artifact_analysis_id,
        )
        callsites: dict[str, list[FlowCallsite]] = defaultdict(list)
        for c in session.scalars(
            select(FlowCallsiteRecord)
            .where(
                FlowCallsiteRecord.flow_evidence_run_id == run_id,
                FlowCallsiteRecord.artifact_analysis_id == artifact_analysis_id,
            )
            .order_by(FlowCallsiteRecord.scope_key, FlowCallsiteRecord.ordinal)
        ):
            callsites[c.scope_key].append(
                FlowCallsite(
                    c.ordinal,
                    c.cfg_node,
                    dict(c.callee),
                    tuple(c.arguments),
                    c.awaited,
                    _span(c),
                    c.start_offset,
                    c.end_offset,
                    c.text_key,
                    tuple(c.copies),
                )
            )
        jsx: dict[str, list[FlowJsxBinding]] = defaultdict(list)
        for j in session.scalars(
            select(FlowJsxBindingRecord)
            .where(
                FlowJsxBindingRecord.flow_evidence_run_id == run_id,
                FlowJsxBindingRecord.artifact_analysis_id == artifact_analysis_id,
            )
            .order_by(FlowJsxBindingRecord.scope_key, FlowJsxBindingRecord.ordinal)
        ):
            jsx[j.scope_key].append(
                FlowJsxBinding(
                    j.ordinal,
                    j.cfg_node,
                    j.element,
                    j.intrinsic,
                    j.attribute,
                    dict(j.value),
                    _span(j),
                    j.start_offset,
                    j.end_offset,
                )
            )
        records = session.scalars(select(FlowScopeRecord).where(*where)).all()
        order = {
            key: index for index, key in enumerate(_scope_order([r.scope_key for r in records]))
        }
        return [
            FlowScope(
                r.scope_key,
                r.parent_scope_key,
                r.kind,
                r.callable_kind,
                r.name,
                _span(r),
                r.start_offset,
                r.end_offset,
                r.truncated,
                dict(r.cfg),
                dict(r.summary),
                tuple(callsites.get(r.scope_key, ())),
                tuple(jsx.get(r.scope_key, ())),
                r.owner_symbol_id,
            )
            for r in sorted(records, key=lambda r: order[r.scope_key])
        ]

    def _texts(
        self, session: Session, run_id: UUID, artifact_analysis_id: UUID
    ) -> list[DisplayText]:
        records = session.scalars(
            select(FlowDisplayTextRecord).where(
                FlowDisplayTextRecord.flow_evidence_run_id == run_id,
                FlowDisplayTextRecord.artifact_analysis_id == artifact_analysis_id,
            )
        ).all()
        return sorted(
            (DisplayText(r.text_key, r.text, tuple(r.flags)) for r in records),
            key=lambda t: _text_order(t.key),
        )

    def scopes(self, run_id: UUID, artifact_analysis_id: UUID) -> list[FlowScope]:
        with self._session_factory() as session:
            return self._scopes(session, run_id, artifact_analysis_id)

    def display_texts(self, run_id: UUID, artifact_analysis_id: UUID) -> list[DisplayText]:
        with self._session_factory() as session:
            return self._texts(session, run_id, artifact_analysis_id)

    def framework_facts(self, run_id: UUID) -> list[FrameworkFact]:
        with self._session_factory() as session:
            records = session.scalars(
                select(FlowFrameworkFactRecord)
                .where(FlowFrameworkFactRecord.flow_evidence_run_id == run_id)
                .order_by(FlowFrameworkFactRecord.ordinal)
            ).all()
            return [
                FrameworkFact(
                    r.ecosystem,
                    r.package,
                    r.version,
                    r.version_constraint,
                    r.source_kind,
                    r.locator,
                    r.input_fingerprint,
                    r.scope,
                )
                for r in records
            ]


def _scope_order(keys: list[str]) -> list[str]:
    """Scope keys in extraction order: ``m``, ``m.0``, ``m.0.0`` … by numeric path."""
    return sorted(keys, key=lambda key: [int(p) if p.isdigit() else -1 for p in key.split(".")[1:]])


def _text_order(key: str) -> tuple[list[int], str]:
    scope, _, rest = key.partition("#")
    return [int(p) for p in scope.split(".")[1:] if p.isdigit()], rest
