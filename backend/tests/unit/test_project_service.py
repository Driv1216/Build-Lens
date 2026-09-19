from uuid import uuid4

import pytest

from buildlens.application.projects import (
    CreateProjectCommand,
    ProjectNotFoundError,
    ProjectService,
)
from tests.conftest import InMemoryProjectRepository


def test_create_normalizes_project_metadata() -> None:
    service = ProjectService(InMemoryProjectRepository())

    project = service.create(
        CreateProjectCommand(name="  BuildLens  ", root_path=" /work/buildlens ")
    )

    assert project.name == "BuildLens"
    assert project.root_path == "/work/buildlens"


@pytest.mark.parametrize("name,root_path", [("", "/work/a"), ("A", "  ")])
def test_create_rejects_blank_required_metadata(name: str, root_path: str) -> None:
    service = ProjectService(InMemoryProjectRepository())

    with pytest.raises(ValueError):
        service.create(CreateProjectCommand(name=name, root_path=root_path))


def test_get_missing_project_raises_application_error() -> None:
    service = ProjectService(InMemoryProjectRepository())

    with pytest.raises(ProjectNotFoundError):
        service.get(uuid4())
