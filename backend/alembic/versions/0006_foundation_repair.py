"""Quarantine pre-repair evidence without deleting history; constrain new provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_foundation_repair"
down_revision: str | None = "0005_artifact_processing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    for table in ("structural_analyses", "processing_runs"):
        op.add_column(
            table, sa.Column("quarantined", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        op.alter_column(table, "quarantined", server_default=sa.false())
        op.add_column(
            table, sa.Column("superseded", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.add_column("structural_analyses", sa.Column("captured_active_analysis_id", uuid))
    op.add_column(
        "structural_analyses",
        sa.Column("captured_latest_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("artifact_processings", sa.Column("dependency_fingerprint", sa.String(64)))
    op.execute(
        "UPDATE project_versions SET active_analysis_id = NULL, active_processing_run_id = NULL"
    )
    op.create_unique_constraint("uq_versions_id_project", "project_versions", ["id", "project_id"])
    op.create_unique_constraint(
        "uq_artifact_analysis_id_artifact", "artifact_analyses", ["id", "artifact_id"]
    )
    op.create_unique_constraint(
        "uq_symbol_identity_id_artifact", "symbol_identities", ["id", "artifact_identity_id"]
    )
    # NOT VALID preserves suspect history but constrains every new insert/update.
    foreign_keys = [
        (
            "fk_version_predecessor",
            "project_versions",
            ["predecessor_id", "project_id"],
            "project_versions",
            ["id", "project_id"],
        ),
        (
            "fk_version_active_structure",
            "project_versions",
            ["active_analysis_id", "id"],
            "structural_analyses",
            ["id", "project_version_id"],
        ),
        (
            "fk_artifact_identity",
            "artifacts",
            ["stable_identity_id"],
            "artifact_identities",
            ["id"],
        ),
        ("fk_symbol_identity", "symbols", ["stable_identity_id"], "symbol_identities", ["id"]),
        (
            "fk_identity_parent",
            "symbol_identities",
            ["parent_identity_id", "artifact_identity_id"],
            "symbol_identities",
            ["id", "artifact_identity_id"],
        ),
        (
            "fk_processing_structure",
            "processing_runs",
            ["structural_analysis_id", "project_version_id"],
            "structural_analyses",
            ["id", "project_version_id"],
        ),
        (
            "fk_processing_artifact_structure",
            "artifact_processings",
            ["structural_artifact_analysis_id", "artifact_id"],
            "artifact_analyses",
            ["id", "artifact_id"],
        ),
        (
            "fk_processing_reuse",
            "artifact_processings",
            ["reuse_source_artifact_processing_id"],
            "artifact_processings",
            ["id"],
        ),
        ("fk_chunk_symbol", "chunks", ["symbol_id"], "symbols", ["id"]),
        ("fk_chunk_symbol_identity", "chunks", ["symbol_identity_id"], "symbol_identities", ["id"]),
        (
            "fk_artifact_analysis_baseline",
            "artifact_analyses",
            ["baseline_artifact_analysis_id"],
            "artifact_analyses",
            ["id"],
        ),
        (
            "fk_artifact_analysis_reuse",
            "artifact_analyses",
            ["reuse_source_artifact_analysis_id"],
            "artifact_analyses",
            ["id"],
        ),
    ]
    for name, table, local, target, remote in foreign_keys:
        op.create_foreign_key(
            name,
            table,
            target,
            local,
            remote,
            deferrable=True,
            initially="DEFERRED",
            postgresql_not_valid=True,
        )
        # Validate only when every historical row satisfies the FK. Preserve invalid history.
        conditions = " AND ".join(f'a."{column}" IS NOT NULL' for column in local)
        match = " AND ".join(
            f'a."{column}" = b."{other}"' for column, other in zip(local, remote, strict=True)
        )
        op.execute(
            f'''DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM "{table}" a WHERE {conditions} AND NOT
            EXISTS (SELECT 1 FROM "{target}" b WHERE {match})) THEN ALTER TABLE "{table}"
            VALIDATE CONSTRAINT "{name}"; END IF; END $$'''
        )
    op.create_index("ix_versions_project_sequence", "project_versions", ["project_id", "sequence"])


def downgrade() -> None:
    # Older application code ignores quarantine and would expose unsafe history.
    raise RuntimeError(
        "Foundation repair cannot be downgraded safely. Restore a pre-upgrade backup with "
        "the matching application version."
    )
