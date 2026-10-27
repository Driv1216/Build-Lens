"""Stage 13 Milestone B: deterministic semantic catalog (additive; no AI text, no backfill)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import SchemaItem

from alembic import op

revision = "0011_semantic_catalog"
down_revision = "0010_multilanguage_structure"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)


def _owned_by_catalog() -> list[SchemaItem]:
    return [
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("catalog_run_id", _UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_run_id"], ["semantic_catalog_runs.id"], ondelete="CASCADE"
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "semantic_catalog_runs",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("project_id", _UUID, nullable=False),
        sa.Column("project_version_id", _UUID, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("structural_analysis_id", _UUID, nullable=False),
        sa.Column("structural_revision", sa.Integer(), nullable=False),
        sa.Column("processing_run_id", _UUID, nullable=False),
        sa.Column("processing_revision", sa.Integer(), nullable=False),
        sa.Column("processing_contract", postgresql.JSONB(), nullable=False),
        sa.Column("catalog_contract", postgresql.JSONB(), nullable=False),
        sa.Column("contract_digest", sa.String(64), nullable=False),
        sa.Column("input_authority_digest", sa.String(64), nullable=False),
        sa.Column("catalog_digest", sa.String(64), nullable=True),
        sa.Column("portable_digest", sa.String(64), nullable=True),
        sa.Column("coverage", postgresql.JSONB(), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
        sa.Column("superseded", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("quarantined", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("captured_active_catalog_run_id", _UUID, nullable=True),
        sa.Column("captured_latest_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "project_version_id", "revision", name="uq_semantic_catalog_runs_snapshot_revision"
        ),
        sa.UniqueConstraint(
            "id", "project_version_id", name="uq_semantic_catalog_runs_id_snapshot"
        ),
        sa.ForeignKeyConstraint(
            ["project_version_id", "project_id"],
            ["project_versions.id", "project_versions.project_id"],
            ondelete="CASCADE",
            name="fk_semantic_catalog_run_snapshot_owner",
        ),
        sa.ForeignKeyConstraint(
            ["structural_analysis_id", "project_version_id"],
            ["structural_analyses.id", "structural_analyses.project_version_id"],
            name="fk_semantic_catalog_run_structure",
        ),
        sa.ForeignKeyConstraint(
            ["processing_run_id", "project_version_id", "processing_revision"],
            [
                "processing_runs.id",
                "processing_runs.project_version_id",
                "processing_runs.revision",
            ],
            name="fk_semantic_catalog_run_processing",
        ),
        sa.ForeignKeyConstraint(
            ["captured_active_catalog_run_id", "project_version_id"],
            ["semantic_catalog_runs.id", "semantic_catalog_runs.project_version_id"],
            name="fk_semantic_catalog_runs_captured_active",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("revision > 0", name="ck_semantic_catalog_runs_revision"),
        sa.CheckConstraint(
            "outcome IN ('complete', 'partial', 'failed')",
            name="ck_semantic_catalog_runs_outcome",
        ),
        sa.CheckConstraint("completed_at >= started_at", name="ck_semantic_catalog_runs_times"),
        sa.CheckConstraint(
            "input_authority_digest ~ '^[0-9a-f]{64}$' AND contract_digest ~ '^[0-9a-f]{64}$'",
            name="ck_semantic_catalog_runs_authority",
        ),
        sa.CheckConstraint(
            "(outcome = 'failed') = (catalog_digest IS NULL) AND "
            "(catalog_digest IS NULL) = (portable_digest IS NULL) AND "
            "(catalog_digest IS NULL OR (catalog_digest ~ '^[0-9a-f]{64}$' "
            "AND portable_digest ~ '^[0-9a-f]{64}$'))",
            name="ck_semantic_catalog_runs_digests",
        ),
    )
    op.create_index(
        "ix_semantic_catalog_runs_snapshot",
        "semantic_catalog_runs",
        ["project_version_id", "revision"],
    )

    op.create_table(
        "semantic_entities",
        *_owned_by_catalog(),
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("artifact_identity_id", _UUID, nullable=True),
        sa.Column("symbol_identity_id", _UUID, nullable=True),
        sa.Column("subject_entity_key", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=True),
        sa.Column("symbol_kind", sa.String(16), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("portable_key", sa.Text(), nullable=False),
        sa.Column("stored_coverage", sa.String(32), nullable=False),
        sa.Column("coverage_reason", sa.String(64), nullable=True),
        sa.Column("context_coverage", sa.String(16), nullable=False),
        sa.Column("context_reason", sa.String(64), nullable=True),
        sa.Column("package_digest", sa.String(64), nullable=True),
        sa.Column("package_bytes", sa.Integer(), nullable=False),
        sa.Column("limitations", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("package", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("catalog_run_id", "entity_key", name="uq_semantic_entities_run_key"),
        sa.ForeignKeyConstraint(
            ["artifact_identity_id"],
            ["artifact_identities.id"],
            name="fk_semantic_entity_artifact_identity",
        ),
        sa.ForeignKeyConstraint(
            ["symbol_identity_id"],
            ["symbol_identities.id"],
            name="fk_semantic_entity_symbol_identity",
        ),
        sa.CheckConstraint(
            "kind IN ('file', 'symbol', 'component', 'hook', 'api_endpoint', 'db_table')",
            name="ck_semantic_entities_kind",
        ),
        sa.CheckConstraint(
            "stored_coverage IN ('complete', 'incomplete', 'unavailable', 'coordinate_mismatch')",
            name="ck_semantic_entities_stored_coverage",
        ),
        sa.CheckConstraint(
            "context_coverage IN ('full', 'outline', 'not_eligible')",
            name="ck_semantic_entities_context_coverage",
        ),
        sa.CheckConstraint(
            "(context_coverage = 'not_eligible') = (package_digest IS NULL) AND "
            "(package_digest IS NULL OR package_digest ~ '^[0-9a-f]{64}$')",
            name="ck_semantic_entities_package",
        ),
        sa.CheckConstraint(
            "context_coverage = 'not_eligible' OR stored_coverage = 'complete'",
            name="ck_semantic_entities_eligibility",
        ),
        sa.CheckConstraint(
            "package_bytes >= 0 AND ordinal >= 0", name="ck_semantic_entities_sizes"
        ),
    )
    op.create_index(
        "ix_semantic_entities_order", "semantic_entities", ["catalog_run_id", "locator", "ordinal"]
    )

    op.create_table(
        "semantic_facts",
        *_owned_by_catalog(),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("subject_entity_key", sa.Text(), nullable=False),
        sa.Column("fact_kind", sa.String(32), nullable=False),
        sa.Column("source_refs", postgresql.JSONB(), nullable=False),
        sa.Column("derived", postgresql.JSONB(), nullable=False),
        sa.Column("renderer_version", sa.String(64), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column("fact_digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("catalog_run_id", "fact_digest", name="uq_semantic_facts_run_digest"),
        sa.CheckConstraint(
            "fact_kind IN ('signature', 'import', 'export', 'reference', 'base', "
            "'recognizer_match', 'relation', 'limitation')",
            name="ck_semantic_facts_kind",
        ),
        sa.CheckConstraint("fact_digest ~ '^[0-9a-f]{64}$'", name="ck_semantic_facts_digest"),
        sa.CheckConstraint(
            "jsonb_typeof(source_refs) = 'array' AND jsonb_array_length(source_refs) > 0",
            name="ck_semantic_facts_provenance",
        ),
    )
    op.create_index(
        "ix_semantic_facts_subject", "semantic_facts", ["catalog_run_id", "subject_entity_key"]
    )

    op.create_table(
        "semantic_relations",
        *_owned_by_catalog(),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source_entity_key", sa.Text(), nullable=False),
        sa.Column("target_entity_key", sa.Text(), nullable=True),
        sa.Column("target_text", sa.Text(), nullable=True),
        sa.Column("resolution_status", sa.String(16), nullable=True),
        sa.Column("fact_digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("catalog_run_id", "ordinal", name="uq_semantic_relations_run_ordinal"),
        sa.ForeignKeyConstraint(
            ["catalog_run_id", "fact_digest"],
            ["semantic_facts.catalog_run_id", "semantic_facts.fact_digest"],
            name="fk_semantic_relation_fact",
        ),
        sa.CheckConstraint(
            "kind IN ('contains', 'imports', 'exports', 'references', 'renders', 'uses_hook', "
            "'inherits', 'defines_route', 'maps_table')",
            name="ck_semantic_relations_kind",
        ),
        sa.CheckConstraint(
            "resolution_status IS NULL OR resolution_status IN ('resolved', 'ambiguous', "
            "'not_found', 'external', 'not_attempted', 'same_file', 'imported', 'unresolved')",
            name="ck_semantic_relations_resolution",
        ),
        sa.CheckConstraint(
            "(target_entity_key IS NULL) <> (target_text IS NULL)",
            name="ck_semantic_relations_target",
        ),
    )
    op.create_index(
        "ix_semantic_relations_source",
        "semantic_relations",
        ["catalog_run_id", "source_entity_key"],
    )
    op.create_index(
        "ix_semantic_relations_target",
        "semantic_relations",
        ["catalog_run_id", "target_entity_key"],
    )

    op.add_column(
        "project_versions", sa.Column("active_semantic_catalog_run_id", _UUID, nullable=True)
    )
    op.create_foreign_key(
        "fk_project_versions_active_semantic_catalog",
        "project_versions",
        "semantic_catalog_runs",
        ["active_semantic_catalog_run_id", "id"],
        ["id", "project_version_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    used = op.get_bind().execute(sa.text("SELECT count(*) FROM semantic_catalog_runs")).scalar_one()
    if used:
        raise RuntimeError(
            "Stage 13 semantic catalog runs exist; downgrading would discard them. "
            "Restore a verified pre-upgrade backup instead."
        )
    op.drop_constraint(
        "fk_project_versions_active_semantic_catalog", "project_versions", type_="foreignkey"
    )
    op.drop_column("project_versions", "active_semantic_catalog_run_id")
    op.drop_table("semantic_relations")
    op.drop_table("semantic_facts")
    op.drop_table("semantic_entities")
    op.drop_table("semantic_catalog_runs")
