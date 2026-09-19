import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, MetaData, create_engine, text
from sqlalchemy.engine import make_url

from alembic import command

pytestmark = pytest.mark.integration


def _config() -> Config:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parents[2] / "alembic"))
    return config


def test_empty_stage7_downgrade_keeps_shared_extension(pg_engine: Engine) -> None:
    command.downgrade(_config(), "0007_evidence_ownership")
    with pg_engine.connect() as connection:
        version = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname='vector'")
        )
        assert version == "0.8.6"
        assert connection.scalar(text("SELECT to_regclass('public.embedding_runs')")) is None


@pytest.mark.parametrize("pg_engine", ["0007_evidence_ownership"], indirect=True)
def test_administrator_preprovisioned_extension_is_verified(pg_engine: Engine) -> None:
    with pg_engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION vector WITH SCHEMA public VERSION '0.8.6'"))
    command.upgrade(_config(), "head")
    with pg_engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT e.extversion, n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'"
            )
        ).one() == ("0.8.6", "public")


def test_extension_creation_requires_sufficient_privilege(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_url = os.environ.get("BUILDLENS_TEST_ADMIN_URL")
    assert admin_url is not None
    suffix = uuid4().hex
    role, database, password = f"bl_limited_{suffix}", f"bl_limited_{suffix}", suffix
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    limited_url = make_url(admin_url).set(username=role, password=password, database=database)
    limited = create_engine(limited_url)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'"))
        connection.execute(text(f'CREATE DATABASE "{database}" OWNER "{role}" TEMPLATE template0'))
    try:
        from buildlens.core.config import get_settings

        monkeypatch.setenv(
            "BUILDLENS_DATABASE_URL", limited_url.render_as_string(hide_password=False)
        )
        get_settings.cache_clear()
        command.upgrade(_config(), "0007_evidence_ownership")
        with pytest.raises(Exception, match="permission denied|superuser"):
            command.upgrade(_config(), "head")
        with limited.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0007_evidence_ownership"
            )
    finally:
        limited.dispose()
        get_settings.cache_clear()
        with admin.begin() as connection:
            connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
            connection.execute(text(f'DROP ROLE "{role}"'))
        admin.dispose()


@pytest.mark.parametrize("pg_engine", ["0005_artifact_processing"], indirect=True)
def test_populated_upgrade_retains_and_quarantines_history(pg_engine: Engine) -> None:
    metadata = MetaData()
    metadata.reflect(pg_engine)
    now = datetime.now(UTC)
    project, snapshot, structure, run, artifact, processing, chunk = (uuid4() for _ in range(7))
    with pg_engine.begin() as connection:
        connection.execute(
            metadata.tables["projects"]
            .insert()
            .values(
                id=project,
                name="Historical",
                root_path="/historical/unavailable",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            metadata.tables["project_versions"]
            .insert()
            .values(id=snapshot, project_id=project, created_at=now)
        )
        connection.execute(
            metadata.tables["artifacts"]
            .insert()
            .values(
                id=artifact,
                project_version_id=snapshot,
                kind="file",
                locator="a.txt",
                created_at=now,
            )
        )
        connection.execute(
            metadata.tables["structural_analyses"]
            .insert()
            .values(
                id=structure,
                project_version_id=snapshot,
                started_at=now,
                completed_at=now,
                outcome="complete",
                parser_version="legacy",
                runtime_version="legacy",
                policy_version="legacy",
                limits={},
                summary={},
                warnings=[],
                revision=1,
            )
        )
        connection.execute(
            metadata.tables["processing_runs"]
            .insert()
            .values(
                id=run,
                project_version_id=snapshot,
                revision=1,
                started_at=now,
                completed_at=now,
                outcome="complete",
                processing_contract={},
                limits={},
                coverage={},
                diagnostics=[],
                captured_latest_revision=0,
            )
        )
        connection.execute(
            metadata.tables["artifact_processings"]
            .insert()
            .values(
                id=processing,
                processing_run_id=run,
                project_version_id=snapshot,
                artifact_id=artifact,
                locator="a.txt",
                outcome="processed",
                diagnostics=[],
                execution_mode="processed",
            )
        )
        # Old schema allowed a dangling symbol association. New writes must not.
        connection.execute(
            metadata.tables["chunks"]
            .insert()
            .values(
                id=chunk,
                artifact_processing_id=processing,
                stable_key="a" * 64,
                ordinal=0,
                unit_anchor="legacy",
                part_index=0,
                kind="text",
                content="retained legacy evidence",
                content_hash="b" * 64,
                start_offset=0,
                end_offset=24,
                start_line=1,
                end_line=1,
                start_column=0,
                end_column=24,
                symbol_id=uuid4(),
            )
        )
        connection.execute(
            text(
                "UPDATE project_versions SET active_analysis_id=:structure, "
                "active_processing_run_id=:run WHERE id=:snapshot"
            ),
            {"structure": structure, "run": run, "snapshot": snapshot},
        )
    command.upgrade(_config(), "head")
    with pg_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM projects")) == 1
        assert connection.scalar(text("SELECT count(*) FROM project_versions")) == 1
        assert (
            connection.scalar(text("SELECT content FROM chunks WHERE id=:id"), {"id": chunk})
            == "retained legacy evidence"
        )
        assert connection.scalar(text("SELECT quarantined FROM structural_analyses")) is True
        assert connection.scalar(text("SELECT quarantined FROM processing_runs")) is True
        assert connection.execute(
            text("SELECT active_analysis_id,active_processing_run_id FROM project_versions")
        ).one() == (None, None)
        invalid = set(
            connection.scalars(text("SELECT conname FROM pg_constraint WHERE NOT convalidated"))
        )
        assert "fk_chunk_symbol" in invalid
        assert "ck_chunk_symbol_owner_required" in invalid
    with pytest.raises(Exception, match="IntegrityError|constraint|foreign key"):
        with pg_engine.begin() as connection:
            connection.execute(
                text("UPDATE chunks SET symbol_id=:id WHERE id=:chunk"),
                {"id": uuid4(), "chunk": chunk},
            )


@pytest.mark.parametrize("pg_engine", ["0004_stable_incremental"], indirect=True)
def test_stage4_downgrade_refuses_before_destructive_changes(pg_engine: Engine) -> None:
    now = datetime.now(UTC)
    project, snapshot = uuid4(), uuid4()
    metadata = MetaData()
    metadata.reflect(pg_engine)
    with pg_engine.begin() as connection:
        connection.execute(
            metadata.tables["projects"]
            .insert()
            .values(
                id=project, name="History", root_path="/history", created_at=now, updated_at=now
            )
        )
        connection.execute(
            metadata.tables["project_versions"]
            .insert()
            .values(id=snapshot, project_id=project, created_at=now)
        )
        for revision in [1, 2]:
            connection.execute(
                metadata.tables["structural_analyses"]
                .insert()
                .values(
                    id=uuid4(),
                    project_version_id=snapshot,
                    started_at=now,
                    completed_at=now,
                    outcome="complete",
                    parser_version="legacy",
                    runtime_version="legacy",
                    policy_version="legacy",
                    limits={},
                    summary={},
                    warnings=[],
                    revision=revision,
                )
            )
    with pytest.raises(Exception, match="Downgrade refused"):
        command.downgrade(_config(), "0003_structural_analysis")
    with pg_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM structural_analyses")) == 2
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0004_stable_incremental"
        )
