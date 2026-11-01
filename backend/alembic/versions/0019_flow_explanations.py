"""Stage 15 Milestone E: plan-gated explanations of pinned flow traces (INV-8).

Additive only: six new tables, no change to any existing table or column (INV-3).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0019_flow_explanations"
down_revision = "0018_flow_model"
branch_labels = None
depends_on = None

_HEX = "~ '^[0-9a-f]{64}$'"
_JSON = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "flow_explanation_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("project_version_id", sa.Uuid(), nullable=False),
        sa.Column("flow_run_id", sa.Uuid(), nullable=False),
        sa.Column("trace_digest", sa.String(length=64), nullable=False),
        sa.Column("package_digest", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("request_bytes", sa.Integer(), nullable=False),
        sa.Column("config_digest", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("retention_note", sa.Text(), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("contract", _JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.CheckConstraint("state IN ('planned', 'used')", name="ck_flow_explanation_plans_state"),
        sa.CheckConstraint(f"package_digest {_HEX}", name="ck_flow_explanation_plans_package"),
        sa.CheckConstraint(f"request_digest {_HEX}", name="ck_flow_explanation_plans_request"),
        sa.CheckConstraint(f"plan_digest {_HEX}", name="ck_flow_explanation_plans_digest"),
        sa.CheckConstraint("request_bytes > 0", name="ck_flow_explanation_plans_bytes"),
        sa.ForeignKeyConstraint(
            ["flow_run_id", "trace_digest"],
            ["flow_trace_records.flow_run_id", "flow_trace_records.trace_digest"],
            name="fk_flow_explanation_plans_trace",
        ),
        sa.ForeignKeyConstraint(
            ["project_version_id"],
            ["project_versions.id"],
            name="fk_flow_explanation_plans_version",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "flow_explanation_items",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("trace_digest", sa.String(length=64), nullable=False),
        sa.Column("package", _JSON, nullable=False),
        sa.Column("package_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(f"package_digest {_HEX}", name="ck_flow_explanation_items_package"),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["flow_explanation_plans.id"], name="fk_flow_explanation_items_plan"
        ),
        sa.PrimaryKeyConstraint("plan_id", "ordinal"),
    )
    op.create_table(
        "flow_explanation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("project_version_id", sa.Uuid(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('running', 'complete', 'failed', 'abandoned')",
            name="ck_flow_explanation_runs_state",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["flow_explanation_plans.id"], name="fk_flow_explanation_runs_plan"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", name="uq_flow_explanation_runs_plan"),
    )
    op.create_table(
        "flow_explanation_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"request_digest {_HEX}", name="ck_flow_explanation_attempts_request"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["flow_explanation_runs.id"], name="fk_flow_explanation_attempts_run"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "ordinal", name="uq_flow_explanation_attempts_ordinal"),
    )
    op.create_table(
        "flow_explanation_outcomes",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("validator", sa.String(length=32), nullable=False),
        sa.Column("returned_model", sa.String(length=200), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("response_digest", sa.String(length=64), nullable=True),
        sa.Column("rejection_codes", _JSON, nullable=False),
        sa.Column("unknowns", _JSON, nullable=False),
        sa.Column("diagnostics", _JSON, nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["flow_explanation_attempts.id"],
            name="fk_flow_explanation_outcomes_attempt",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_table(
        "flow_explanation_claims",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("steps", _JSON, nullable=False),
        sa.Column("citations", _JSON, nullable=False),
        sa.Column("claim_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(f"claim_digest {_HEX}", name="ck_flow_explanation_claims_digest"),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["flow_explanation_outcomes.attempt_id"],
            name="fk_flow_explanation_claims_outcome",
        ),
        sa.PrimaryKeyConstraint("attempt_id", "ordinal"),
    )


def downgrade() -> None:
    used = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM flow_explanation_plans")).scalar_one()
    )
    if used:
        raise RuntimeError(
            "Stage 15 flow explanation plans exist; downgrading would discard them. "
            "Restore a verified pre-upgrade backup instead."
        )
    op.drop_table("flow_explanation_claims")
    op.drop_table("flow_explanation_outcomes")
    op.drop_table("flow_explanation_attempts")
    op.drop_table("flow_explanation_runs")
    op.drop_table("flow_explanation_items")
    op.drop_table("flow_explanation_plans")
