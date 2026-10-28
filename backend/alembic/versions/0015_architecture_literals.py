"""Stage 14 Milestone B: sanitised architecture literal rows (additive extractor evidence)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015_architecture_literals"
down_revision = "0014_architecture_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "architecture_literals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("start_column", sa.Integer(), nullable=True),
        sa.Column("end_column", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('app_or_router', 'router_mount', 'migration_table', 'http_call', "
            "'url_call', 'import_guard', 'type_import', 'url_constant', 'url_helper', "
            "'frontend_route', 'opaque_dynamic_import')",
            name="ck_architecture_literals_kind",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_architecture_literals_ordinal"),
        sa.CheckConstraint(
            "subject IS NULL OR char_length(subject) BETWEEN 1 AND 256",
            name="ck_architecture_literals_subject",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'", name="ck_architecture_literals_attributes"
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id", "artifact_analysis_id"],
            ["artifact_extractions.id", "artifact_extractions.artifact_analysis_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("extraction_id", "ordinal", name="uq_architecture_literals_ordinal"),
    )
    # Architecture evidence may now cite literal rows.
    op.drop_constraint("ck_architecture_evidence_kind", "architecture_evidence", type_="check")
    op.create_check_constraint(
        "ck_architecture_evidence_kind",
        "architecture_evidence",
        "kind IN ('import_statements', 'inventory_artifact', 'derivation', 'literal_rows')",
    )


def downgrade() -> None:
    used = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT (SELECT count(*) FROM architecture_literals) + (SELECT count(*) FROM "
                "architecture_evidence WHERE kind = 'literal_rows')"
            )
        )
        .scalar_one()
    )
    if used:
        raise RuntimeError(
            "Stage 14 architecture literal rows exist; downgrading would discard them. "
            "Restore a verified pre-upgrade backup instead."
        )
    op.drop_constraint("ck_architecture_evidence_kind", "architecture_evidence", type_="check")
    op.create_check_constraint(
        "ck_architecture_evidence_kind",
        "architecture_evidence",
        "kind IN ('import_statements', 'inventory_artifact', 'derivation')",
    )
    op.drop_table("architecture_literals")
