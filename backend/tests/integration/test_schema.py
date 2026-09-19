import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text

from buildlens.infrastructure.persistence.models import Base

pytestmark = pytest.mark.integration


def test_migrated_schema_matches_metadata(pg_engine: Engine) -> None:
    with pg_engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        extension = connection.execute(
            text(
                "SELECT e.extversion, n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'"
            )
        ).one()
    assert differences == [], repr(differences)
    assert extension == ("0.8.6", "public")
