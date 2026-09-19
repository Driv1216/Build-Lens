"""Stage 4 stable identities, snapshot fingerprints, and retained analysis runs.

Revision ID: 0004_stable_incremental
Revises: 0003_structural_analysis
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_stable_incremental"
down_revision: str | None = "0003_structural_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "artifact_identities",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "project_id", uuid, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("scheme", sa.String(64), nullable=False, server_default="artifact-path-slot.v1"),
        sa.UniqueConstraint("id", "project_id", name="uq_artifact_identities_id_project"),
        sa.UniqueConstraint("project_id", "id", name="uq_artifact_identities_project_id"),
    )
    op.create_index("ix_artifact_identities_project", "artifact_identities", ["project_id"])
    op.create_table(
        "symbol_identities",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, nullable=False),
        sa.Column("artifact_identity_id", uuid, nullable=False),
        sa.Column("parent_identity_id", uuid),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "scheme", sa.String(64), nullable=False, server_default="python-declaration-slot.v1"
        ),
        sa.ForeignKeyConstraint(
            ["artifact_identity_id", "project_id"],
            ["artifact_identities.id", "artifact_identities.project_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_symbol_identities_artifact", "symbol_identities", ["artifact_identity_id"])
    op.add_column(
        "project_versions", sa.Column("sequence", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("project_versions", sa.Column("predecessor_id", uuid))
    op.add_column("project_versions", sa.Column("active_analysis_id", uuid))
    op.add_column("project_versions", sa.Column("root_observation", postgresql.JSONB()))
    op.add_column("project_versions", sa.Column("fingerprint_coverage", postgresql.JSONB()))
    op.add_column("project_versions", sa.Column("content_manifest", postgresql.JSONB()))
    op.add_column("project_versions", sa.Column("eligibility_provenance", postgresql.JSONB()))
    op.add_column(
        "project_versions",
        sa.Column(
            "comparison_contract",
            sa.String(64),
            nullable=False,
            server_default="stage4.comparison.v1",
        ),
    )
    op.add_column(
        "project_versions",
        sa.Column("legacy_unverified", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_project_versions_predecessor", "project_versions", ["predecessor_id"])
    op.add_column("artifacts", sa.Column("stable_identity_id", uuid))
    op.add_column("artifacts", sa.Column("continuity_reason", sa.String(64)))
    op.add_column("artifacts", sa.Column("fingerprint_algorithm", sa.String(16)))
    op.add_column("artifacts", sa.Column("fingerprint_schema_version", sa.String(64)))
    op.add_column("artifacts", sa.Column("content_digest", sa.String(64)))
    op.add_column(
        "artifacts",
        sa.Column(
            "fingerprint_status", sa.String(32), nullable=False, server_default="legacy_unverified"
        ),
    )
    op.add_column("artifacts", sa.Column("fingerprint_size_bytes", sa.BigInteger()))
    op.add_column("artifacts", sa.Column("fingerprint_read_started_at", sa.DateTime(timezone=True)))
    op.add_column(
        "artifacts", sa.Column("fingerprint_read_completed_at", sa.DateTime(timezone=True))
    )
    op.add_column("artifacts", sa.Column("fingerprint_reader_version", sa.String(64)))
    op.add_column("artifacts", sa.Column("fingerprint_diagnostic", sa.String(64)))
    op.create_index("ix_artifacts_stable_identity", "artifacts", ["stable_identity_id"])
    op.drop_constraint("uq_structural_analyses_version", "structural_analyses", type_="unique")
    op.add_column(
        "structural_analyses",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("structural_analyses", sa.Column("captured_predecessor_analysis_id", uuid))
    op.add_column("structural_analyses", sa.Column("producer_contract", postgresql.JSONB()))
    op.create_unique_constraint(
        "uq_structural_analyses_version_revision",
        "structural_analyses",
        ["project_version_id", "revision"],
    )
    op.add_column("artifact_analyses", sa.Column("input_fingerprint", sa.String(64)))
    op.add_column(
        "artifact_analyses",
        sa.Column("snapshot_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "artifact_analyses",
        sa.Column("execution_mode", sa.String(16), nullable=False, server_default="parsed"),
    )
    op.add_column("artifact_analyses", sa.Column("baseline_artifact_analysis_id", uuid))
    op.add_column("artifact_analyses", sa.Column("reuse_source_artifact_analysis_id", uuid))
    op.add_column("symbols", sa.Column("stable_identity_id", uuid))
    op.add_column("symbols", sa.Column("continuity_reason", sa.String(64)))
    op.add_column("symbols", sa.Column("state_fingerprint", sa.String(64)))
    op.create_index("ix_symbols_stable_identity", "symbols", ["stable_identity_id"])
    # Historical rows retain their timestamps but are deliberately never
    # upgraded into authoritative source evidence.
    op.execute("""
        WITH ordered AS (
          SELECT id, project_id,
                 row_number() OVER (
                   PARTITION BY project_id ORDER BY scan_completed_at NULLS FIRST, id
                 ) AS n,
                 lag(id) OVER (
                   PARTITION BY project_id ORDER BY scan_completed_at NULLS FIRST, id
                 ) AS predecessor
          FROM project_versions
        )
        UPDATE project_versions v SET sequence = ordered.n, predecessor_id = ordered.predecessor,
          legacy_unverified = true
        FROM ordered WHERE v.id = ordered.id
    """)
    op.execute("""
        UPDATE project_versions v SET active_analysis_id = a.id
        FROM structural_analyses a WHERE a.project_version_id = v.id
    """)


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN IF EXISTS (SELECT 1 FROM structural_analyses GROUP BY
        project_version_id HAVING count(*) > 1) THEN RAISE EXCEPTION 'Downgrade refused:
        retained analyses exceed the Stage 3 model'; END IF; END $$"""
    )
    op.drop_index("ix_symbols_stable_identity", table_name="symbols")
    for column in ["state_fingerprint", "continuity_reason", "stable_identity_id"]:
        op.drop_column("symbols", column)
    for column in [
        "reuse_source_artifact_analysis_id",
        "baseline_artifact_analysis_id",
        "execution_mode",
        "snapshot_verified",
        "input_fingerprint",
    ]:
        op.drop_column("artifact_analyses", column)
    op.drop_constraint(
        "uq_structural_analyses_version_revision", "structural_analyses", type_="unique"
    )
    for column in ["producer_contract", "captured_predecessor_analysis_id", "revision"]:
        op.drop_column("structural_analyses", column)
    op.create_unique_constraint(
        "uq_structural_analyses_version", "structural_analyses", ["project_version_id"]
    )
    op.drop_index("ix_artifacts_stable_identity", table_name="artifacts")
    for column in [
        "fingerprint_diagnostic",
        "fingerprint_reader_version",
        "fingerprint_read_completed_at",
        "fingerprint_read_started_at",
        "fingerprint_size_bytes",
        "fingerprint_status",
        "content_digest",
        "fingerprint_schema_version",
        "fingerprint_algorithm",
        "continuity_reason",
        "stable_identity_id",
    ]:
        op.drop_column("artifacts", column)
    op.drop_index("ix_project_versions_predecessor", table_name="project_versions")
    for column in [
        "legacy_unverified",
        "comparison_contract",
        "eligibility_provenance",
        "content_manifest",
        "fingerprint_coverage",
        "root_observation",
        "active_analysis_id",
        "predecessor_id",
        "sequence",
    ]:
        op.drop_column("project_versions", column)
    op.drop_index("ix_symbol_identities_artifact", table_name="symbol_identities")
    op.drop_table("symbol_identities")
    op.drop_index("ix_artifact_identities_project", table_name="artifact_identities")
    op.drop_table("artifact_identities")
