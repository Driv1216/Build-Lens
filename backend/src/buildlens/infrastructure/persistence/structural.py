"""PostgreSQL persistence for a replaceable structural-analysis aggregate."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session, defer, sessionmaker

from buildlens.application.structural import StructuralAnalysisStore
from buildlens.domain.entities import Artifact, Project, ProjectVersion
from buildlens.domain.extractors import (
    LANGUAGES,
    PYTHON_IDENTITY_SCHEME,
    TS_IDENTITY_SCHEME,
    current_declaration_digests,
    extractor_contract_digest,
)
from buildlens.domain.source import structural_contract
from buildlens.domain.structural import (
    ArchitectureLiteral,
    ArtifactAnalysis,
    ArtifactExtraction,
    DecoratorObservation,
    ExportReference,
    ImportReference,
    ObservedExpression,
    Parameter,
    ParseDiagnostic,
    Signature,
    SourceSpan,
    StructuralAnalysis,
    Symbol,
    SymbolFacts,
    SymbolReference,
)
from buildlens.infrastructure.persistence.models import (
    ArchitectureLiteralRecord,
    ArtifactAnalysisRecord,
    ArtifactExtractionRecord,
    ArtifactRecord,
    ExportReferenceRecord,
    ImportReferenceRecord,
    ProjectRecord,
    ProjectVersionRecord,
    StructuralAnalysisRecord,
    SymbolFactsRecord,
    SymbolIdentityRecord,
    SymbolRecord,
    SymbolReferenceRecord,
)
from buildlens.infrastructure.persistence.repositories import _to_domain
from buildlens.infrastructure.persistence.snapshots import _artifact, _version


def _span(value: SourceSpan | None) -> dict[str, int | None] | None:
    return (
        None
        if value is None
        else {
            "start_line": value.start_line,
            "end_line": value.end_line,
            "start_column": value.start_column,
            "end_column": value.end_column,
        }
    )


def _span_from(value: dict[str, Any] | None) -> SourceSpan | None:
    return (
        None
        if not value
        else SourceSpan(
            value.get("start_line"),
            value.get("end_line"),
            value.get("start_column"),
            value.get("end_column"),
        )
    )


def _expression(value: ObservedExpression | None) -> dict[str, object] | None:
    return (
        None
        if value is None
        else {
            "text": value.text,
            "span": _span(value.span),
            "omitted": value.omitted,
            "truncated": value.truncated,
        }
    )


def _expression_from(value: dict[str, Any] | None) -> ObservedExpression | None:
    return (
        None
        if value is None
        else ObservedExpression(
            cast(str | None, value.get("text")),
            _span_from(cast(dict[str, Any] | None, value.get("span"))),
            bool(value.get("omitted")),
            bool(value.get("truncated")),
        )
    )


def _diagnostic(value: ParseDiagnostic) -> dict[str, object]:
    return {
        "code": value.code,
        "message": value.message,
        "severity": value.severity,
        "span": _span(value.span),
    }


def _diagnostic_from(value: dict[str, Any]) -> ParseDiagnostic:
    return ParseDiagnostic(
        cast(str, value["code"]),
        cast(str, value["message"]),
        cast(Any, value["severity"]),
        _span_from(cast(dict[str, Any] | None, value.get("span"))),
    )


def _signature(value: Signature | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "parameters": [
            {
                "name": item.name,
                "kind": item.kind,
                "has_default": item.has_default,
                "default": _expression(item.default),
                "annotation": _expression(item.annotation),
                "span": _span(item.span),
            }
            for item in value.parameters
        ],
        "return_annotation": _expression(value.return_annotation),
    }


def _signature_from(value: dict[str, Any] | None) -> Signature | None:
    if value is None:
        return None
    return Signature(
        tuple(
            Parameter(
                cast(str, item["name"]),
                cast(Any, item["kind"]),
                bool(item["has_default"]),
                _expression_from(cast(dict[str, Any] | None, item.get("default"))),
                _expression_from(cast(dict[str, Any] | None, item.get("annotation"))),
                _span_from(cast(dict[str, Any] | None, item.get("span"))),
            )
            for item in cast(list[dict[str, Any]], value["parameters"])
        ),
        _expression_from(cast(dict[str, Any] | None, value.get("return_annotation"))),
    )


def _decorator(value: DecoratorObservation) -> dict[str, object]:
    return {
        "ordinal": value.ordinal,
        "form": value.form,
        "expression": _expression(value.expression),
        "span": _span(value.span),
    }


def _decorator_from(value: dict[str, Any]) -> DecoratorObservation:
    expression = _expression_from(cast(dict[str, Any] | None, value.get("expression")))
    assert expression is not None
    return DecoratorObservation(
        cast(int, value["ordinal"]),
        cast(str, value["form"]),
        expression,
        _span_from(cast(dict[str, Any] | None, value.get("span"))),
    )


def _current_declarations() -> ColumnElement[bool]:
    """Invariant M4: non-Python evidence is current only under its own current contract."""
    return or_(
        ArtifactAnalysisRecord.language.is_(None),
        ArtifactAnalysisRecord.language == "Python",
        ArtifactAnalysisRecord.declaration_extraction_id.in_(
            select(ArtifactExtractionRecord.id).where(
                ArtifactExtractionRecord.contract_digest.in_(
                    list(current_declaration_digests().values())
                )
            )
        ),
    )


def _extraction(
    record: ArtifactExtractionRecord,
    facts: Sequence[SymbolFactsRecord],
    references: Sequence[SymbolReferenceRecord],
    exports: Sequence[ExportReferenceRecord],
    literals: Sequence[ArchitectureLiteralRecord] = (),
) -> ArtifactExtraction:
    return ArtifactExtraction(
        record.id,
        record.extractor,
        cast(dict[str, str], record.contract),
        record.contract_digest,
        cast(Any, record.outcome),
        record.input_fingerprint,
        tuple(_diagnostic_from(cast(dict[str, Any], x)) for x in record.diagnostics),
        cast(Any, record.execution_mode),
        record.reuse_source_extraction_id,
        cast(dict[str, object] | None, record.artifact_facts),
        tuple(SymbolFacts(item.symbol_id, cast(dict[str, object], item.facts)) for item in facts),
        tuple(
            SymbolReference(
                item.ordinal,
                item.owner_symbol_id,
                item.kind,
                item.target,
                SourceSpan(item.start_line, item.end_line, item.start_column, item.end_column),
            )
            for item in references
        ),
        tuple(
            ExportReference(
                item.ordinal,
                item.owner_symbol_id,
                item.kind,
                item.name,
                item.local_name,
                item.source_module,
                SourceSpan(item.start_line, item.end_line, item.start_column, item.end_column),
            )
            for item in exports
        ),
        tuple(
            ArchitectureLiteral(
                item.ordinal,
                item.kind,
                SourceSpan(item.start_line, item.end_line, item.start_column, item.end_column),
                item.subject,
                cast(dict[str, object], item.attributes),
            )
            for item in literals
        ),
    )


class SqlAlchemyStructuralAnalysisStore(StructuralAnalysisStore):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_project(self, project_id: UUID) -> Project | None:
        with self._session_factory() as session:
            record = session.get(ProjectRecord, project_id)
            return _to_domain(record) if record else None

    def get_snapshot(self, project_id: UUID, snapshot_id: UUID) -> ProjectVersion | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(ProjectVersionRecord).where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                    ProjectVersionRecord.scan_completed_at.is_not(None),
                )
            )
            return _version(record) if record else None

    def candidate_artifacts(self, project_id: UUID, snapshot_id: UUID) -> list[Artifact]:
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
                    ArtifactRecord.language.in_(list(LANGUAGES)),
                    ArtifactRecord.pruned.is_not(True),
                    ArtifactRecord.exclusion_reasons == [],
                )
                .order_by(ArtifactRecord.locator)
            ).all()
            return [_artifact(record) for record in records]

    def latest_analysis(self, project_id: UUID, snapshot_id: UUID) -> StructuralAnalysis | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(StructuralAnalysisRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == StructuralAnalysisRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                    StructuralAnalysisRecord.id == ProjectVersionRecord.active_analysis_id,
                    StructuralAnalysisRecord.quarantined.is_(False),
                    StructuralAnalysisRecord.producer_contract == structural_contract(),
                )
            )
            return self._analysis(session, record) if record else None

    def latest_attempt(self, project_id: UUID, snapshot_id: UUID) -> StructuralAnalysis | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(StructuralAnalysisRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == StructuralAnalysisRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProjectVersionRecord.id == snapshot_id,
                )
                .order_by(StructuralAnalysisRecord.revision.desc())
                .limit(1)
            )
            return self._analysis(session, record) if record else None

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> StructuralAnalysis | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(StructuralAnalysisRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == StructuralAnalysisRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProjectVersionRecord.id == snapshot_id,
                    StructuralAnalysisRecord.id == run_id,
                )
            )
            return self._analysis(session, record) if record else None

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[StructuralAnalysis], int]:
        with self._session_factory() as session:
            base = (
                select(StructuralAnalysisRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == StructuralAnalysisRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProjectVersionRecord.id == snapshot_id,
                )
            )
            total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
            records = session.scalars(
                base.order_by(StructuralAnalysisRecord.revision.desc()).limit(limit).offset(offset)
            ).all()
            return [self._analysis(session, item) for item in records], total

    def _artifact_summary(
        self, session: Session, record: ArtifactAnalysisRecord
    ) -> ArtifactAnalysis:
        return ArtifactAnalysis(
            record.id,
            record.structural_analysis_id,
            record.project_version_id,
            record.artifact_id,
            record.locator,
            cast(Any, record.outcome),
            record.read_started_at,
            record.read_completed_at,
            record.encoding,
            record.line_count,
            record.is_package_initializer,
            tuple(_diagnostic_from(x) for x in record.diagnostics),
            input_fingerprint=record.input_fingerprint,
            snapshot_verified=record.snapshot_verified,
            execution_mode=cast(Any, record.execution_mode),
            reuse_source_artifact_analysis_id=record.reuse_source_artifact_analysis_id,
            language=record.language,
            declaration_extraction_id=record.declaration_extraction_id,
        )

    def baseline_artifact(
        self, project_id: UUID, snapshot_id: UUID, artifact_id: UUID
    ) -> ArtifactAnalysis | None:
        with self._session_factory() as session:
            current = session.get(ArtifactRecord, artifact_id)
            version = session.get(ProjectVersionRecord, snapshot_id)
            if (
                current is None
                or version is None
                or version.project_id != project_id
                or current.project_version_id != snapshot_id
            ):
                return None
            candidates = [version.active_analysis_id]
            if version.predecessor_id:
                predecessor = session.get(ProjectVersionRecord, version.predecessor_id)
                if predecessor:
                    candidates.append(predecessor.active_analysis_id)
            for analysis_id in candidates:
                analysis = (
                    session.get(StructuralAnalysisRecord, analysis_id) if analysis_id else None
                )
                if (
                    not analysis
                    or analysis.quarantined
                    or analysis.producer_contract != structural_contract()
                ):
                    continue
                source = session.scalar(
                    select(ArtifactAnalysisRecord)
                    .join(ArtifactRecord, ArtifactRecord.id == ArtifactAnalysisRecord.artifact_id)
                    .where(
                        ArtifactAnalysisRecord.structural_analysis_id == analysis_id,
                        ArtifactRecord.stable_identity_id == current.stable_identity_id,
                        ArtifactAnalysisRecord.snapshot_verified.is_(True),
                        ArtifactAnalysisRecord.outcome == "parsed",
                    )
                )
                if source:
                    return self._artifact_analysis(session, source)
            return None

    def reusable_artifact(
        self, project_id: UUID, snapshot_id: UUID, artifact_id: UUID
    ) -> ArtifactAnalysis | None:
        with self._session_factory() as session:
            current = session.get(ArtifactRecord, artifact_id)
            version = session.get(ProjectVersionRecord, snapshot_id)
            if (
                current is None
                or version is None
                or version.project_id != project_id
                or current.project_version_id != snapshot_id
                or version.legacy_unverified
                or current.fingerprint_status != "fingerprinted"
                or not current.content_digest
            ):
                return None
            candidates: list[UUID] = []
            if version.active_analysis_id:
                candidates.append(version.active_analysis_id)
            if version.predecessor_id:
                predecessor = session.get(ProjectVersionRecord, version.predecessor_id)
                if predecessor and predecessor.active_analysis_id:
                    candidates.append(predecessor.active_analysis_id)
            for analysis_id in candidates:
                producer = session.get(StructuralAnalysisRecord, analysis_id)
                if (
                    not producer
                    or producer.quarantined
                    or producer.producer_contract != structural_contract()
                ):
                    continue
                source = session.scalar(
                    select(ArtifactAnalysisRecord)
                    .join(ArtifactRecord, ArtifactRecord.id == ArtifactAnalysisRecord.artifact_id)
                    .where(
                        ArtifactAnalysisRecord.structural_analysis_id == analysis_id,
                        ArtifactAnalysisRecord.snapshot_verified.is_(True),
                        ArtifactAnalysisRecord.outcome == "parsed",
                        ArtifactAnalysisRecord.input_fingerprint == current.content_digest,
                        ArtifactRecord.stable_identity_id == current.stable_identity_id,
                    )
                )
                if source is None:
                    continue
                candidate = self._artifact_analysis(session, source)
                if current.language in LANGUAGES and current.language != "Python":
                    declaration = candidate.declaration_extraction
                    if (
                        declaration is None
                        or declaration.outcome not in {"extracted", "partial"}
                        or declaration.contract_digest
                        != extractor_contract_digest(declaration.extractor)
                    ):
                        continue
                return candidate
            return None

    def save_replacement(
        self, project_id: UUID, snapshot_id: UUID, analysis: StructuralAnalysis
    ) -> StructuralAnalysis:
        with self._session_factory.begin() as session:
            version = session.scalar(
                select(ProjectVersionRecord)
                .where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                )
                .with_for_update()
            )
            if version is None:
                raise RuntimeError("Snapshot disappeared before persistence.")
            latest_revision = (
                session.scalar(
                    select(func.max(StructuralAnalysisRecord.revision)).where(
                        StructuralAnalysisRecord.project_version_id == snapshot_id
                    )
                )
                or 0
            )
            predecessor = (
                session.get(ProjectVersionRecord, version.predecessor_id)
                if version.predecessor_id
                else None
            )
            superseded = (
                latest_revision != analysis.captured_latest_revision
                or version.active_analysis_id != analysis.captured_active_analysis_id
                or (predecessor.active_analysis_id if predecessor else None)
                != analysis.captured_predecessor_analysis_id
            )
            revision = latest_revision + 1
            session.add(
                StructuralAnalysisRecord(
                    id=analysis.id,
                    project_version_id=analysis.project_version_id,
                    started_at=analysis.started_at,
                    completed_at=analysis.completed_at,
                    outcome=analysis.outcome,
                    parser_version=analysis.parser_version,
                    runtime_version=analysis.runtime_version,
                    policy_version=analysis.policy_version,
                    limits=analysis.limits,
                    summary=analysis.summary,
                    warnings=[_diagnostic(item) for item in analysis.warnings],
                    revision=revision,
                    captured_predecessor_analysis_id=analysis.captured_predecessor_analysis_id,
                    producer_contract=analysis.producer_contract,
                    quarantined=False,
                    superseded=superseded,
                    captured_active_analysis_id=analysis.captured_active_analysis_id,
                    captured_latest_revision=analysis.captured_latest_revision,
                )
            )
            session.flush()
            for artifact in analysis.artifact_analyses:
                session.add(
                    ArtifactAnalysisRecord(
                        artifact_identity_id=session.scalar(
                            select(ArtifactRecord.stable_identity_id).where(
                                ArtifactRecord.id == artifact.artifact_id
                            )
                        ),
                        id=artifact.id,
                        structural_analysis_id=analysis.id,
                        project_version_id=artifact.project_version_id,
                        artifact_id=artifact.artifact_id,
                        locator=artifact.locator,
                        outcome=artifact.outcome,
                        read_started_at=artifact.read_started_at,
                        read_completed_at=artifact.read_completed_at,
                        encoding=artifact.encoding,
                        line_count=artifact.line_count,
                        is_package_initializer=artifact.is_package_initializer,
                        diagnostics=[_diagnostic(item) for item in artifact.diagnostics],
                        input_fingerprint=artifact.input_fingerprint,
                        snapshot_verified=artifact.snapshot_verified,
                        execution_mode=artifact.execution_mode,
                        baseline_artifact_analysis_id=artifact.baseline_artifact_analysis_id,
                        reuse_source_artifact_analysis_id=artifact.reuse_source_artifact_analysis_id,
                        language=artifact.language,
                        declaration_extraction_id=artifact.declaration_extraction_id,
                    )
                )
                session.flush()
                typescript_slot = artifact.language not in {None, "Python"}
                source_symbols: Sequence[SymbolRecord] = []
                if artifact.baseline_artifact_analysis_id:
                    source_symbols = session.scalars(
                        select(SymbolRecord).where(
                            SymbolRecord.artifact_analysis_id
                            == artifact.baseline_artifact_analysis_id
                        )
                    ).all()
                source_by_parent: dict[tuple[UUID | None, str, str], list[SymbolRecord]] = {}
                for candidate in source_symbols:
                    parent = (
                        session.get(SymbolRecord, candidate.parent_id)
                        if candidate.parent_id
                        else None
                    )
                    source_parent = parent.stable_identity_id if parent else None
                    source_by_parent.setdefault(
                        (source_parent, candidate.kind, candidate.name), []
                    ).append(candidate)
                new_group_counts: dict[tuple[UUID | None, str, str], int] = {}
                parent_stable: dict[UUID, UUID] = {}
                for symbol in artifact.symbols:
                    key = (symbol.parent_id, symbol.kind, symbol.name)
                    new_group_counts[key] = new_group_counts.get(key, 0) + 1
                for symbol in artifact.symbols:
                    span = symbol.declaration_span
                    parent_identity = (
                        parent_stable.get(symbol.parent_id) if symbol.parent_id else None
                    )
                    count_key = (symbol.parent_id, symbol.kind, symbol.name)
                    source_key = (parent_identity, symbol.kind, symbol.name)
                    candidates = source_by_parent.get(source_key, [])
                    identity_id = (
                        symbol.stable_identity_id if artifact.execution_mode == "reused" else None
                    ) or (
                        candidates[0].stable_identity_id
                        if len(candidates) == 1
                        and new_group_counts[count_key] == 1
                        and candidates[0].stable_identity_id
                        else None
                    )
                    if identity_id is None:
                        stable_artifact = session.scalar(
                            select(ArtifactRecord.stable_identity_id).where(
                                ArtifactRecord.id == artifact.artifact_id
                            )
                        )
                        if stable_artifact:
                            identity_id = uuid4()
                            session.add(
                                SymbolIdentityRecord(
                                    id=identity_id,
                                    project_id=project_id,
                                    artifact_identity_id=stable_artifact,
                                    parent_identity_id=parent_identity,
                                    kind=symbol.kind,
                                    name=symbol.name,
                                    scheme=TS_IDENTITY_SCHEME
                                    if typescript_slot
                                    else PYTHON_IDENTITY_SCHEME,
                                    declaration_space=symbol.declaration_space,
                                )
                            )
                    session.flush()
                    if identity_id:
                        parent_stable[symbol.id] = identity_id
                    session.add(
                        SymbolRecord(
                            artifact_identity_id=session.scalar(
                                select(ArtifactRecord.stable_identity_id).where(
                                    ArtifactRecord.id == artifact.artifact_id
                                )
                            ),
                            id=symbol.id,
                            artifact_analysis_id=artifact.id,
                            structural_analysis_id=analysis.id,
                            parent_id=symbol.parent_id,
                            ordinal=symbol.ordinal,
                            kind=symbol.kind,
                            name=symbol.name,
                            qualified_name=symbol.qualified_name,
                            is_async=symbol.is_async,
                            start_line=span.start_line or 0,
                            end_line=span.end_line or 0,
                            start_column=span.start_column,
                            end_column=span.end_column,
                            full_start_line=symbol.full_start_line,
                            signature=_signature(symbol.signature),
                            decorators=[_decorator(item) for item in symbol.decorators],
                            type_parameters=[
                                cast(dict[str, object], _expression(item))
                                for item in symbol.type_parameters
                            ],
                            stable_identity_id=identity_id,
                            continuity_reason=(
                                "continued_unique_slot"
                                if identity_id
                                and len(candidates) == 1
                                and new_group_counts[count_key] == 1
                                else "new_or_ambiguous_slot"
                            ),
                            state_fingerprint=symbol.state_fingerprint,
                            declaration_form=symbol.declaration_form,
                            declaration_space=symbol.declaration_space,
                            name_source=symbol.name_source,
                            exported=symbol.exported,
                            start_offset=symbol.start_offset,
                            end_offset=symbol.end_offset,
                            additional_spans=[list(item) for item in symbol.additional_spans]
                            if symbol.additional_spans
                            else None,
                        )
                    )
                    session.flush()
                for item in artifact.imports:
                    span = item.span
                    session.add(
                        ImportReferenceRecord(
                            id=item.id,
                            artifact_analysis_id=artifact.id,
                            structural_analysis_id=analysis.id,
                            owner_symbol_id=item.owner_symbol_id,
                            statement_ordinal=item.statement_ordinal,
                            item_ordinal=item.item_ordinal,
                            form=item.form,
                            from_module=item.from_module,
                            name=item.name,
                            alias=item.alias,
                            level=item.level,
                            start_line=span.start_line or 0,
                            end_line=span.end_line or 0,
                            start_column=span.start_column,
                            end_column=span.end_column,
                            resolution_status=item.resolution_status,
                            resolution_reason=item.resolution_reason,
                        )
                    )
                session.flush()
                for extraction in artifact.extractions:
                    self._save_extraction(session, analysis.id, artifact.id, extraction)
            if not superseded and analysis.outcome in {"complete", "partial"}:
                version.active_analysis_id = analysis.id
            session.flush()
            record = session.get(StructuralAnalysisRecord, analysis.id)
            assert record is not None
            return self._analysis(session, record, True)

    @staticmethod
    def _save_extraction(
        session: Session, analysis_id: UUID, artifact_analysis_id: UUID, value: ArtifactExtraction
    ) -> None:
        session.add(
            ArtifactExtractionRecord(
                id=value.id,
                artifact_analysis_id=artifact_analysis_id,
                structural_analysis_id=analysis_id,
                extractor=value.extractor,
                contract=value.contract,
                contract_digest=value.contract_digest,
                outcome=value.outcome,
                execution_mode=value.execution_mode,
                reuse_source_extraction_id=value.reuse_source_extraction_id,
                input_fingerprint=value.input_fingerprint,
                diagnostics=[_diagnostic(item) for item in value.diagnostics],
                artifact_facts=value.artifact_facts,
            )
        )
        session.flush()
        for fact in value.symbol_facts:
            session.add(
                SymbolFactsRecord(
                    id=uuid4(),
                    extraction_id=value.id,
                    artifact_analysis_id=artifact_analysis_id,
                    symbol_id=fact.symbol_id,
                    facts=fact.facts,
                )
            )
        for reference in value.references:
            span = reference.span
            session.add(
                SymbolReferenceRecord(
                    id=uuid4(),
                    extraction_id=value.id,
                    artifact_analysis_id=artifact_analysis_id,
                    owner_symbol_id=reference.owner_symbol_id,
                    ordinal=reference.ordinal,
                    kind=reference.kind,
                    target=reference.target,
                    start_line=span.start_line or 0,
                    end_line=span.end_line or 0,
                    start_column=span.start_column,
                    end_column=span.end_column,
                )
            )
        for export in value.exports:
            span = export.span
            session.add(
                ExportReferenceRecord(
                    id=uuid4(),
                    extraction_id=value.id,
                    artifact_analysis_id=artifact_analysis_id,
                    owner_symbol_id=export.owner_symbol_id,
                    ordinal=export.ordinal,
                    kind=export.kind,
                    name=export.name,
                    local_name=export.local_name,
                    source_module=export.source_module,
                    start_line=span.start_line or 0,
                    end_line=span.end_line or 0,
                    start_column=span.start_column,
                    end_column=span.end_column,
                )
            )
        for literal in value.literals:
            span = literal.span
            session.add(
                ArchitectureLiteralRecord(
                    id=uuid4(),
                    extraction_id=value.id,
                    artifact_analysis_id=artifact_analysis_id,
                    ordinal=literal.ordinal,
                    kind=literal.kind,
                    subject=literal.subject,
                    attributes=literal.attributes,
                    start_line=span.start_line or 0,
                    end_line=span.end_line or 0,
                    start_column=span.start_column,
                    end_column=span.end_column,
                )
            )
        session.flush()

    def extractions(
        self, session: Session, artifact_analysis_id: UUID
    ) -> tuple[ArtifactExtraction, ...]:
        records = session.scalars(
            select(ArtifactExtractionRecord)
            .where(ArtifactExtractionRecord.artifact_analysis_id == artifact_analysis_id)
            .order_by(ArtifactExtractionRecord.extractor)
        ).all()
        values = []
        for record in records:
            facts = session.scalars(
                select(SymbolFactsRecord).where(SymbolFactsRecord.extraction_id == record.id)
            ).all()
            references = session.scalars(
                select(SymbolReferenceRecord)
                .where(SymbolReferenceRecord.extraction_id == record.id)
                .order_by(SymbolReferenceRecord.ordinal)
            ).all()
            exports = session.scalars(
                select(ExportReferenceRecord)
                .where(ExportReferenceRecord.extraction_id == record.id)
                .order_by(ExportReferenceRecord.ordinal)
            ).all()
            literals = session.scalars(
                select(ArchitectureLiteralRecord)
                .where(ArchitectureLiteralRecord.extraction_id == record.id)
                .order_by(ArchitectureLiteralRecord.ordinal)
            ).all()
            values.append(_extraction(record, facts, references, exports, literals))
        return tuple(values)

    def artifact_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        limit: int,
        offset: int,
        outcome: str | None,
        run_id: UUID | None = None,
    ) -> tuple[list[ArtifactAnalysis], int, UUID | None]:
        with self._session_factory() as session:
            analysis_id = self._analysis_id(session, project_id, snapshot_id, run_id)
            if analysis_id is None:
                return [], 0, None
            base = select(ArtifactAnalysisRecord).where(
                ArtifactAnalysisRecord.structural_analysis_id == analysis_id
            )
            if outcome:
                base = base.where(ArtifactAnalysisRecord.outcome == outcome)
            total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
            records = session.scalars(
                base.order_by(ArtifactAnalysisRecord.locator).limit(limit).offset(offset)
            ).all()
            ids = [x.id for x in records]
            symbols = {
                key: count
                for key, count in session.execute(
                    select(SymbolRecord.artifact_analysis_id, func.count())
                    .where(SymbolRecord.artifact_analysis_id.in_(ids))
                    .group_by(SymbolRecord.artifact_analysis_id)
                )
            }
            imports = {
                key: count
                for key, count in session.execute(
                    select(ImportReferenceRecord.artifact_analysis_id, func.count())
                    .where(ImportReferenceRecord.artifact_analysis_id.in_(ids))
                    .group_by(ImportReferenceRecord.artifact_analysis_id)
                )
            }
            extractions: dict[UUID, list[ArtifactExtraction]] = {}
            for item in session.scalars(
                select(ArtifactExtractionRecord)
                .where(ArtifactExtractionRecord.artifact_analysis_id.in_(ids))
                .order_by(ArtifactExtractionRecord.extractor)
            ):
                extractions.setdefault(item.artifact_analysis_id, []).append(
                    _extraction(item, (), (), ())
                )
            return (
                [
                    replace(
                        self._artifact_summary(session, record),
                        stored_symbol_count=symbols.get(record.id, 0),
                        stored_import_count=imports.get(record.id, 0),
                        extractions=tuple(extractions.get(record.id, ())),
                    )
                    for record in records
                ],
                total,
                analysis_id,
            )

    def symbol_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        limit: int,
        offset: int,
        artifact_id: UUID | None,
        kind: str | None,
        name: str | None,
        run_id: UUID | None = None,
        current_only: bool = False,
    ) -> tuple[list[Symbol], int, UUID | None]:
        with self._session_factory() as session:
            analysis_id = self._analysis_id(session, project_id, snapshot_id, run_id)
            if analysis_id is None:
                return [], 0, None
            base = (
                select(SymbolRecord, ArtifactAnalysisRecord.locator)
                .join(
                    ArtifactAnalysisRecord,
                    ArtifactAnalysisRecord.id == SymbolRecord.artifact_analysis_id,
                )
                .where(SymbolRecord.structural_analysis_id == analysis_id)
            )
            if run_id is None or current_only:
                base = base.where(_current_declarations())
            if artifact_id:
                base = base.where(ArtifactAnalysisRecord.artifact_id == artifact_id)
            if kind:
                base = base.where(SymbolRecord.kind == kind)
            if name:
                base = base.where(SymbolRecord.name.ilike(f"%{name}%"))
            total = (
                session.scalar(
                    select(func.count()).select_from(
                        base.with_only_columns(SymbolRecord.id).subquery()
                    )
                )
                or 0
            )
            records = session.execute(
                base.options(
                    defer(SymbolRecord.signature, raiseload=True),
                    defer(SymbolRecord.decorators, raiseload=True),
                    defer(SymbolRecord.type_parameters, raiseload=True),
                )
                .order_by(
                    ArtifactAnalysisRecord.locator, SymbolRecord.start_line, SymbolRecord.ordinal
                )
                .limit(limit)
                .offset(offset)
            ).all()
            return (
                [
                    replace(self._symbol(record, include_content=False), locator=locator)
                    for record, locator in records
                ],
                total,
                analysis_id,
            )

    def symbol(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        symbol_id: UUID,
        run_id: UUID | None = None,
        current_only: bool = False,
    ) -> Symbol | None:
        with self._session_factory() as session:
            analysis_id = self._analysis_id(session, project_id, snapshot_id, run_id)
            record = (
                session.scalar(
                    select(SymbolRecord)
                    .join(
                        ArtifactAnalysisRecord,
                        ArtifactAnalysisRecord.id == SymbolRecord.artifact_analysis_id,
                    )
                    .where(
                        SymbolRecord.id == symbol_id,
                        SymbolRecord.structural_analysis_id == analysis_id,
                        _current_declarations() if run_id is None or current_only else True,
                    )
                )
                if analysis_id
                else None
            )

            return self._symbol(record) if record is not None else None

    def import_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        limit: int,
        offset: int,
        artifact_id: UUID | None,
        owner_id: UUID | None,
        run_id: UUID | None = None,
        current_only: bool = False,
    ) -> tuple[list[ImportReference], int, UUID | None]:
        with self._session_factory() as session:
            analysis_id = self._analysis_id(session, project_id, snapshot_id, run_id)
            if analysis_id is None:
                return [], 0, None
            base = (
                select(ImportReferenceRecord, ArtifactAnalysisRecord.locator)
                .join(
                    ArtifactAnalysisRecord,
                    ArtifactAnalysisRecord.id == ImportReferenceRecord.artifact_analysis_id,
                )
                .where(ImportReferenceRecord.structural_analysis_id == analysis_id)
            )
            if run_id is None or current_only:
                base = base.where(_current_declarations())
            if artifact_id:
                base = base.where(ArtifactAnalysisRecord.artifact_id == artifact_id)
            if owner_id:
                base = base.where(ImportReferenceRecord.owner_symbol_id == owner_id)
            total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
            records = session.execute(
                base.order_by(
                    ArtifactAnalysisRecord.locator,
                    ImportReferenceRecord.start_line,
                    ImportReferenceRecord.statement_ordinal,
                    ImportReferenceRecord.item_ordinal,
                )
                .limit(limit)
                .offset(offset)
            ).all()
            return (
                [replace(self._import(record), locator=locator) for record, locator in records],
                total,
                analysis_id,
            )

    @staticmethod
    def _analysis_id(
        session: Session, project_id: UUID, snapshot_id: UUID, run_id: UUID | None = None
    ) -> UUID | None:
        return cast(
            UUID | None,
            session.scalar(
                select(StructuralAnalysisRecord.id)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == StructuralAnalysisRecord.project_version_id,
                )
                .where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                    StructuralAnalysisRecord.id
                    == (run_id if run_id else ProjectVersionRecord.active_analysis_id),
                    StructuralAnalysisRecord.quarantined.is_(False),
                    StructuralAnalysisRecord.producer_contract == structural_contract(),
                )
            ),
        )

    def _analysis(
        self, session: Session, record: StructuralAnalysisRecord, include_artifacts: bool = False
    ) -> StructuralAnalysis:
        artifacts = (
            session.scalars(
                select(ArtifactAnalysisRecord)
                .where(ArtifactAnalysisRecord.structural_analysis_id == record.id)
                .order_by(ArtifactAnalysisRecord.locator)
            ).all()
            if include_artifacts
            else []
        )
        return StructuralAnalysis(
            record.id,
            record.project_version_id,
            record.started_at,
            record.completed_at,
            cast(Any, record.outcome),
            record.parser_version,
            record.runtime_version,
            record.policy_version,
            cast(dict[str, int], record.limits),
            tuple(_diagnostic_from(cast(dict[str, Any], value)) for value in record.warnings),
            tuple(self._artifact_analysis(session, item) for item in artifacts),
            record.revision,
            record.captured_predecessor_analysis_id,
            cast(dict[str, str] | None, record.producer_contract),
            quarantined=record.quarantined,
            superseded=record.superseded,
            captured_active_analysis_id=record.captured_active_analysis_id,
            captured_latest_revision=record.captured_latest_revision,
            summary_data=cast(dict[str, int], record.summary),
        )

    def _artifact_analysis(
        self, session: Session, record: ArtifactAnalysisRecord
    ) -> ArtifactAnalysis:
        symbols = session.scalars(
            select(SymbolRecord)
            .where(SymbolRecord.artifact_analysis_id == record.id)
            .order_by(SymbolRecord.ordinal)
        ).all()
        imports = session.scalars(
            select(ImportReferenceRecord)
            .where(ImportReferenceRecord.artifact_analysis_id == record.id)
            .order_by(ImportReferenceRecord.statement_ordinal, ImportReferenceRecord.item_ordinal)
        ).all()
        parsed_symbols = tuple(self._symbol(item) for item in symbols)
        parsed_imports = tuple(self._import(item) for item in imports)
        return ArtifactAnalysis(
            record.id,
            record.structural_analysis_id,
            record.project_version_id,
            record.artifact_id,
            record.locator,
            cast(Any, record.outcome),
            record.read_started_at,
            record.read_completed_at,
            record.encoding,
            record.line_count,
            record.is_package_initializer,
            tuple(_diagnostic_from(cast(dict[str, Any], item)) for item in record.diagnostics),
            parsed_symbols,
            parsed_imports,
            record.input_fingerprint,
            record.snapshot_verified,
            cast(Any, record.execution_mode),
            record.baseline_artifact_analysis_id,
            record.reuse_source_artifact_analysis_id,
            language=record.language,
            declaration_extraction_id=record.declaration_extraction_id,
            extractions=self.extractions(session, record.id),
        )

    @staticmethod
    def _symbol(item: SymbolRecord, include_content: bool = True) -> Symbol:
        return Symbol(
            item.id,
            item.artifact_analysis_id,
            item.parent_id,
            item.ordinal,
            cast(Any, item.kind),
            item.name,
            item.qualified_name,
            item.is_async,
            SourceSpan(item.start_line, item.end_line, item.start_column, item.end_column),
            item.full_start_line,
            _signature_from(cast(dict[str, Any] | None, item.signature))
            if include_content
            else None,
            tuple(_decorator_from(cast(dict[str, Any], value)) for value in item.decorators)
            if include_content
            else (),
            tuple(
                value
                for raw in item.type_parameters
                if (value := _expression_from(cast(dict[str, Any], raw))) is not None
            )
            if include_content
            else (),
            item.stable_identity_id,
            item.continuity_reason,
            item.state_fingerprint,
            declaration_form=item.declaration_form,
            declaration_space=item.declaration_space,
            name_source=item.name_source,
            exported=item.exported,
            start_offset=item.start_offset,
            end_offset=item.end_offset,
            additional_spans=tuple(
                (int(pair[0]), int(pair[1])) for pair in (item.additional_spans or [])
            ),
        )

    @staticmethod
    def _import(item: ImportReferenceRecord) -> ImportReference:
        return ImportReference(
            item.id,
            item.artifact_analysis_id,
            item.owner_symbol_id,
            item.statement_ordinal,
            item.item_ordinal,
            cast(Any, item.form),
            item.from_module,
            item.name,
            item.alias,
            item.level,
            SourceSpan(item.start_line, item.end_line, item.start_column, item.end_column),
            cast(Any, item.resolution_status),
            cast(Any, item.resolution_reason),
        )
