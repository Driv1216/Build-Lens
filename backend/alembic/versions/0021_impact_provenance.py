"""Seal new impact preparations without trusting or rewriting legacy provenance."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0021_impact_provenance"
down_revision = "0020_impact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("impact_preparations", sa.Column("source_identity", JSONB, nullable=True))
    op.add_column(
        "impact_preparations", sa.Column("input_seal_digest", sa.String(64), nullable=True)
    )
    op.drop_constraint("uq_impact_preparations_input", "impact_preparations", type_="unique")
    op.create_index(
        "uq_impact_preparations_sealed_input",
        "impact_preparations",
        ["project_id", "locator", "fingerprint", "contract"],
        unique=True,
        postgresql_where=sa.text("input_seal_digest IS NOT NULL"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM impact_preparations LIMIT 1")).first():
        raise RuntimeError("Retained preparation provenance must be preserved before downgrade.")
    op.drop_index("uq_impact_preparations_sealed_input", table_name="impact_preparations")
    op.create_unique_constraint(
        "uq_impact_preparations_input",
        "impact_preparations",
        ["project_id", "locator", "fingerprint", "contract"],
    )
    op.drop_column("impact_preparations", "input_seal_digest")
    op.drop_column("impact_preparations", "source_identity")
