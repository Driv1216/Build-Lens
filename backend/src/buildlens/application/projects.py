import os
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from buildlens.domain.entities import Project
from buildlens.domain.project_factory import new_project


class ProjectNotFoundError(Exception):
    """Raised when a requested project does not exist."""


class DuplicateProjectRootPathError(Exception):
    """Raised when a project root path is already registered."""


class ProjectRepository(Protocol):
    def add(self, project: Project) -> Project: ...

    def list(self) -> list[Project]: ...

    def get(self, project_id: UUID) -> Project | None: ...


@dataclass(frozen=True, slots=True)
class CreateProjectCommand:
    name: str
    root_path: str


class ProjectService:
    """Application boundary for the project-management use cases."""

    def __init__(self, repository: ProjectRepository) -> None:
        self._repository = repository

    def create(self, command: CreateProjectCommand) -> Project:
        name = command.name.strip()
        root_path = command.root_path.strip()
        if not name:
            raise ValueError("Project name must not be blank.")
        if not root_path:
            raise ValueError("Project root path must not be blank.")
        if not (os.path.isabs(root_path) or root_path == "~" or root_path.startswith("~/")):
            raise ValueError("Project root path must be absolute or use the ~/ shorthand.")
        return self._repository.add(new_project(name=name, root_path=root_path))

    def list(self) -> list[Project]:
        return self._repository.list()

    def get(self, project_id: UUID) -> Project:
        project = self._repository.get(project_id)
        if project is None:
            raise ProjectNotFoundError(f"Project {project_id} was not found.")
        return project
