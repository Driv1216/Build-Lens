"""Stage 5 retained artifact-processing evidence and authority.

Revision ID: 0005_artifact_processing
Revises: 0004_stable_incremental
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_artifact_processing"
down_revision: str | None = "0004_stable_incremental"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "processing_runs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "project_version_id",
            uuid,
            sa.ForeignKey("project_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("structural_analysis_id", uuid),
        sa.Column("processing_contract", postgresql.JSONB(), nullable=False),
        sa.Column("limits", postgresql.JSONB(), nullable=False),
        sa.Column("coverage", postgresql.JSONB(), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
        sa.Column("captured_active_processing_run_id", uuid),
        sa.Column("captured_latest_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "project_version_id", "revision", name="uq_processing_runs_version_revision"
        ),
        sa.UniqueConstraint("id", "project_version_id", name="uq_processing_runs_id_version"),
        sa.CheckConstraint(
            "outcome IN ('complete', 'partial', 'failed')", name="ck_processing_runs_outcome"
        ),
        sa.CheckConstraint("completed_at >= started_at", name="ck_processing_runs_times"),
    )
    op.create_index(
        "ix_processing_runs_version_revision", "processing_runs", ["project_version_id", "revision"]
    )
    op.create_table(
        "artifact_processings",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("processing_run_id", uuid, nullable=False),
        sa.Column("project_version_id", uuid, nullable=False),
        sa.Column("artifact_id", uuid, nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("processor_contract", postgresql.JSONB()),
        sa.Column("input_fingerprint", sa.String(64)),
        sa.Column("encoding", sa.String(64)),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False, server_default="processed"),
        sa.Column("reuse_source_artifact_processing_id", uuid),
        sa.Column("structural_artifact_analysis_id", uuid),
        sa.Column("result_digest", sa.String(64)),
        sa.ForeignKeyConstraint(
            ["processing_run_id", "project_version_id"],
            ["processing_runs.id", "processing_runs.project_version_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id", "project_version_id"],
            ["artifacts.id", "artifacts.project_version_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "processing_run_id", "artifact_id", name="uq_artifact_processings_run_artifact"
        ),
        sa.UniqueConstraint("id", "processing_run_id", name="uq_artifact_processings_id_run"),
        sa.CheckConstraint(
            "outcome IN ('processed', 'empty', 'excluded', 'unsupported', 'skipped', 'failed')",
            name="ck_artifact_processings_outcome",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('processed', 'reused')", name="ck_artifact_processings_mode"
        ),
    )
    op.create_index(
        "ix_artifact_processings_run", "artifact_processings", ["processing_run_id", "locator"]
    )
    op.create_table(
        "chunks",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "artifact_processing_id",
            uuid,
            sa.ForeignKey("artifact_processings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stable_key", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("unit_anchor", sa.Text(), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("start_column", sa.Integer(), nullable=False),
        sa.Column("end_column", sa.Integer(), nullable=False),
        sa.Column("symbol_id", uuid),
        sa.Column("symbol_identity_id", uuid),
        sa.Column("heading_path", postgresql.ARRAY(sa.Text())),
        sa.Column("json_path", sa.Text()),
        sa.UniqueConstraint(
            "artifact_processing_id", "ordinal", name="uq_chunks_processing_ordinal"
        ),
        sa.UniqueConstraint(
            "artifact_processing_id", "stable_key", name="uq_chunks_processing_stable_key"
        ),
        sa.CheckConstraint("ordinal >= 0 AND part_index >= 0", name="ck_chunks_ordinals"),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset >= start_offset", name="ck_chunks_offsets"
        ),
        sa.CheckConstraint("start_line >= 1 AND end_line >= start_line", name="ck_chunks_lines"),
    )
    op.create_index("ix_chunks_processing_order", "chunks", ["artifact_processing_id", "ordinal"])
    op.create_index("ix_chunks_stable_key", "chunks", ["stable_key"])
    op.add_column("project_versions", sa.Column("active_processing_run_id", uuid))
    op.create_foreign_key(
        "fk_project_versions_active_processing_owner",
        "project_versions",
        "processing_runs",
        ["active_processing_run_id", "id"],
        ["id", "project_version_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_project_versions_active_processing_owner", "project_versions", type_="foreignkey"
    )
    op.drop_column("project_versions", "active_processing_run_id")
    op.drop_index("ix_chunks_stable_key", table_name="chunks")
    op.drop_index("ix_chunks_processing_order", table_name="chunks")
    op.drop_table("chunks")
    op.drop_index("ix_artifact_processings_run", table_name="artifact_processings")
    op.drop_table("artifact_processings")
    op.drop_index("ix_processing_runs_version_revision", table_name="processing_runs")
    op.drop_table("processing_runs")
