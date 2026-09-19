"""Snapshot orchestration and infrastructure ports."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from buildlens.application.fingerprinting import SnapshotFingerprinter
from buildlens.application.projects import ProjectNotFoundError
from buildlens.domain.entities import Artifact, Project, ProjectVersion
from buildlens.domain.inspection import InspectionResult


class SnapshotNotFoundError(Exception):
    pass


class ScanBusyError(Exception):
    pass


class SnapshotConflictError(Exception):
    pass


class SnapshotStore(Protocol):
    def get_project(self, project_id: UUID) -> Project | None: ...
    def latest(self, project_id: UUID) -> ProjectVersion | None: ...
    def get_snapshot(self, project_id: UUID, snapshot_id: UUID) -> ProjectVersion | None: ...
    def artifacts(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[Artifact], int]: ...
    def changes(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[dict[str, object]], int]: ...
    def save(self, version: ProjectVersion, artifacts: list[Artifact]) -> ProjectVersion: ...


class RepositoryInspector(Protocol):
    def inspect(
        self, root_path: str, previous_canonical_root: str | None = None
    ) -> InspectionResult: ...


@dataclass(frozen=True, slots=True)
class ArtifactPage:
    items: list[Artifact]
    total: int
    limit: int
    offset: int


class SnapshotService:
    """Coordinates an inspection outside of a short aggregate save transaction."""

    def __init__(
        self,
        store: SnapshotStore,
        inspector: RepositoryInspector,
        fingerprinter: SnapshotFingerprinter,
        scan_lock: Lock | None = None,
    ) -> None:
        self._store = store
        self._inspector = inspector
        self._scan_lock = scan_lock or Lock()
        self._fingerprinter = fingerprinter

    def create(self, project_id: UUID) -> ProjectVersion:
        project = self._store.get_project(project_id)
        if project is None:
            raise ProjectNotFoundError(f"Project {project_id} was not found.")
        if not self._scan_lock.acquire(blocking=False):
            raise ScanBusyError("Another repository inspection is already running.")
        try:
            previous = self._store.latest(project_id)
            result = self._inspector.inspect(
                project.root_path, previous.canonical_root if previous else None
            )
            provisional = ProjectVersion(
                id=uuid4(),
                project_id=project_id,
                label=None,
                created_at=result.scan_completed_at,
                canonical_root=result.canonical_root,
                scan_started_at=result.scan_started_at,
                scan_completed_at=result.scan_completed_at,
                scan_status=result.scan_status,
                policy_version=result.policy_version,
                git_metadata=result.git_metadata,
                warnings=result.warnings,
                summary=result.summary,
                predecessor_id=previous.id if previous else None,
            )
            artifacts = [
                Artifact(
                    uuid4(),
                    provisional.id,
                    item.kind,
                    item.locator,
                    result.scan_completed_at,
                    item.metadata,
                )
                for item in result.artifacts
            ]
            fingerprints = self._fingerprinter.fingerprint(
                result.canonical_root, artifacts, result.root_observation
            )
            version = replace(
                provisional,
                scan_completed_at=datetime.now(UTC),
                fingerprint_coverage=fingerprints.coverage,
                content_manifest=fingerprints.manifest,
                root_observation=fingerprints.root_observation,
                eligibility_provenance={
                    "policy_version": result.policy_version,
                    "ignore": "evaluated",
                },
            )
            return self._store.save(version, fingerprints.artifacts)
        finally:
            self._scan_lock.release()

    def latest(self, project_id: UUID) -> ProjectVersion:
        self._require_project(project_id)
        version = self._store.latest(project_id)
        if version is None:
            raise SnapshotNotFoundError("No repository snapshot exists for this project.")
        return version

    def get(self, project_id: UUID, snapshot_id: UUID) -> ProjectVersion:
        self._require_project(project_id)
        version = self._store.get_snapshot(project_id, snapshot_id)
        if version is None:
            raise SnapshotNotFoundError("Repository snapshot was not found.")
        return version

    def artifact_page(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> ArtifactPage:
        self.get(project_id, snapshot_id)
        items, total = self._store.artifacts(project_id, snapshot_id, limit, offset)
        return ArtifactPage(items, total, limit, offset)

    def change_page(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[dict[str, object]], int]:
        self.get(project_id, snapshot_id)
        return self._store.changes(project_id, snapshot_id, limit, offset)

    def _require_project(self, project_id: UUID) -> None:
        if self._store.get_project(project_id) is None:
            raise ProjectNotFoundError(f"Project {project_id} was not found.")
