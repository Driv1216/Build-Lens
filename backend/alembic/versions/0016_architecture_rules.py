"""Stage 14 Milestone C: owner-declared architecture rule sets (additive, append-only)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016_architecture_rules"
down_revision = "0015_architecture_literals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "architecture_rule_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("contract", sa.String(length=64), nullable=False),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_architecture_rule_sets_version"),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_architecture_rule_sets_digest"),
        sa.CheckConstraint("jsonb_typeof(rules) = 'array'", name="ck_architecture_rule_sets_rules"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "version", name="uq_architecture_rule_sets_version"),
    )


def downgrade() -> None:
    used = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM architecture_rule_sets")).scalar_one()
    )
    if used:
        raise RuntimeError(
            "Stage 14 architecture rule sets exist; downgrading would discard them. "
            "Restore a verified pre-upgrade backup instead."
        )
    op.drop_table("architecture_rule_sets")
