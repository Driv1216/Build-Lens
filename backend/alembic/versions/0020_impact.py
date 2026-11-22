"""Add Stage 16 preparation, sealed flow slices and retained reports."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0020_impact"
down_revision = "0019_flow_explanations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "impact_preparations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("contract", sa.String(64), nullable=False),
        sa.Column("facts_digest", sa.String(64), nullable=False),
        sa.Column("facts", JSONB, nullable=False),
        sa.UniqueConstraint(
            "project_id", "locator", "fingerprint", "contract", name="uq_impact_preparations_input"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "project_id"],
            ["project_versions.id", "project_versions.project_id"],
            ondelete="CASCADE",
            name="fk_impact_preparation_owner",
        ),
    )
    op.create_table(
        "impact_flow_seals",
        sa.Column(
            "flow_run_id",
            sa.Uuid(),
            sa.ForeignKey("flow_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("graph_digest", sa.String(64), nullable=False),
        sa.Column("seal_digest", sa.String(64), nullable=False),
        sa.Column("shards", JSONB, nullable=False),
    )
    op.create_table(
        "impact_flow_seal_shards",
        sa.Column(
            "flow_run_id",
            sa.Uuid(),
            sa.ForeignKey("impact_flow_seals.flow_run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("shard", sa.Integer(), primary_key=True),
        sa.Column("members", JSONB, nullable=False),
    )
    op.create_table(
        "impact_reports",
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("result_digest", sa.String(64), primary_key=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("payload", JSONB, nullable=False),
    )


def downgrade() -> None:
    for table in (
        "impact_reports",
        "impact_flow_seal_shards",
        "impact_flow_seals",
        "impact_preparations",
    ):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("Stage 16 retained evidence must be preserved before downgrade.")
    for table in (
        "impact_reports",
        "impact_flow_seal_shards",
        "impact_flow_seals",
        "impact_preparations",
    ):
        op.drop_table(table)
