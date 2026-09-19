"""PostgreSQL snapshot aggregate persistence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from buildlens.application.snapshots import SnapshotConflictError, SnapshotStore
from buildlens.domain.entities import Artifact, Project, ProjectVersion
from buildlens.domain.fingerprints import ContentFingerprint, FingerprintCoverage
from buildlens.domain.repository_snapshot import (
    ArtifactMetadata,
    ContentHint,
    GitMetadata,
    IgnoreStatus,
    InventoryStatus,
    ScanStatus,
    SnapshotSummary,
    SnapshotWarning,
)
from buildlens.infrastructure.persistence.models import (
    ArtifactIdentityRecord,
    ArtifactRecord,
    ProjectRecord,
    ProjectVersionRecord,
)
from buildlens.infrastructure.persistence.repositories import _to_domain


def _git_to_json(value: GitMetadata | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "state": value.state,
        "repository_root": value.repository_root,
        "project_prefix": value.project_prefix,
        "branch": value.branch,
        "head_commit": value.head_commit,
        "dirty": value.dirty,
        "observed_at": value.observed_at.isoformat(),
    }


def _version(record: ProjectVersionRecord) -> ProjectVersion:
    metadata = cast(dict[str, Any], record.git_metadata or {})
    git = (
        None
        if not metadata
        else GitMetadata(
            cast("Any", metadata["state"]),
            cast("str | None", metadata.get("repository_root")),
            cast("str | None", metadata.get("project_prefix")),
            cast("str | None", metadata.get("branch")),
            cast("str | None", metadata.get("head_commit")),
            cast("bool | None", metadata.get("dirty")),
            # Git timing is evidence in its own right; do not replace it with
            # the enclosing snapshot timestamp while reconstructing records.
            datetime.fromisoformat(cast(str, metadata["observed_at"]))
            if metadata.get("observed_at")
            else (record.scan_completed_at or record.created_at),
        )
    )
    warnings = tuple(
        SnapshotWarning(
            cast(str, item["code"]),
            cast(str, item["message"]),
            cast("str | None", item.get("locator")),
        )
        for item in (record.warnings or [])
    )
    raw_summary = cast(dict[str, Any], record.summary or {})
    summary = (
        None
        if not raw_summary
        else SnapshotSummary(
            cast(dict[str, int], raw_summary.get("observed_by_kind", {})),
            cast(int, raw_summary.get("included_regular_files", 0)),
            cast(int, raw_summary.get("excluded_regular_files", 0)),
            cast(int, raw_summary.get("pruned_directories", 0)),
            cast(int, raw_summary.get("included_bytes", 0)),
            cast(dict[str, int], raw_summary.get("language_counts", {})),
            cast(dict[str, int], raw_summary.get("tag_counts", {})),
            cast(dict[str, int], raw_summary.get("exclusion_reason_counts", {})),
            cast(int, raw_summary.get("warning_count", 0)),
        )
    )
    return ProjectVersion(
        record.id,
        record.project_id,
        record.label,
        record.created_at,
        record.canonical_root,
        record.scan_started_at,
        record.scan_completed_at,
        cast(ScanStatus | None, record.scan_status),
        record.policy_version,
        git,
        warnings,
        summary,
        record.sequence,
        record.predecessor_id,
        record.root_observation,
        FingerprintCoverage(**cast(dict[str, Any], record.fingerprint_coverage))
        if record.fingerprint_coverage
        else None,
        ContentFingerprint(
            cast(Any, record.content_manifest["algorithm"]),
            cast(Any, record.content_manifest["schema_version"]),
            cast(str, record.content_manifest["digest"]),
        )
        if record.content_manifest
        else None,
        record.eligibility_provenance,
        record.comparison_contract,
        record.active_analysis_id,
        record.active_processing_run_id,
        record.legacy_unverified,
    )


def _artifact(record: ArtifactRecord) -> Artifact:
    metadata = (
        ArtifactMetadata(
            cast(InventoryStatus, record.inventory_status),
            record.size_bytes,
            record.language,
            tuple(record.tags or []),
            cast(ContentHint, record.content_hint),
            cast(IgnoreStatus, record.ignore_status),
            tuple(record.exclusion_reasons or []),
            bool(record.pruned),
        )
        if record.inventory_status
        else None
    )
    return Artifact(
        record.id,
        record.project_version_id,
        record.kind,
        record.locator,
        record.created_at,
        metadata,
        record.stable_identity_id,
        record.continuity_reason,
        ContentFingerprint(
            cast(Any, record.fingerprint_algorithm),
            cast(Any, record.fingerprint_schema_version),
            record.content_digest,
        )
        if record.content_digest
        and record.fingerprint_algorithm
        and record.fingerprint_schema_version
        else None,
        cast(Any, record.fingerprint_status),
        record.fingerprint_size_bytes,
        record.fingerprint_read_started_at,
        record.fingerprint_read_completed_at,
        record.fingerprint_reader_version,
        record.fingerprint_diagnostic,
    )


class SqlAlchemySnapshotStore(SnapshotStore):
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_project(self, project_id: UUID) -> Project | None:
        with self._session_factory() as session:
            record = session.get(ProjectRecord, project_id)
            return _to_domain(record) if record else None

    def latest(self, project_id: UUID) -> ProjectVersion | None:
        with self._session_factory() as session:
            record = session.scalar(
                select(ProjectVersionRecord)
                .where(
                    ProjectVersionRecord.project_id == project_id,
                    ProjectVersionRecord.scan_completed_at.is_not(None),
                )
                .order_by(ProjectVersionRecord.sequence.desc(), ProjectVersionRecord.id.desc())
                .limit(1)
            )
            return _version(record) if record else None

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

    def artifacts(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[Artifact], int]:
        with self._session_factory() as session:
            base = (
                select(ArtifactRecord)
                .join(
                    ProjectVersionRecord,
                    ProjectVersionRecord.id == ArtifactRecord.project_version_id,
                )
                .where(
                    ArtifactRecord.project_version_id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                    ProjectVersionRecord.scan_completed_at.is_not(None),
                )
            )
            total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
            records = session.scalars(
                base.order_by(ArtifactRecord.locator).limit(limit).offset(offset)
            ).all()
            return [_artifact(record) for record in records], total

    def save(self, version: ProjectVersion, artifacts: list[Artifact]) -> ProjectVersion:
        with self._session_factory.begin() as session:
            project = session.get(ProjectRecord, version.project_id, with_for_update=True)
            if project is None:
                raise RuntimeError("Project disappeared before snapshot persistence.")
            latest = session.scalar(
                select(ProjectVersionRecord)
                .where(ProjectVersionRecord.project_id == version.project_id)
                .order_by(ProjectVersionRecord.sequence.desc(), ProjectVersionRecord.id.desc())
                .limit(1)
            )
            if (latest.id if latest else None) != version.predecessor_id:
                raise SnapshotConflictError("The snapshot predecessor changed before commit.")
            sequence = (latest.sequence if latest else 0) + 1
            session.add(
                ProjectVersionRecord(
                    id=version.id,
                    project_id=version.project_id,
                    label=version.label,
                    created_at=version.created_at,
                    canonical_root=version.canonical_root,
                    scan_started_at=version.scan_started_at,
                    scan_completed_at=version.scan_completed_at,
                    scan_status=version.scan_status,
                    policy_version=version.policy_version,
                    git_metadata=_git_to_json(version.git_metadata),
                    warnings=[
                        {
                            "code": warning.code,
                            "message": warning.message,
                            "locator": warning.locator,
                        }
                        for warning in version.warnings
                    ],
                    summary={
                        "observed_by_kind": version.summary.observed_by_kind,
                        "included_regular_files": version.summary.included_regular_files,
                        "excluded_regular_files": version.summary.excluded_regular_files,
                        "pruned_directories": version.summary.pruned_directories,
                        "included_bytes": version.summary.included_bytes,
                        "language_counts": version.summary.language_counts,
                        "tag_counts": version.summary.tag_counts,
                        "exclusion_reason_counts": version.summary.exclusion_reason_counts,
                        "warning_count": version.summary.warning_count,
                    }
                    if version.summary
                    else None,
                    sequence=sequence,
                    predecessor_id=version.predecessor_id,
                    root_observation=version.root_observation,
                    fingerprint_coverage=(
                        {
                            "eligible_files": version.fingerprint_coverage.eligible_files,
                            "fingerprinted_files": version.fingerprint_coverage.fingerprinted_files,
                            "unavailable_files": version.fingerprint_coverage.unavailable_files,
                            "not_eligible_files": version.fingerprint_coverage.not_eligible_files,
                            "bytes_consumed": version.fingerprint_coverage.bytes_consumed,
                            "complete": version.fingerprint_coverage.complete,
                        }
                        if version.fingerprint_coverage
                        else None
                    ),
                    content_manifest=(
                        {
                            "algorithm": version.content_manifest.algorithm,
                            "schema_version": version.content_manifest.schema_version,
                            "digest": version.content_manifest.digest,
                        }
                        if version.content_manifest
                        else None
                    ),
                    eligibility_provenance=version.eligibility_provenance,
                    comparison_contract=version.comparison_contract,
                    legacy_unverified=False,
                )
            )
            # Artifacts reference the new version by UUID rather than an ORM relationship.
            # Flush it first so PostgreSQL can satisfy the foreign key in this transaction.
            session.flush()
            previous_by_locator: dict[str, ArtifactRecord] = {}
            if latest and latest.root_observation == version.root_observation:
                previous_by_locator = {
                    item.locator: item
                    for item in session.scalars(
                        select(ArtifactRecord).where(ArtifactRecord.project_version_id == latest.id)
                    )
                }
            records: list[ArtifactRecord] = []
            for item in artifacts:
                if not item.metadata:
                    continue
                predecessor = previous_by_locator.get(item.locator)
                continuing = bool(
                    predecessor
                    and predecessor.kind == "file"
                    and predecessor.inventory_status == "included"
                    and predecessor.stable_identity_id
                    and item.kind == "file"
                    and item.metadata.inventory_status == "included"
                )
                identity_id = predecessor.stable_identity_id if continuing and predecessor else None
                if (
                    item.kind == "file"
                    and item.metadata.inventory_status == "included"
                    and identity_id is None
                ):
                    identity = ArtifactIdentityRecord(id=uuid4(), project_id=version.project_id)
                    session.add(identity)
                    session.flush()
                    identity_id = identity.id
                records.append(
                    ArtifactRecord(
                        project_id=version.project_id,
                        id=item.id,
                        project_version_id=item.project_version_id,
                        kind=item.kind,
                        locator=item.locator,
                        created_at=item.created_at,
                        inventory_status=item.metadata.inventory_status,
                        size_bytes=item.metadata.size_bytes,
                        language=item.metadata.language,
                        tags=list(item.metadata.tags),
                        content_hint=item.metadata.content_hint,
                        ignore_status=item.metadata.ignore_status,
                        exclusion_reasons=list(item.metadata.exclusion_reasons),
                        pruned=item.metadata.pruned,
                        stable_identity_id=identity_id,
                        continuity_reason=(
                            "continued_path_slot" if continuing else "new_path_slot"
                        ),
                        fingerprint_algorithm=item.content_fingerprint.algorithm
                        if item.content_fingerprint
                        else None,
                        fingerprint_schema_version=item.content_fingerprint.schema_version
                        if item.content_fingerprint
                        else None,
                        content_digest=item.content_fingerprint.digest
                        if item.content_fingerprint
                        else None,
                        fingerprint_status=item.fingerprint_status,
                        fingerprint_size_bytes=item.fingerprint_size_bytes,
                        fingerprint_read_started_at=item.fingerprint_read_started_at,
                        fingerprint_read_completed_at=item.fingerprint_read_completed_at,
                        fingerprint_reader_version=item.fingerprint_reader_version,
                        fingerprint_diagnostic=item.fingerprint_diagnostic,
                    )
                )
            session.add_all(records)
        return replace(version, sequence=sequence, legacy_unverified=False)

    def changes(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[dict[str, object]], int]:
        with self._session_factory() as session:
            current = session.scalar(
                select(ProjectVersionRecord).where(
                    ProjectVersionRecord.id == snapshot_id,
                    ProjectVersionRecord.project_id == project_id,
                )
            )
            if current is None:
                return [], 0
            current_artifacts = session.scalars(
                select(ArtifactRecord).where(ArtifactRecord.project_version_id == snapshot_id)
            ).all()
            predecessor = (
                session.get(ProjectVersionRecord, current.predecessor_id)
                if current.predecessor_id
                else None
            )
            previous_artifacts = (
                session.scalars(
                    select(ArtifactRecord).where(
                        ArtifactRecord.project_version_id == predecessor.id
                    )
                ).all()
                if predecessor
                else []
            )
            previous_by_identity = {
                item.stable_identity_id: item
                for item in previous_artifacts
                if item.stable_identity_id is not None
            }
            current_by_locator = {item.locator: item for item in current_artifacts}
            rows: list[dict[str, object]] = []
            for item in current_artifacts:
                if item.kind != "file" or item.inventory_status != "included":
                    continue
                baseline = (
                    previous_by_identity.get(item.stable_identity_id)
                    if item.stable_identity_id is not None
                    else None
                )
                if predecessor is None:
                    state, reason = "ADDED", "first_observed_baseline"
                elif baseline is None:
                    state, reason = (
                        ("ADDED", "observable_absence")
                        if predecessor.scan_status == "complete"
                        and current.scan_status == "complete"
                        else ("UNKNOWN", "absence_not_observable")
                    )
                elif (
                    item.fingerprint_status == "fingerprinted"
                    and baseline.fingerprint_status == "fingerprinted"
                    and item.fingerprint_algorithm == baseline.fingerprint_algorithm
                    and item.fingerprint_schema_version == baseline.fingerprint_schema_version
                ):
                    state = (
                        "UNCHANGED" if item.content_digest == baseline.content_digest else "CHANGED"
                    )
                    reason = "authoritative_byte_fingerprints"
                else:
                    state, reason = "UNKNOWN", "fingerprint_unavailable_or_incompatible"
                rows.append(
                    {
                        "state": state,
                        "reason": reason,
                        "artifact_id": item.id,
                        "previous_artifact_id": baseline.id if baseline else None,
                        "locator": item.locator,
                        "stable_identity_id": item.stable_identity_id,
                    }
                )
            if (
                predecessor
                and predecessor.scan_status == "complete"
                and current.scan_status == "complete"
            ):
                current_identity_ids = {item.stable_identity_id for item in current_artifacts}
                for item in previous_artifacts:
                    if (
                        item.kind == "file"
                        and item.inventory_status == "included"
                        and item.stable_identity_id not in current_identity_ids
                        and item.locator not in current_by_locator
                    ):
                        rows.append(
                            {
                                "state": "REMOVED",
                                "reason": "observable_absence",
                                "artifact_id": None,
                                "previous_artifact_id": item.id,
                                "locator": item.locator,
                                "stable_identity_id": item.stable_identity_id,
                            }
                        )
            rows.sort(
                key=lambda row: (cast(str, row["locator"]).encode("utf-8"), cast(str, row["state"]))
            )
            return rows[offset : offset + limit], len(rows)
