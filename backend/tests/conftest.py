import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

# Unit HTTP fixtures never connect to this database. Integration fixtures override it.
os.environ.setdefault("BUILDLENS_DATABASE_URL", "postgresql+psycopg://test@127.0.0.1:1/test")

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from alembic import command
from buildlens.api.dependencies import get_project_service
from buildlens.api.main import create_app
from buildlens.application.projects import ProjectService
from buildlens.domain.entities import Project


@pytest.fixture
def pg_engine(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[Engine]:
    """Give integration and real-model tests a migrated database created from template0."""
    url = os.getenv("BUILDLENS_TEST_ADMIN_URL")
    if not url:
        if os.getenv("CI"):
            pytest.fail("CI requires BUILDLENS_TEST_ADMIN_URL; integration cannot be skipped")
        pytest.skip("Set BUILDLENS_TEST_ADMIN_URL to enable disposable PostgreSQL tests")
    assert make_url(url).drivername.startswith("postgresql")
    name = "buildlens_test_" + uuid4().hex
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    isolated_url = make_url(url).set(database=name)
    engine = create_engine(isolated_url)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    try:
        from buildlens.core.config import get_settings

        monkeypatch.setenv(
            "BUILDLENS_DATABASE_URL", isolated_url.render_as_string(hide_password=False)
        )
        get_settings.cache_clear()
        config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
        command.upgrade(config, getattr(request, "param", "head"))
        yield engine
    finally:
        engine.dispose()
        get_settings.cache_clear()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


class InMemoryProjectRepository:
    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}

    def add(self, project: Project) -> Project:
        if any(existing.root_path == project.root_path for existing in self.projects.values()):
            from buildlens.application.projects import DuplicateProjectRootPathError

            raise DuplicateProjectRootPathError("The root path is already registered.")
        self.projects[str(project.id)] = project
        return project

    def list(self) -> list[Project]:
        return list(reversed(list(self.projects.values())))

    def get(self, project_id: object) -> Project | None:
        return self.projects.get(str(project_id))


@pytest.fixture
def repository() -> InMemoryProjectRepository:
    return InMemoryProjectRepository()


@pytest.fixture
def client(repository: InMemoryProjectRepository) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_project_service] = lambda: ProjectService(repository)
    with TestClient(app) as test_client:
        yield test_client
