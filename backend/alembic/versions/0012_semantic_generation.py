"""Stage 13 Milestone C: controlled semantic generation (additive; plans, runs, candidates)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_semantic_generation"
down_revision = "0011_semantic_catalog"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_NOW = sa.text("now()")


def _snapshot_owner(name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["project_version_id", "project_id"],
        ["project_versions.id", "project_versions.project_id"],
        ondelete="CASCADE",
        name=name,
    )


def upgrade() -> None:
    # Catalog v2: a cross-snapshot content projection per entity keys source freshness.
    op.add_column("semantic_entities", sa.Column("content_digest", sa.String(64), nullable=True))
    op.create_table(
        "semantic_generation_plans",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("project_id", _UUID, nullable=False),
        sa.Column("project_version_id", _UUID, nullable=False),
        sa.Column("plan_nonce", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selection", postgresql.JSONB(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("accept_profiles", postgresql.JSONB(), nullable=True),
        sa.Column("provider_config", postgresql.JSONB(), nullable=False),
        sa.Column("provider_config_digest", sa.String(64), nullable=False),
        sa.Column("generator_profile_digest", sa.String(64), nullable=False),
        sa.Column("sharing_class", sa.String(16), nullable=False),
        sa.Column("catalog_run_id", _UUID, nullable=False),
        sa.Column("structural_analysis_id", _UUID, nullable=False),
        sa.Column("processing_run_id", _UUID, nullable=False),
        sa.Column("index_run_id", _UUID, nullable=True),
        sa.Column("caps", postgresql.JSONB(), nullable=False),
        sa.Column("totals", postgresql.JSONB(), nullable=False),
        sa.Column("contributing_locators", postgresql.JSONB(), nullable=False),
        sa.Column("retention_note", sa.Text(), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("plan_nonce", name="uq_semantic_generation_plans_nonce"),
        sa.UniqueConstraint(
            "id", "project_version_id", name="uq_semantic_generation_plans_id_snapshot"
        ),
        _snapshot_owner("fk_semantic_generation_plan_snapshot_owner"),
        sa.ForeignKeyConstraint(
            ["catalog_run_id", "project_version_id"],
            ["semantic_catalog_runs.id", "semantic_catalog_runs.project_version_id"],
            name="fk_semantic_generation_plan_catalog",
        ),
        sa.CheckConstraint("expires_at > issued_at", name="ck_semantic_generation_plans_expiry"),
        sa.CheckConstraint(
            "mode IN ('generate', 'reuse_only')", name="ck_semantic_generation_plans_mode"
        ),
        sa.CheckConstraint(
            "sharing_class IN ('cloud', 'loopback')", name="ck_semantic_generation_plans_sharing"
        ),
        sa.CheckConstraint(
            "plan_digest ~ '^[0-9a-f]{64}$' AND provider_config_digest ~ '^[0-9a-f]{64}$' "
            "AND generator_profile_digest ~ '^[0-9a-f]{64}$'",
            name="ck_semantic_generation_plans_digests",
        ),
    )
    op.create_index(
        "ix_semantic_generation_plans_snapshot",
        "semantic_generation_plans",
        ["project_version_id", "issued_at"],
    )

    op.create_table(
        "semantic_generation_plan_items",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("plan_id", _UUID, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("context_coverage", sa.String(16), nullable=False),
        sa.Column("package_digest", sa.String(64), nullable=True),
        sa.Column("input_key", sa.String(64), nullable=True),
        sa.Column("request_digest", sa.String(64), nullable=True),
        sa.Column("request_bytes", sa.Integer(), nullable=False),
        sa.Column("contributing_locators", postgresql.JSONB(), nullable=False),
        sa.Column("reuse_interpretation_id", _UUID, nullable=True),
        sa.Column("reuse_profile_digest", sa.String(64), nullable=True),
        sa.Column("reuse_provider", sa.String(16), nullable=True),
        sa.Column("reuse_returned_model", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "plan_id", "entity_key", name="uq_semantic_generation_plan_items_entity"
        ),
        sa.UniqueConstraint("plan_id", "ordinal", name="uq_semantic_generation_plan_items_ordinal"),
        sa.ForeignKeyConstraint(["plan_id"], ["semantic_generation_plans.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "disposition IN ('generate', 'reuse', 'ineligible')",
            name="ck_semantic_generation_plan_items_disposition",
        ),
        sa.CheckConstraint(
            "disposition <> 'generate' OR (request_digest ~ '^[0-9a-f]{64}$' "
            "AND package_digest IS NOT NULL AND input_key IS NOT NULL)",
            name="ck_semantic_generation_plan_items_request",
        ),
        sa.CheckConstraint(
            "(disposition = 'reuse') = (reuse_interpretation_id IS NOT NULL)",
            name="ck_semantic_generation_plan_items_reuse",
        ),
    )

    op.create_table(
        "semantic_generation_runs",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("project_id", _UUID, nullable=False),
        sa.Column("project_version_id", _UUID, nullable=False),
        sa.Column("plan_id", _UUID, nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("catalog_run_id", _UUID, nullable=False),
        sa.Column("structural_analysis_id", _UUID, nullable=False),
        sa.Column("processing_run_id", _UUID, nullable=False),
        sa.Column("captured_index_run_id", _UUID, nullable=True),
        sa.Column("caps", postgresql.JSONB(), nullable=False),
        sa.Column("attempts_started", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", _UUID, nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("totals", postgresql.JSONB(), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("plan_id", name="uq_semantic_generation_runs_plan"),
        sa.UniqueConstraint(
            "id", "project_version_id", name="uq_semantic_generation_runs_id_snapshot"
        ),
        sa.UniqueConstraint("id", "project_id", name="uq_semantic_generation_runs_id_project"),
        _snapshot_owner("fk_semantic_generation_run_snapshot_owner"),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_version_id"],
            ["semantic_generation_plans.id", "semantic_generation_plans.project_version_id"],
            name="fk_semantic_generation_run_plan",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_run_id", "project_version_id"],
            ["semantic_catalog_runs.id", "semantic_catalog_runs.project_version_id"],
            name="fk_semantic_generation_run_catalog",
        ),
        sa.ForeignKeyConstraint(
            ["captured_index_run_id", "project_version_id"],
            ["semantic_generation_runs.id", "semantic_generation_runs.project_version_id"],
            name="fk_semantic_generation_run_captured_index",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "state IN ('executing', 'complete', 'partial', 'failed', 'superseded', 'abandoned')",
            name="ck_semantic_generation_runs_state",
        ),
        sa.CheckConstraint(
            "(state = 'executing') = (completed_at IS NULL)",
            name="ck_semantic_generation_runs_terminal",
        ),
        sa.CheckConstraint(
            "attempts_started >= 0 AND deadline_at > started_at",
            name="ck_semantic_generation_runs_bounds",
        ),
    )
    op.create_index(
        "uq_semantic_generation_runs_one_executing",
        "semantic_generation_runs",
        ["project_version_id"],
        unique=True,
        postgresql_where=sa.text("state = 'executing'"),
    )

    op.create_table(
        "semantic_generation_attempts",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("run_id", _UUID, nullable=False),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("package_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("input_key", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.UniqueConstraint(
            "run_id", "entity_key", "attempt_no", name="uq_semantic_generation_attempts_slot"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["semantic_generation_runs.id"], ondelete="CASCADE"),
        sa.CheckConstraint("attempt_no >= 1", name="ck_semantic_generation_attempts_number"),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'", name="ck_semantic_generation_attempts_digest"
        ),
    )

    op.create_table(
        "semantic_generation_attempt_outcomes",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("attempt_id", _UUID, nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("returned_model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("response_digest", sa.String(64), nullable=True),
        sa.Column("rejection_codes", postgresql.JSONB(), nullable=False),
        sa.Column("retained_output", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.UniqueConstraint("attempt_id", name="uq_semantic_generation_outcomes_attempt"),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["semantic_generation_attempts.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'accepted_with_conflicts', 'insufficient_evidence', "
            "'rejected', 'rate_limited', 'provider_unavailable', 'timeout', 'context_rejected', "
            "'refused', 'token_bound_violated', 'reused_concurrently', 'outcome_unknown', "
            "'not_sent')",
            name="ck_semantic_generation_outcomes_code",
        ),
    )

    op.create_table(
        "semantic_generation_late_receipts",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("attempt_id", _UUID, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.Column("response_digest", sa.String(64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["semantic_generation_attempts.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "reason IN ('RUN_FENCED', 'RUN_TERMINAL')",
            name="ck_semantic_generation_late_receipts_reason",
        ),
    )

    op.create_table(
        "semantic_interpretations",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("project_id", _UUID, nullable=False),
        sa.Column("project_version_id", _UUID, nullable=False),
        sa.Column("run_id", _UUID, nullable=False),
        sa.Column("attempt_id", _UUID, nullable=False),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("input_key", sa.String(64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("generator_profile_digest", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("requested_model", sa.Text(), nullable=False),
        sa.Column("returned_model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("validator_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("unknowns", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_NOW),
        sa.UniqueConstraint("attempt_id", name="uq_semantic_interpretations_attempt"),
        sa.UniqueConstraint("id", "project_id", name="uq_semantic_interpretations_id_project"),
        sa.ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["semantic_generation_runs.id", "semantic_generation_runs.project_id"],
            ondelete="CASCADE",
            name="fk_semantic_interpretation_run",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["semantic_generation_attempts.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'accepted_with_conflicts', 'insufficient_evidence')",
            name="ck_semantic_interpretations_status",
        ),
        sa.CheckConstraint(
            "input_key ~ '^[0-9a-f]{64}$' AND generator_profile_digest ~ '^[0-9a-f]{64}$'",
            name="ck_semantic_interpretations_keys",
        ),
    )
    op.create_index(
        "ix_semantic_interpretations_reuse",
        "semantic_interpretations",
        ["project_id", "entity_key", "input_key", "generator_profile_digest"],
    )
    op.create_index(
        "ix_semantic_interpretations_input", "semantic_interpretations", ["project_id", "input_key"]
    )

    op.create_table(
        "semantic_claims",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("interpretation_id", _UUID, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("basis_label", sa.String(32), nullable=False),
        sa.Column("assertion", postgresql.JSONB(), nullable=True),
        sa.Column("assertion_outcome", sa.String(32), nullable=True),
        sa.Column("scope_warning", sa.Boolean(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("interpretation_id", "ordinal", name="uq_semantic_claims_ordinal"),
        sa.ForeignKeyConstraint(
            ["interpretation_id"], ["semantic_interpretations.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "category IN ('purpose', 'responsibility', 'behavior', 'side_effect', 'dependency', "
            "'usage_note')",
            name="ck_semantic_claims_category",
        ),
        sa.CheckConstraint(
            "status IN ('cited', 'conflicts_with_fact')", name="ck_semantic_claims_status"
        ),
        sa.CheckConstraint(
            "basis IN ('code', 'documentation', 'facts')", name="ck_semantic_claims_basis"
        ),
        sa.CheckConstraint(
            "octet_length(text) BETWEEN 1 AND 400 "
            "AND jsonb_array_length(citations) BETWEEN 1 AND 4",
            name="ck_semantic_claims_bounds",
        ),
    )

    op.create_table(
        "semantic_index_entries",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("run_id", _UUID, nullable=False),
        sa.Column("project_id", _UUID, nullable=False),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("interpretation_id", _UUID, nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("reused_from_profile", sa.String(64), nullable=True),
        sa.UniqueConstraint("run_id", "entity_key", name="uq_semantic_index_entries_entity"),
        sa.ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["semantic_generation_runs.id", "semantic_generation_runs.project_id"],
            ondelete="CASCADE",
            name="fk_semantic_index_entry_run",
        ),
        sa.ForeignKeyConstraint(
            ["interpretation_id", "project_id"],
            ["semantic_interpretations.id", "semantic_interpretations.project_id"],
            name="fk_semantic_index_entry_interpretation",
        ),
        sa.CheckConstraint(
            "mode IN ('generated', 'reused', 'retained')", name="ck_semantic_index_entries_mode"
        ),
    )

    op.add_column(
        "project_versions", sa.Column("active_semantic_index_run_id", _UUID, nullable=True)
    )
    op.create_foreign_key(
        "fk_project_versions_active_semantic_index",
        "project_versions",
        "semantic_generation_runs",
        ["active_semantic_index_run_id", "id"],
        ["id", "project_version_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    used = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT (SELECT count(*) FROM semantic_generation_plans) "
                "+ (SELECT count(*) FROM semantic_generation_runs)"
            )
        )
        .scalar_one()
    )
    if used:
        raise RuntimeError(
            "Stage 13 semantic generation plans or runs exist; downgrading would discard them. "
            "Restore a verified pre-upgrade backup instead."
        )
    op.drop_constraint(
        "fk_project_versions_active_semantic_index", "project_versions", type_="foreignkey"
    )
    op.drop_column("project_versions", "active_semantic_index_run_id")
    op.drop_column("semantic_entities", "content_digest")
    for table in (
        "semantic_index_entries",
        "semantic_claims",
        "semantic_interpretations",
        "semantic_generation_late_receipts",
        "semantic_generation_attempt_outcomes",
        "semantic_generation_attempts",
        "semantic_generation_runs",
        "semantic_generation_plan_items",
        "semantic_generation_plans",
    ):
        op.drop_table(table)
