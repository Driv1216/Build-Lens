"""Repository inspection results, independent of filesystem implementation."""

from dataclasses import dataclass
from datetime import datetime

from buildlens.domain.repository_snapshot import (
    ArtifactMetadata,
    GitMetadata,
    ScanStatus,
    SnapshotSummary,
    SnapshotWarning,
)


class InspectionError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ArtifactObservation:
    kind: str
    locator: str
    metadata: ArtifactMetadata


@dataclass(frozen=True, slots=True)
class InspectionResult:
    canonical_root: str
    scan_started_at: datetime
    scan_completed_at: datetime
    scan_status: ScanStatus
    policy_version: str
    git_metadata: GitMetadata
    warnings: tuple[SnapshotWarning, ...]
    summary: SnapshotSummary
    artifacts: tuple[ArtifactObservation, ...]
    root_observation: dict[str, object]
