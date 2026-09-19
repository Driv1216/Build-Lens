from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from buildlens.domain.fingerprints import ContentFingerprint, FingerprintCoverage, FingerprintStatus
from buildlens.domain.repository_snapshot import (
    ArtifactMetadata,
    GitMetadata,
    ScanStatus,
    SnapshotSummary,
    SnapshotWarning,
)


@dataclass(frozen=True, slots=True)
class Project:
    """A local software project registered with BuildLens.

    `root_path` is opaque metadata in Stage 1; no filesystem access is performed.
    """

    id: UUID
    name: str
    root_path: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectVersion:
    """A minimal, user-independent snapshot identity for a project."""

    id: UUID
    project_id: UUID
    label: str | None
    created_at: datetime
    canonical_root: str | None = None
    scan_started_at: datetime | None = None
    scan_completed_at: datetime | None = None
    scan_status: ScanStatus | None = None
    policy_version: str | None = None
    git_metadata: GitMetadata | None = None
    warnings: tuple[SnapshotWarning, ...] = ()
    summary: SnapshotSummary | None = None
    sequence: int = 0
    predecessor_id: UUID | None = None
    root_observation: dict[str, object] | None = None
    fingerprint_coverage: FingerprintCoverage | None = None
    content_manifest: ContentFingerprint | None = None
    eligibility_provenance: dict[str, object] | None = None
    comparison_contract: str = "stage4.comparison.v1"
    active_analysis_id: UUID | None = None
    active_processing_run_id: UUID | None = None
    legacy_unverified: bool = False


@dataclass(frozen=True, slots=True)
class Artifact:
    """A minimal thing attached to one project version.

    Stage 1 deliberately does not infer content, hashes, or file structure.
    """

    id: UUID
    project_version_id: UUID
    kind: str
    locator: str
    created_at: datetime
    metadata: ArtifactMetadata | None = None
    stable_identity_id: UUID | None = None
    continuity_reason: str | None = None
    content_fingerprint: ContentFingerprint | None = None
    fingerprint_status: FingerprintStatus = "legacy_unverified"
    fingerprint_size_bytes: int | None = None
    fingerprint_read_started_at: datetime | None = None
    fingerprint_read_completed_at: datetime | None = None
    fingerprint_reader_version: str | None = None
    fingerprint_diagnostic: str | None = None
