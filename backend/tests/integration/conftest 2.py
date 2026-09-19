"""Every integration test owns a newly-created, migrated PostgreSQL database."""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from alembic import command


@pytest.fixture
def pg_engine(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[Engine]:
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
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        config.set_main_option("script_location", str(Path(__file__).parents[2] / "alembic"))
        command.upgrade(config, getattr(request, "param", "head"))
        yield engine
    finally:
        engine.dispose()
        get_settings.cache_clear()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
