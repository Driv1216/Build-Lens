import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from buildlens.application.fingerprinting import SnapshotFingerprinter
from buildlens.application.snapshots import SnapshotService
from buildlens.domain.entities import Artifact, Project, ProjectVersion
from buildlens.infrastructure.discovery.filesystem import LocalRepositoryInspector
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader


class MemorySnapshotStore:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.versions: list[ProjectVersion] = []
        self.saved_artifacts: list[list[Artifact]] = []

    def get_project(self, project_id: UUID) -> Project | None:
        return self.project if self.project.id == project_id else None

    def latest(self, project_id: UUID) -> ProjectVersion | None:
        return (
            self.versions[-1]
            if self.versions and self.versions[-1].project_id == project_id
            else None
        )

    def get_snapshot(self, project_id: UUID, snapshot_id: UUID) -> ProjectVersion | None:
        return next(
            (
                item
                for item in self.versions
                if item.project_id == project_id and item.id == snapshot_id
            ),
            None,
        )

    def artifacts(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[Artifact], int]:
        for version, artifacts in zip(self.versions, self.saved_artifacts, strict=True):
            if version.project_id == project_id and version.id == snapshot_id:
                return artifacts[offset : offset + limit], len(artifacts)
        return [], 0

    def changes(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[dict[str, object]], int]:
        return [], 0

    def save(self, version: ProjectVersion, artifacts: list[Artifact]) -> ProjectVersion:
        self.versions.append(version)
        self.saved_artifacts.append(artifacts)
        return version


def test_inspector_is_metadata_only_and_preserves_editor_directories(tmp_path: Path) -> None:
    (tmp_path / "src.py").write_text("print('not read by inspection')")
    (tmp_path / ".env.example").write_text("SECRET=not-persisted")
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text("{}")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "nested.js").write_text("ignored")
    os.symlink("src.py", tmp_path / "source-link")

    result = LocalRepositoryInspector().inspect(str(tmp_path))
    by_locator = {item.locator: item for item in result.artifacts}

    assert by_locator[".env.example"].metadata.inventory_status == "excluded"
    assert by_locator[".env.example"].metadata.size_bytes is None
    assert by_locator[".vscode"].metadata.inventory_status == "included"
    assert by_locator[".vscode/settings.json"].metadata.inventory_status == "included"
    assert by_locator["node_modules"].metadata.pruned is True
    assert "node_modules/nested.js" not in by_locator
    assert by_locator["source-link"].metadata.exclusion_reasons == ("symlink_not_followed",)


def test_rescans_create_independent_snapshot_identities(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('metadata only')")
    now = datetime.now(UTC)
    project = Project(uuid4(), "Example", str(tmp_path), now, now)
    store = MemorySnapshotStore(project)
    service = SnapshotService(
        store, LocalRepositoryInspector(), SnapshotFingerprinter(reader=SupervisedSourceReader())
    )

    first = service.create(project.id)
    second = service.create(project.id)

    assert first.id != second.id
    assert first.scan_completed_at is not None
    assert second.scan_completed_at is not None
    assert first.scan_completed_at <= second.scan_completed_at
    assert len(store.saved_artifacts) == 2
