"""Persist immutable embedding evidence and provision pgvector 0.8.6."""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008_persistent_vector_evidence"
down_revision = "0007_evidence_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE available_version text; installed_version text; installed_schema text;
        BEGIN
          SELECT default_version INTO available_version
          FROM pg_available_extensions WHERE name = 'vector';
          IF available_version IS NULL THEN
            RAISE EXCEPTION 'BuildLens requires pgvector 0.8.6 binaries on PostgreSQL 16';
          END IF;
          SELECT e.extversion, n.nspname INTO installed_version, installed_schema
          FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
          WHERE e.extname = 'vector';
          IF installed_version IS NULL THEN
            EXECUTE 'CREATE EXTENSION vector WITH SCHEMA public VERSION ''0.8.6''';
            installed_version := '0.8.6'; installed_schema := 'public';
          END IF;
          IF installed_version <> '0.8.6' OR installed_schema <> 'public' THEN
            RAISE EXCEPTION 'BuildLens requires vector 0.8.6 installed in public (found %.%)',
              installed_schema, installed_version;
          END IF;
        END $$
        """
    )
    op.add_column("project_versions", sa.Column("active_embedding_run_id", sa.Uuid()))
    op.create_unique_constraint(
        "uq_chunks_embedding_identity",
        "chunks",
        ["id", "artifact_processing_id", "stable_key", "content_hash"],
    )
    op.create_unique_constraint(
        "uq_processing_runs_embedding_owner",
        "processing_runs",
        ["id", "project_version_id", "revision"],
    )
    op.create_table(
        "embedding_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("project_version_id", sa.Uuid(), nullable=False),
        sa.Column("processing_run_id", sa.Uuid(), nullable=False),
        sa.Column("processing_revision", sa.Integer(), nullable=False),
        sa.Column("processing_contract", postgresql.JSONB(), nullable=False),
        sa.Column("embedding_contract", postgresql.JSONB(), nullable=False),
        sa.Column("contract_digest", sa.String(64), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("superseded", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("quarantined", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("captured_active_embedding_run_id", sa.Uuid()),
        sa.Column("captured_latest_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("membership_digest", sa.String(64), nullable=False),
        sa.Column("limits", postgresql.JSONB(), nullable=False),
        sa.Column("coverage", postgresql.JSONB(), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint(
            "project_version_id", "revision", name="uq_embedding_runs_snapshot_revision"
        ),
        sa.UniqueConstraint("id", "project_version_id", name="uq_embedding_runs_id_snapshot"),
        sa.UniqueConstraint(
            "id",
            "project_id",
            "contract_digest",
            "dimensions",
            name="uq_embedding_runs_reuse_owner",
        ),
        sa.UniqueConstraint(
            "id",
            "project_id",
            "project_version_id",
            "processing_run_id",
            "contract_digest",
            "dimensions",
            name="uq_embedding_runs_row_owner",
        ),
        sa.ForeignKeyConstraint(
            ["project_version_id", "project_id"],
            ["project_versions.id", "project_versions.project_id"],
            ondelete="CASCADE",
            name="fk_embedding_run_snapshot_owner",
        ),
        sa.ForeignKeyConstraint(
            ["processing_run_id", "project_version_id", "processing_revision"],
            [
                "processing_runs.id",
                "processing_runs.project_version_id",
                "processing_runs.revision",
            ],
            name="fk_embedding_run_processing_owner",
        ),
        sa.CheckConstraint("revision > 0", name="ck_embedding_runs_revision"),
        sa.CheckConstraint("dimensions > 0", name="ck_embedding_runs_dimensions"),
        sa.CheckConstraint("outcome IN ('complete', 'failed')", name="ck_embedding_runs_outcome"),
        sa.CheckConstraint("completed_at >= started_at", name="ck_embedding_runs_times"),
        sa.CheckConstraint(
            "contract_digest ~ '^[0-9a-f]{64}$' AND membership_digest ~ '^[0-9a-f]{64}$'",
            name="ck_embedding_runs_hashes",
        ),
    )
    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("embedding_run_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("project_version_id", sa.Uuid(), nullable=False),
        sa.Column("processing_run_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_processing_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("stable_chunk_key", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("logical_embedding_key", sa.String(64), nullable=False),
        sa.Column("contract_digest", sa.String(64), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("window_count", sa.Integer(), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("reuse_source_embedding_id", sa.Uuid()),
        sa.UniqueConstraint("embedding_run_id", "chunk_id", name="uq_chunk_embeddings_run_chunk"),
        sa.UniqueConstraint(
            "id",
            "project_id",
            "contract_digest",
            "dimensions",
            "stable_chunk_key",
            "input_hash",
            name="uq_chunk_embeddings_reuse_source",
        ),
        sa.ForeignKeyConstraint(
            ["embedding_run_id", "project_version_id"],
            ["embedding_runs.id", "embedding_runs.project_version_id"],
            ondelete="CASCADE",
            name="fk_chunk_embedding_run_owner",
        ),
        sa.ForeignKeyConstraint(
            ["embedding_run_id", "project_id", "contract_digest", "dimensions"],
            [
                "embedding_runs.id",
                "embedding_runs.project_id",
                "embedding_runs.contract_digest",
                "embedding_runs.dimensions",
            ],
            name="fk_chunk_embedding_contract_owner",
        ),
        sa.ForeignKeyConstraint(
            [
                "embedding_run_id",
                "project_id",
                "project_version_id",
                "processing_run_id",
                "contract_digest",
                "dimensions",
            ],
            [
                "embedding_runs.id",
                "embedding_runs.project_id",
                "embedding_runs.project_version_id",
                "embedding_runs.processing_run_id",
                "embedding_runs.contract_digest",
                "embedding_runs.dimensions",
            ],
            name="fk_chunk_embedding_complete_owner",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_processing_id", "processing_run_id"],
            ["artifact_processings.id", "artifact_processings.processing_run_id"],
            name="fk_chunk_embedding_processing_owner",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id", "artifact_processing_id", "stable_chunk_key", "input_hash"],
            [
                "chunks.id",
                "chunks.artifact_processing_id",
                "chunks.stable_key",
                "chunks.content_hash",
            ],
            name="fk_chunk_embedding_chunk_identity",
        ),
        sa.ForeignKeyConstraint(
            [
                "reuse_source_embedding_id",
                "project_id",
                "contract_digest",
                "dimensions",
                "stable_chunk_key",
                "input_hash",
            ],
            [
                "chunk_embeddings.id",
                "chunk_embeddings.project_id",
                "chunk_embeddings.contract_digest",
                "chunk_embeddings.dimensions",
                "chunk_embeddings.stable_chunk_key",
                "chunk_embeddings.input_hash",
            ],
            name="fk_chunk_embedding_reuse_source",
        ),
        sa.CheckConstraint(
            "dimensions > 0 AND window_count > 0", name="ck_chunk_embeddings_positive"
        ),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$' AND logical_embedding_key ~ '^[0-9a-f]{64}$' "
            "AND contract_digest ~ '^[0-9a-f]{64}$'",
            name="ck_chunk_embeddings_hashes",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('generated', 'reused')", name="ck_chunk_embeddings_mode"
        ),
        sa.CheckConstraint(
            "(execution_mode = 'reused') = (reuse_source_embedding_id IS NOT NULL)",
            name="ck_chunk_embeddings_reuse_provenance",
        ),
        sa.CheckConstraint("vector_dims(embedding) = dimensions", name="ck_chunk_embeddings_dims"),
        sa.CheckConstraint(
            "abs(vector_norm(embedding) - 1.0) <= 0.0001",
            name="ck_chunk_embeddings_unit_norm",
        ),
    )
    op.create_foreign_key(
        "fk_project_versions_active_embedding_owner",
        "project_versions",
        "embedding_runs",
        ["active_embedding_run_id", "id"],
        ["id", "project_version_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_embedding_runs_captured_active",
        "embedding_runs",
        "embedding_runs",
        ["captured_active_embedding_run_id", "project_version_id"],
        ["id", "project_version_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index(
        "ix_embedding_runs_snapshot_revision",
        "embedding_runs",
        ["project_version_id", sa.text("revision DESC")],
    )
    op.create_index(
        "ix_embedding_runs_project_contract", "embedding_runs", ["project_id", "contract_digest"]
    )
    op.create_index("ix_chunk_embeddings_run", "chunk_embeddings", ["embedding_run_id"])
    op.create_index(
        "ix_chunk_embeddings_logical_reuse",
        "chunk_embeddings",
        ["project_id", "logical_embedding_key", "contract_digest"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM embedding_runs)")):
        raise RuntimeError(
            "Refusing to remove retained embedding history; restore a compatible backup."
        )
    op.drop_constraint(
        "fk_project_versions_active_embedding_owner", "project_versions", type_="foreignkey"
    )
    op.drop_table("chunk_embeddings")
    op.drop_table("embedding_runs")
    # IF EXISTS also supports development databases that briefly ran an earlier draft of 0008.
    op.execute(
        "ALTER TABLE processing_runs DROP CONSTRAINT IF EXISTS uq_processing_runs_embedding_owner"
    )
    op.drop_constraint("uq_chunks_embedding_identity", "chunks", type_="unique")
    op.drop_column("project_versions", "active_embedding_run_id")
