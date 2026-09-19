"""Optional real-PostgreSQL coverage; this suite never substitutes SQLite."""

from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from buildlens.domain.project_factory import new_project
from buildlens.infrastructure.persistence.repositories import SqlAlchemyProjectRepository

pytestmark = pytest.mark.integration


def test_postgres_repository_persists_a_project(pg_engine: Engine) -> None:
    engine = pg_engine
    root_path = f"/tmp/buildlens-integration-{uuid4()}"
    try:
        with Session(engine) as session:
            repository = SqlAlchemyProjectRepository(session)
            saved = repository.add(new_project(name="Integration Project", root_path=root_path))
            reloaded = repository.get(saved.id)
            assert reloaded == saved
    finally:
        engine.dispose()
