from datetime import datetime
from importlib import import_module
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import Uuid

Vector = cast(Any, import_module("pgvector.sqlalchemy").Vector)


class Base(DeclarativeBase):
    pass


class ProjectRecord(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("char_length(trim(name)) > 0", name="ck_projects_name_not_blank"),
        CheckConstraint("char_length(trim(root_path)) > 0", name="ck_projects_root_path_not_blank"),
        UniqueConstraint("root_path", name="uq_projects_root_path"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectVersionRecord(Base):
    __tablename__ = "project_versions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=False,
    )
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    canonical_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    scan_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scan_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    git_metadata: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    warnings: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)
    summary: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    sequence: Mapped[int] = mapped_column(nullable=False, default=0)
    predecessor_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=False
    )
    active_analysis_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    active_processing_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    active_embedding_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    root_observation: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    fingerprint_coverage: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    content_manifest: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    eligibility_provenance: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    comparison_contract: Mapped[str] = mapped_column(
        String(64), nullable=False, default="stage4.comparison.v1"
    )
    legacy_unverified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active_semantic_catalog_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    active_semantic_index_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )


class ArtifactIdentityRecord(Base):
    __tablename__ = "artifact_identities"
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_artifact_identities_id_project"),
        UniqueConstraint("project_id", "id", name="uq_artifact_identities_project_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=False,
    )
    scheme: Mapped[str] = mapped_column(String(64), nullable=False, default="artifact-path-slot.v1")


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("char_length(trim(kind)) > 0", name="ck_artifacts_kind_not_blank"),
        CheckConstraint("char_length(trim(locator)) > 0", name="ck_artifacts_locator_not_blank"),
        UniqueConstraint("id", "project_version_id", name="uq_artifacts_id_version"),
    )

    project_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("project_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=False,
    )
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    inventory_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    content_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ignore_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    exclusion_reasons: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    pruned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    stable_identity_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=False
    )
    continuity_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fingerprint_algorithm: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fingerprint_schema_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fingerprint_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="legacy_unverified"
    )
    fingerprint_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fingerprint_read_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fingerprint_read_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fingerprint_reader_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fingerprint_diagnostic: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SymbolIdentityRecord(Base):
    __tablename__ = "symbol_identities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["artifact_identity_id", "project_id"],
            ["artifact_identities.id", "artifact_identities.project_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=False)
    artifact_identity_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=False
    )
    parent_identity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scheme: Mapped[str] = mapped_column(
        String(64), nullable=False, default="python-declaration-slot.v1"
    )
    declaration_space: Mapped[str | None] = mapped_column(String(16), nullable=True)


class StructuralAnalysisRecord(Base):
    __tablename__ = "structural_analyses"
    __table_args__ = (
        UniqueConstraint(
            "project_version_id", "revision", name="uq_structural_analyses_version_revision"
        ),
        UniqueConstraint("id", "project_version_id", name="uq_structural_analyses_id_version"),
        CheckConstraint(
            "outcome IN ('complete', 'partial', 'failed')", name="ck_structural_analyses_outcome"
        ),
        CheckConstraint("completed_at >= started_at", name="ck_structural_analyses_times"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("project_versions.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    limits: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    warnings: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    captured_predecessor_analysis_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    producer_contract: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)

    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    captured_active_analysis_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    captured_latest_revision: Mapped[int] = mapped_column(nullable=False, server_default="0")


class ArtifactAnalysisRecord(Base):
    __tablename__ = "artifact_analyses"
    __table_args__ = (
        UniqueConstraint(
            "structural_analysis_id", "artifact_id", name="uq_artifact_analyses_analysis_artifact"
        ),
        UniqueConstraint("id", "structural_analysis_id", name="uq_artifact_analyses_id_analysis"),
        ForeignKeyConstraint(
            ["structural_analysis_id", "project_version_id"],
            ["structural_analyses.id", "structural_analyses.project_version_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["artifact_id", "project_version_id"],
            ["artifacts.id", "artifacts.project_version_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "outcome IN ('parsed', 'failed', 'skipped')", name="ck_artifact_analyses_outcome"
        ),
        CheckConstraint("line_count IS NULL OR line_count >= 0", name="ck_artifact_analyses_lines"),
        CheckConstraint(
            "outcome <> 'parsed' OR language IS NULL OR language = 'Python' "
            "OR declaration_extraction_id IS NOT NULL",
            name="ck_artifact_analyses_declaration_authority",
        ),
    )
    artifact_identity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    structural_analysis_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=False
    )
    project_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    read_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    encoding: Mapped[str | None] = mapped_column(String(64))
    line_count: Mapped[int | None] = mapped_column(nullable=True)
    is_package_initializer: Mapped[bool | None] = mapped_column(Boolean)
    diagnostics: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="parsed")
    baseline_artifact_analysis_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    reuse_source_artifact_analysis_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    declaration_extraction_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )


class SymbolRecord(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        UniqueConstraint("id", "artifact_analysis_id", name="uq_symbols_id_artifact_analysis"),
        ForeignKeyConstraint(
            ["artifact_analysis_id", "structural_analysis_id"],
            ["artifact_analyses.id", "artifact_analyses.structural_analysis_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["parent_id", "artifact_analysis_id"],
            ["symbols.id", "symbols.artifact_analysis_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('function', 'class', 'method', 'interface', 'type_alias', 'enum', "
            "'namespace')",
            name="ck_symbols_kind",
        ),
        CheckConstraint(
            "declaration_space IS NULL OR declaration_space IN ('value', 'type', 'namespace')",
            name="ck_symbols_declaration_space",
        ),
        CheckConstraint(
            "(start_offset IS NULL) = (end_offset IS NULL) AND "
            "(start_offset IS NULL OR (start_offset >= 0 AND end_offset >= start_offset))",
            name="ck_symbols_offsets",
        ),
        CheckConstraint("ordinal >= 0", name="ck_symbols_ordinal"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_symbols_not_self_parent"),
    )
    artifact_identity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    artifact_analysis_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=False
    )
    structural_analysis_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_async: Mapped[bool] = mapped_column(Boolean, nullable=False)
    start_line: Mapped[int] = mapped_column(nullable=False)
    end_line: Mapped[int] = mapped_column(nullable=False)
    start_column: Mapped[int | None] = mapped_column(nullable=True)
    end_column: Mapped[int | None] = mapped_column(nullable=True)
    full_start_line: Mapped[int | None] = mapped_column(nullable=True)
    signature: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    decorators: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    type_parameters: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    stable_identity_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=False
    )
    continuity_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    declaration_form: Mapped[str | None] = mapped_column(String(32), nullable=True)
    declaration_space: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exported: Mapped[str | None] = mapped_column(String(16), nullable=True)
    start_offset: Mapped[int | None] = mapped_column(nullable=True)
    end_offset: Mapped[int | None] = mapped_column(nullable=True)
    additional_spans: Mapped[list[list[int]] | None] = mapped_column(JSONB, nullable=True)


class ImportReferenceRecord(Base):
    __tablename__ = "import_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["artifact_analysis_id", "structural_analysis_id"],
            ["artifact_analyses.id", "artifact_analyses.structural_analysis_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_symbol_id", "artifact_analysis_id"],
            ["symbols.id", "symbols.artifact_analysis_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "form IN ('import', 'from', 'es_import', 'es_import_type', 'dynamic_import', "
            "'require')",
            name="ck_import_references_form",
        ),
        CheckConstraint("resolution_status = 'unresolved'", name="ck_import_references_unresolved"),
        CheckConstraint(
            "statement_ordinal >= 0 AND item_ordinal >= 0", name="ck_import_references_ordinals"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    artifact_analysis_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=False
    )
    structural_analysis_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_symbol_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    statement_ordinal: Mapped[int] = mapped_column(nullable=False)
    item_ordinal: Mapped[int] = mapped_column(nullable=False)
    form: Mapped[str] = mapped_column(String(16), nullable=False)
    from_module: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    alias: Mapped[str | None] = mapped_column(Text, nullable=True)
    level: Mapped[int] = mapped_column(nullable=False)
    start_line: Mapped[int] = mapped_column(nullable=False)
    end_line: Mapped[int] = mapped_column(nullable=False)
    start_column: Mapped[int | None] = mapped_column(nullable=True)
    end_column: Mapped[int | None] = mapped_column(nullable=True)
    resolution_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unresolved")
    resolution_reason: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_attempted"
    )


def _extraction_owner() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["extraction_id", "artifact_analysis_id"],
        ["artifact_extractions.id", "artifact_extractions.artifact_analysis_id"],
        ondelete="CASCADE",
    )


def _symbol_owner(column: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        [column, "artifact_analysis_id"],
        ["symbols.id", "symbols.artifact_analysis_id"],
        ondelete="CASCADE",
    )


class ArtifactExtractionRecord(Base):
    """Stage 13 additive extractor pass with its own producer authority."""

    __tablename__ = "artifact_extractions"
    __table_args__ = (
        UniqueConstraint(
            "artifact_analysis_id", "extractor", name="uq_artifact_extractions_analysis_extractor"
        ),
        UniqueConstraint("id", "artifact_analysis_id", name="uq_artifact_extractions_id_analysis"),
        ForeignKeyConstraint(
            ["artifact_analysis_id", "structural_analysis_id"],
            ["artifact_analyses.id", "artifact_analyses.structural_analysis_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["reuse_source_extraction_id"],
            ["artifact_extractions.id"],
            name="fk_artifact_extraction_reuse",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "outcome IN ('extracted', 'partial', 'failed', 'skipped')",
            name="ck_artifact_extractions_outcome",
        ),
        CheckConstraint(
            "execution_mode IN ('extracted', 'reused')", name="ck_artifact_extractions_mode"
        ),
        CheckConstraint(
            "(execution_mode = 'reused') = (reuse_source_extraction_id IS NOT NULL)",
            name="ck_artifact_extractions_reuse",
        ),
        CheckConstraint(
            "contract_digest ~ '^[0-9a-f]{64}$'", name="ck_artifact_extractions_contract_digest"
        ),
        Index("ix_artifact_extractions_analysis", "structural_analysis_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    artifact_analysis_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    structural_analysis_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    extractor: Mapped[str] = mapped_column(String(64), nullable=False)
    contract: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    reuse_source_extraction_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    diagnostics: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    artifact_facts: Mapped[dict[str, object] | None] = mapped_column(JSONB)


class SymbolFactsRecord(Base):
    __tablename__ = "symbol_facts"
    __table_args__ = (
        UniqueConstraint("extraction_id", "symbol_id", name="uq_symbol_facts_extraction_symbol"),
        _extraction_owner(),
        _symbol_owner("symbol_id"),
        Index("ix_symbol_facts_symbol", "symbol_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    extraction_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_analysis_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    symbol_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    facts: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class SymbolReferenceRecord(Base):
    __tablename__ = "symbol_references"
    __table_args__ = (
        UniqueConstraint("extraction_id", "ordinal", name="uq_symbol_references_ordinal"),
        _extraction_owner(),
        _symbol_owner("owner_symbol_id"),
        CheckConstraint(
            "kind IN ('call', 'base', 'decorator', 'jsx_element', 'hook_call', 'raise', "
            "'type_ref')",
            name="ck_symbol_references_kind",
        ),
        CheckConstraint(
            "char_length(target) BETWEEN 1 AND 512", name="ck_symbol_references_target"
        ),
        CheckConstraint("ordinal >= 0", name="ck_symbol_references_ordinal"),
        Index("ix_symbol_references_owner", "owner_symbol_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    extraction_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_analysis_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_symbol_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(nullable=False)
    end_line: Mapped[int] = mapped_column(nullable=False)
    start_column: Mapped[int | None] = mapped_column(nullable=True)
    end_column: Mapped[int | None] = mapped_column(nullable=True)


class ExportReferenceRecord(Base):
    __tablename__ = "export_references"
    __table_args__ = (
        UniqueConstraint("extraction_id", "ordinal", name="uq_export_references_ordinal"),
        _extraction_owner(),
        _symbol_owner("owner_symbol_id"),
        CheckConstraint(
            "kind IN ('named', 'default', 'reexport', 'reexport_all', 'commonjs', 'dunder_all')",
            name="ck_export_references_kind",
        ),
        CheckConstraint("ordinal >= 0", name="ck_export_references_ordinal"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    extraction_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_analysis_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_symbol_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    local_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_module: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_line: Mapped[int] = mapped_column(nullable=False)
    end_line: Mapped[int] = mapped_column(nullable=False)
    start_column: Mapped[int | None] = mapped_column(nullable=True)
    end_column: Mapped[int | None] = mapped_column(nullable=True)


class ProcessingRunRecord(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (
        UniqueConstraint(
            "project_version_id", "revision", name="uq_processing_runs_version_revision"
        ),
        UniqueConstraint("id", "project_version_id", name="uq_processing_runs_id_version"),
        UniqueConstraint(
            "id",
            "project_version_id",
            "revision",
            name="uq_processing_runs_embedding_owner",
        ),
        CheckConstraint(
            "outcome IN ('complete', 'partial', 'failed')", name="ck_processing_runs_outcome"
        ),
        CheckConstraint("completed_at >= started_at", name="ck_processing_runs_times"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("project_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=False,
    )
    revision: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    structural_analysis_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    processing_contract: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    limits: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    coverage: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    diagnostics: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    captured_active_processing_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    captured_latest_revision: Mapped[int] = mapped_column(nullable=False, default=0)
    processor_set: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class ArtifactProcessingRecord(Base):
    __tablename__ = "artifact_processings"
    __table_args__ = (
        UniqueConstraint(
            "processing_run_id", "artifact_id", name="uq_artifact_processings_run_artifact"
        ),
        UniqueConstraint("id", "processing_run_id", name="uq_artifact_processings_id_run"),
        ForeignKeyConstraint(
            ["processing_run_id", "project_version_id"],
            ["processing_runs.id", "processing_runs.project_version_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["artifact_id", "project_version_id"],
            ["artifacts.id", "artifacts.project_version_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "outcome IN ('processed', 'empty', 'excluded', 'unsupported', 'skipped', 'failed')",
            name="ck_artifact_processings_outcome",
        ),
        CheckConstraint(
            "execution_mode IN ('processed', 'reused')", name="ck_artifact_processings_mode"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    processing_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=False)
    project_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=False)
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    processor_contract: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    encoding: Mapped[str | None] = mapped_column(String(64))
    diagnostics: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="processed")
    reuse_source_artifact_processing_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    structural_artifact_analysis_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    result_digest: Mapped[str | None] = mapped_column(String(64))
    dependency_fingerprint: Mapped[str | None] = mapped_column(String(64))
    fingerprint_scheme: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ChunkRecord(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("artifact_processing_id", "ordinal", name="uq_chunks_processing_ordinal"),
        UniqueConstraint(
            "artifact_processing_id", "stable_key", name="uq_chunks_processing_stable_key"
        ),
        CheckConstraint("ordinal >= 0 AND part_index >= 0", name="ck_chunks_ordinals"),
        CheckConstraint(
            "start_offset >= 0 AND end_offset >= start_offset", name="ck_chunks_offsets"
        ),
        CheckConstraint("start_line >= 1 AND end_line >= start_line", name="ck_chunks_lines"),
    )
    structural_artifact_analysis_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    artifact_processing_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("artifact_processings.id", ondelete="CASCADE"),
        nullable=False,
        index=False,
    )
    stable_key: Mapped[str] = mapped_column(String(64), nullable=False, index=False)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    unit_anchor: Mapped[str] = mapped_column(Text, nullable=False)
    part_index: Mapped[int] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    start_offset: Mapped[int] = mapped_column(nullable=False)
    end_offset: Mapped[int] = mapped_column(nullable=False)
    start_line: Mapped[int] = mapped_column(nullable=False)
    end_line: Mapped[int] = mapped_column(nullable=False)
    start_column: Mapped[int] = mapped_column(nullable=False)
    end_column: Mapped[int] = mapped_column(nullable=False)
    symbol_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    symbol_identity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), index=False)
    heading_path: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    json_path: Mapped[str | None] = mapped_column(Text)


class EmbeddingRunRecord(Base):
    __tablename__ = "embedding_runs"
    __table_args__ = (
        UniqueConstraint(
            "project_version_id", "revision", name="uq_embedding_runs_snapshot_revision"
        ),
        UniqueConstraint("id", "project_version_id", name="uq_embedding_runs_id_snapshot"),
        UniqueConstraint(
            "id",
            "project_id",
            "contract_digest",
            "dimensions",
            name="uq_embedding_runs_reuse_owner",
        ),
        UniqueConstraint(
            "id",
            "project_id",
            "project_version_id",
            "processing_run_id",
            "contract_digest",
            "dimensions",
            name="uq_embedding_runs_row_owner",
        ),
        ForeignKeyConstraint(
            ["project_version_id", "project_id"],
            ["project_versions.id", "project_versions.project_id"],
            ondelete="CASCADE",
            name="fk_embedding_run_snapshot_owner",
        ),
        ForeignKeyConstraint(
            ["processing_run_id", "project_version_id", "processing_revision"],
            [
                "processing_runs.id",
                "processing_runs.project_version_id",
                "processing_runs.revision",
            ],
            name="fk_embedding_run_processing_owner",
        ),
        CheckConstraint("revision > 0", name="ck_embedding_runs_revision"),
        CheckConstraint("dimensions > 0", name="ck_embedding_runs_dimensions"),
        CheckConstraint("outcome IN ('complete', 'failed')", name="ck_embedding_runs_outcome"),
        CheckConstraint("completed_at >= started_at", name="ck_embedding_runs_times"),
        CheckConstraint(
            "contract_digest ~ '^[0-9a-f]{64}$' AND membership_digest ~ '^[0-9a-f]{64}$'",
            name="ck_embedding_runs_hashes",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    processing_revision: Mapped[int] = mapped_column(nullable=False)
    processing_contract: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    embedding_contract: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    dimensions: Mapped[int] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    captured_active_embedding_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    captured_latest_revision: Mapped[int] = mapped_column(nullable=False, server_default="0")
    membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    limits: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    coverage: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    diagnostics: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class ChunkEmbeddingRecord(Base):
    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        UniqueConstraint("embedding_run_id", "chunk_id", name="uq_chunk_embeddings_run_chunk"),
        UniqueConstraint(
            "id",
            "project_id",
            "contract_digest",
            "dimensions",
            "stable_chunk_key",
            "input_hash",
            name="uq_chunk_embeddings_reuse_source",
        ),
        ForeignKeyConstraint(
            ["embedding_run_id", "project_version_id"],
            ["embedding_runs.id", "embedding_runs.project_version_id"],
            ondelete="CASCADE",
            name="fk_chunk_embedding_run_owner",
        ),
        ForeignKeyConstraint(
            ["embedding_run_id", "project_id", "contract_digest", "dimensions"],
            [
                "embedding_runs.id",
                "embedding_runs.project_id",
                "embedding_runs.contract_digest",
                "embedding_runs.dimensions",
            ],
            name="fk_chunk_embedding_contract_owner",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["artifact_processing_id", "processing_run_id"],
            ["artifact_processings.id", "artifact_processings.processing_run_id"],
            name="fk_chunk_embedding_processing_owner",
        ),
        ForeignKeyConstraint(
            ["chunk_id", "artifact_processing_id", "stable_chunk_key", "input_hash"],
            [
                "chunks.id",
                "chunks.artifact_processing_id",
                "chunks.stable_key",
                "chunks.content_hash",
            ],
            name="fk_chunk_embedding_chunk_identity",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint("dimensions > 0 AND window_count > 0", name="ck_chunk_embeddings_positive"),
        CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$' AND logical_embedding_key ~ '^[0-9a-f]{64}$' "
            "AND contract_digest ~ '^[0-9a-f]{64}$'",
            name="ck_chunk_embeddings_hashes",
        ),
        CheckConstraint(
            "execution_mode IN ('generated', 'reused')",
            name="ck_chunk_embeddings_mode",
        ),
        CheckConstraint(
            "(execution_mode = 'reused') = (reuse_source_embedding_id IS NOT NULL)",
            name="ck_chunk_embeddings_reuse_provenance",
        ),
        CheckConstraint("vector_dims(embedding) = dimensions", name="ck_chunk_embeddings_dims"),
        CheckConstraint(
            "abs(vector_norm(embedding) - 1.0) <= 0.0001",
            name="ck_chunk_embeddings_unit_norm",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    embedding_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_processing_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stable_chunk_key: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_embedding_key: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    dimensions: Mapped[int] = mapped_column(nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    window_count: Mapped[int] = mapped_column(nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    reuse_source_embedding_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))


class SemanticCatalogRunRecord(Base):
    """Stage 13 immutable deterministic catalog run (no AI text; invariant F4)."""

    __tablename__ = "semantic_catalog_runs"
    __table_args__ = (
        UniqueConstraint(
            "project_version_id", "revision", name="uq_semantic_catalog_runs_snapshot_revision"
        ),
        UniqueConstraint("id", "project_version_id", name="uq_semantic_catalog_runs_id_snapshot"),
        ForeignKeyConstraint(
            ["project_version_id", "project_id"],
            ["project_versions.id", "project_versions.project_id"],
            ondelete="CASCADE",
            name="fk_semantic_catalog_run_snapshot_owner",
        ),
        ForeignKeyConstraint(
            ["structural_analysis_id", "project_version_id"],
            ["structural_analyses.id", "structural_analyses.project_version_id"],
            name="fk_semantic_catalog_run_structure",
        ),
        ForeignKeyConstraint(
            ["processing_run_id", "project_version_id", "processing_revision"],
            [
                "processing_runs.id",
                "processing_runs.project_version_id",
                "processing_runs.revision",
            ],
            name="fk_semantic_catalog_run_processing",
        ),
        ForeignKeyConstraint(
            ["captured_active_catalog_run_id", "project_version_id"],
            ["semantic_catalog_runs.id", "semantic_catalog_runs.project_version_id"],
            name="fk_semantic_catalog_runs_captured_active",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("revision > 0", name="ck_semantic_catalog_runs_revision"),
        CheckConstraint(
            "outcome IN ('complete', 'partial', 'failed')", name="ck_semantic_catalog_runs_outcome"
        ),
        CheckConstraint("completed_at >= started_at", name="ck_semantic_catalog_runs_times"),
        CheckConstraint(
            "input_authority_digest ~ '^[0-9a-f]{64}$' AND contract_digest ~ '^[0-9a-f]{64}$'",
            name="ck_semantic_catalog_runs_authority",
        ),
        CheckConstraint(
            "(outcome = 'failed') = (catalog_digest IS NULL) AND "
            "(catalog_digest IS NULL) = (portable_digest IS NULL) AND "
            "(catalog_digest IS NULL OR (catalog_digest ~ '^[0-9a-f]{64}$' "
            "AND portable_digest ~ '^[0-9a-f]{64}$'))",
            name="ck_semantic_catalog_runs_digests",
        ),
        Index("ix_semantic_catalog_runs_snapshot", "project_version_id", "revision"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    structural_analysis_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    structural_revision: Mapped[int] = mapped_column(nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    processing_revision: Mapped[int] = mapped_column(nullable=False)
    processing_contract: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    catalog_contract: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    contract_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_authority_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    portable_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    coverage: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    diagnostics: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    captured_active_catalog_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    captured_latest_revision: Mapped[int] = mapped_column(nullable=False, server_default="0")


def _catalog_owner() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["catalog_run_id"], ["semantic_catalog_runs.id"], ondelete="CASCADE"
    )


class SemanticEntityRecord(Base):
    __tablename__ = "semantic_entities"
    __table_args__ = (
        UniqueConstraint("catalog_run_id", "entity_key", name="uq_semantic_entities_run_key"),
        _catalog_owner(),
        ForeignKeyConstraint(
            ["artifact_identity_id"],
            ["artifact_identities.id"],
            name="fk_semantic_entity_artifact_identity",
        ),
        ForeignKeyConstraint(
            ["symbol_identity_id"],
            ["symbol_identities.id"],
            name="fk_semantic_entity_symbol_identity",
        ),
        CheckConstraint(
            "kind IN ('file', 'symbol', 'component', 'hook', 'api_endpoint', 'db_table')",
            name="ck_semantic_entities_kind",
        ),
        CheckConstraint(
            "stored_coverage IN ('complete', 'incomplete', 'unavailable', 'coordinate_mismatch')",
            name="ck_semantic_entities_stored_coverage",
        ),
        CheckConstraint(
            "context_coverage IN ('full', 'outline', 'not_eligible')",
            name="ck_semantic_entities_context_coverage",
        ),
        CheckConstraint(
            "(context_coverage = 'not_eligible') = (package_digest IS NULL) AND "
            "(package_digest IS NULL OR package_digest ~ '^[0-9a-f]{64}$')",
            name="ck_semantic_entities_package",
        ),
        CheckConstraint(
            "context_coverage = 'not_eligible' OR stored_coverage = 'complete'",
            name="ck_semantic_entities_eligibility",
        ),
        CheckConstraint("package_bytes >= 0 AND ordinal >= 0", name="ck_semantic_entities_sizes"),
        Index("ix_semantic_entities_order", "catalog_run_id", "locator", "ordinal"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    catalog_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_identity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    symbol_identity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    subject_entity_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    start_line: Mapped[int | None] = mapped_column(nullable=True)
    end_line: Mapped[int | None] = mapped_column(nullable=True)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    portable_key: Mapped[str] = mapped_column(Text, nullable=False)
    stored_coverage: Mapped[str] = mapped_column(String(32), nullable=False)
    coverage_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_coverage: Mapped[str] = mapped_column(String(16), nullable=False)
    context_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_bytes: Mapped[int] = mapped_column(nullable=False)
    content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    limitations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    package: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class SemanticFactRecord(Base):
    __tablename__ = "semantic_facts"
    __table_args__ = (
        UniqueConstraint("catalog_run_id", "fact_digest", name="uq_semantic_facts_run_digest"),
        _catalog_owner(),
        CheckConstraint(
            "fact_kind IN ('signature', 'import', 'export', 'reference', 'base', "
            "'recognizer_match', 'relation', 'limitation')",
            name="ck_semantic_facts_kind",
        ),
        CheckConstraint("fact_digest ~ '^[0-9a-f]{64}$'", name="ck_semantic_facts_digest"),
        CheckConstraint(
            "jsonb_typeof(source_refs) = 'array' AND jsonb_array_length(source_refs) > 0",
            name="ck_semantic_facts_provenance",
        ),
        Index("ix_semantic_facts_subject", "catalog_run_id", "subject_entity_key"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    catalog_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    subject_entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    fact_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_refs: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    derived: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rendered_text: Mapped[str] = mapped_column(Text, nullable=False)
    fact_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class SemanticRelationRecord(Base):
    __tablename__ = "semantic_relations"
    __table_args__ = (
        UniqueConstraint("catalog_run_id", "ordinal", name="uq_semantic_relations_run_ordinal"),
        _catalog_owner(),
        ForeignKeyConstraint(
            ["catalog_run_id", "fact_digest"],
            ["semantic_facts.catalog_run_id", "semantic_facts.fact_digest"],
            name="fk_semantic_relation_fact",
        ),
        CheckConstraint(
            "kind IN ('contains', 'imports', 'exports', 'references', 'renders', 'uses_hook', "
            "'inherits', 'defines_route', 'maps_table')",
            name="ck_semantic_relations_kind",
        ),
        CheckConstraint(
            "resolution_status IS NULL OR resolution_status IN ('resolved', 'ambiguous', "
            "'not_found', 'external', 'not_attempted', 'same_file', 'imported', 'unresolved')",
            name="ck_semantic_relations_resolution",
        ),
        CheckConstraint(
            "(target_entity_key IS NULL) <> (target_text IS NULL)",
            name="ck_semantic_relations_target",
        ),
        Index("ix_semantic_relations_source", "catalog_run_id", "source_entity_key"),
        Index("ix_semantic_relations_target", "catalog_run_id", "target_entity_key"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    catalog_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    target_entity_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fact_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class SemanticGenerationPlanRecord(Base):
    """Stage 13 persisted, immutable, single-use authorization plan (§11.1)."""

    __tablename__ = "semantic_generation_plans"
    __table_args__ = (
        UniqueConstraint("plan_nonce", name="uq_semantic_generation_plans_nonce"),
        UniqueConstraint(
            "id", "project_version_id", name="uq_semantic_generation_plans_id_snapshot"
        ),
        ForeignKeyConstraint(
            ["project_version_id", "project_id"],
            ["project_versions.id", "project_versions.project_id"],
            ondelete="CASCADE",
            name="fk_semantic_generation_plan_snapshot_owner",
        ),
        ForeignKeyConstraint(
            ["catalog_run_id", "project_version_id"],
            ["semantic_catalog_runs.id", "semantic_catalog_runs.project_version_id"],
            name="fk_semantic_generation_plan_catalog",
        ),
        CheckConstraint("expires_at > issued_at", name="ck_semantic_generation_plans_expiry"),
        CheckConstraint(
            "mode IN ('generate', 'reuse_only')", name="ck_semantic_generation_plans_mode"
        ),
        CheckConstraint(
            "sharing_class IN ('cloud', 'loopback')", name="ck_semantic_generation_plans_sharing"
        ),
        CheckConstraint(
            "plan_digest ~ '^[0-9a-f]{64}$' AND provider_config_digest ~ '^[0-9a-f]{64}$' "
            "AND generator_profile_digest ~ '^[0-9a-f]{64}$'",
            name="ck_semantic_generation_plans_digests",
        ),
        Index("ix_semantic_generation_plans_snapshot", "project_version_id", "issued_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    selection: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    accept_profiles: Mapped[object] = mapped_column(JSONB, nullable=True)
    provider_config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    provider_config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_profile_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    sharing_class: Mapped[str] = mapped_column(String(16), nullable=False)
    catalog_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    structural_analysis_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    index_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    caps: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    totals: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    contributing_locators: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    retention_note: Mapped[str] = mapped_column(Text, nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class SemanticGenerationPlanItemRecord(Base):
    __tablename__ = "semantic_generation_plan_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "entity_key", name="uq_semantic_generation_plan_items_entity"),
        UniqueConstraint("plan_id", "ordinal", name="uq_semantic_generation_plan_items_ordinal"),
        ForeignKeyConstraint(["plan_id"], ["semantic_generation_plans.id"], ondelete="CASCADE"),
        CheckConstraint(
            "disposition IN ('generate', 'reuse', 'ineligible')",
            name="ck_semantic_generation_plan_items_disposition",
        ),
        CheckConstraint(
            "disposition <> 'generate' OR (request_digest ~ '^[0-9a-f]{64}$' "
            "AND package_digest IS NOT NULL AND input_key IS NOT NULL)",
            name="ck_semantic_generation_plan_items_request",
        ),
        CheckConstraint(
            "(disposition = 'reuse') = (reuse_interpretation_id IS NOT NULL)",
            name="ck_semantic_generation_plan_items_reuse",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_coverage: Mapped[str] = mapped_column(String(16), nullable=False)
    package_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_bytes: Mapped[int] = mapped_column(nullable=False)
    contributing_locators: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    reuse_interpretation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    reuse_profile_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reuse_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reuse_returned_model: Mapped[str | None] = mapped_column(Text, nullable=True)


class SemanticGenerationRunRecord(Base):
    """One explicit, plan-authorised generation run; the only row with a guarded transition."""

    __tablename__ = "semantic_generation_runs"
    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_semantic_generation_runs_plan"),
        UniqueConstraint(
            "id", "project_version_id", name="uq_semantic_generation_runs_id_snapshot"
        ),
        UniqueConstraint("id", "project_id", name="uq_semantic_generation_runs_id_project"),
        ForeignKeyConstraint(
            ["project_version_id", "project_id"],
            ["project_versions.id", "project_versions.project_id"],
            ondelete="CASCADE",
            name="fk_semantic_generation_run_snapshot_owner",
        ),
        ForeignKeyConstraint(
            ["plan_id", "project_version_id"],
            ["semantic_generation_plans.id", "semantic_generation_plans.project_version_id"],
            name="fk_semantic_generation_run_plan",
        ),
        ForeignKeyConstraint(
            ["catalog_run_id", "project_version_id"],
            ["semantic_catalog_runs.id", "semantic_catalog_runs.project_version_id"],
            name="fk_semantic_generation_run_catalog",
        ),
        ForeignKeyConstraint(
            ["captured_index_run_id", "project_version_id"],
            ["semantic_generation_runs.id", "semantic_generation_runs.project_version_id"],
            name="fk_semantic_generation_run_captured_index",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "state IN ('executing', 'complete', 'partial', 'failed', 'superseded', 'abandoned')",
            name="ck_semantic_generation_runs_state",
        ),
        CheckConstraint(
            "(state = 'executing') = (completed_at IS NULL)",
            name="ck_semantic_generation_runs_terminal",
        ),
        CheckConstraint(
            "attempts_started >= 0 AND deadline_at > started_at",
            name="ck_semantic_generation_runs_bounds",
        ),
        Index(
            "uq_semantic_generation_runs_one_executing",
            "project_version_id",
            unique=True,
            postgresql_where=text("state = 'executing'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    catalog_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    structural_analysis_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    captured_index_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    caps: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    attempts_started: Mapped[int] = mapped_column(nullable=False, server_default="0")
    lease_token: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    totals: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    diagnostics: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class SemanticGenerationAttemptRecord(Base):
    __tablename__ = "semantic_generation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "entity_key", "attempt_no", name="uq_semantic_generation_attempts_slot"
        ),
        ForeignKeyConstraint(["run_id"], ["semantic_generation_runs.id"], ondelete="CASCADE"),
        CheckConstraint("attempt_no >= 1", name="ck_semantic_generation_attempts_number"),
        CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'", name="ck_semantic_generation_attempts_digest"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_no: Mapped[int] = mapped_column(nullable=False)
    package_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_key: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SemanticGenerationOutcomeRecord(Base):
    __tablename__ = "semantic_generation_attempt_outcomes"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_semantic_generation_outcomes_attempt"),
        ForeignKeyConstraint(
            ["attempt_id"], ["semantic_generation_attempts.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "outcome IN ('accepted', 'accepted_with_conflicts', 'insufficient_evidence', "
            "'rejected', 'rate_limited', 'provider_unavailable', 'timeout', 'context_rejected', "
            "'refused', 'token_bound_violated', 'reused_concurrently', 'outcome_unknown', "
            "'not_sent')",
            name="ck_semantic_generation_outcomes_code",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    returned_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    response_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rejection_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    retained_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SemanticGenerationLateReceiptRecord(Base):
    """A provider response that arrived after fencing: recorded, never stored as a candidate."""

    __tablename__ = "semantic_generation_late_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id"], ["semantic_generation_attempts.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "reason IN ('RUN_FENCED', 'RUN_TERMINAL')",
            name="ck_semantic_generation_late_receipts_reason",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    response_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)


class SemanticInterpretationRecord(Base):
    """Project-owned, immutable AI interpretation candidate with immutable provenance (F9)."""

    __tablename__ = "semantic_interpretations"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_semantic_interpretations_attempt"),
        UniqueConstraint("id", "project_id", name="uq_semantic_interpretations_id_project"),
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["semantic_generation_runs.id", "semantic_generation_runs.project_id"],
            ondelete="CASCADE",
            name="fk_semantic_interpretation_run",
        ),
        ForeignKeyConstraint(
            ["attempt_id"], ["semantic_generation_attempts.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "status IN ('accepted', 'accepted_with_conflicts', 'insufficient_evidence')",
            name="ck_semantic_interpretations_status",
        ),
        CheckConstraint(
            "input_key ~ '^[0-9a-f]{64}$' AND generator_profile_digest ~ '^[0-9a-f]{64}$'",
            name="ck_semantic_interpretations_keys",
        ),
        Index(
            "ix_semantic_interpretations_reuse",
            "project_id",
            "entity_key",
            "input_key",
            "generator_profile_digest",
        ),
        Index("ix_semantic_interpretations_input", "project_id", "input_key"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    input_key: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    generator_profile_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_model: Mapped[str] = mapped_column(Text, nullable=False)
    returned_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    validator_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    unknowns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SemanticClaimRecord(Base):
    __tablename__ = "semantic_claims"
    __table_args__ = (
        UniqueConstraint("interpretation_id", "ordinal", name="uq_semantic_claims_ordinal"),
        ForeignKeyConstraint(
            ["interpretation_id"], ["semantic_interpretations.id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "category IN ('purpose', 'responsibility', 'behavior', 'side_effect', 'dependency', "
            "'usage_note')",
            name="ck_semantic_claims_category",
        ),
        CheckConstraint(
            "status IN ('cited', 'conflicts_with_fact')", name="ck_semantic_claims_status"
        ),
        CheckConstraint(
            "basis IN ('code', 'documentation', 'facts')", name="ck_semantic_claims_basis"
        ),
        CheckConstraint(
            "octet_length(text) BETWEEN 1 AND 400 "
            "AND jsonb_array_length(citations) BETWEEN 1 AND 4",
            name="ck_semantic_claims_bounds",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    interpretation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    basis: Mapped[str] = mapped_column(String(16), nullable=False)
    basis_label: Mapped[str] = mapped_column(String(32), nullable=False)
    assertion: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    assertion_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scope_warning: Mapped[bool] = mapped_column(Boolean, nullable=False)
    citations: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)


class SemanticIndexEntryRecord(Base):
    __tablename__ = "semantic_index_entries"
    __table_args__ = (
        UniqueConstraint("run_id", "entity_key", name="uq_semantic_index_entries_entity"),
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["semantic_generation_runs.id", "semantic_generation_runs.project_id"],
            ondelete="CASCADE",
            name="fk_semantic_index_entry_run",
        ),
        ForeignKeyConstraint(
            ["interpretation_id", "project_id"],
            ["semantic_interpretations.id", "semantic_interpretations.project_id"],
            name="fk_semantic_index_entry_interpretation",
        ),
        CheckConstraint(
            "mode IN ('generated', 'reused', 'retained')", name="ck_semantic_index_entries_mode"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    interpretation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    reused_from_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)


# Explicit names and ownership match the forward-migrated PostgreSQL schema.
Index(
    "ix_project_versions_project_id",
    *[Base.metadata.tables["project_versions"].c[c] for c in ["project_id"]],
)
Index(
    "ix_artifacts_project_version_id",
    *[Base.metadata.tables["artifacts"].c[c] for c in ["project_version_id"]],
)
Index(
    "ix_artifact_identities_project",
    *[Base.metadata.tables["artifact_identities"].c[c] for c in ["project_id"]],
)
Index(
    "ix_artifact_analyses_order",
    *[
        Base.metadata.tables["artifact_analyses"].c[c]
        for c in ["structural_analysis_id", "locator"]
    ],
)
Index(
    "ix_artifact_processings_run",
    *[Base.metadata.tables["artifact_processings"].c[c] for c in ["processing_run_id", "locator"]],
)
Index(
    "ix_artifacts_stable_identity",
    *[Base.metadata.tables["artifacts"].c[c] for c in ["stable_identity_id"]],
)
Index(
    "ix_chunks_processing_order",
    *[Base.metadata.tables["chunks"].c[c] for c in ["artifact_processing_id", "ordinal"]],
)
Index("ix_chunks_stable_key", *[Base.metadata.tables["chunks"].c[c] for c in ["stable_key"]])
Index(
    "ix_import_references_order",
    *[
        Base.metadata.tables["import_references"].c[c]
        for c in ["artifact_analysis_id", "start_line", "statement_ordinal", "item_ordinal"]
    ],
)
Index(
    "ix_processing_runs_version_revision",
    *[Base.metadata.tables["processing_runs"].c[c] for c in ["project_version_id", "revision"]],
)
Index(
    "ix_embedding_runs_snapshot_revision",
    EmbeddingRunRecord.project_version_id,
    EmbeddingRunRecord.revision.desc(),
)
Index(
    "ix_embedding_runs_project_contract",
    EmbeddingRunRecord.project_id,
    EmbeddingRunRecord.contract_digest,
)
Index(
    "ix_chunk_embeddings_logical_reuse",
    ChunkEmbeddingRecord.project_id,
    ChunkEmbeddingRecord.logical_embedding_key,
    ChunkEmbeddingRecord.contract_digest,
)
Index("ix_chunk_embeddings_run", ChunkEmbeddingRecord.embedding_run_id)
Base.metadata.tables["chunks"].append_constraint(
    Index(
        "ix_chunks_lexical_content_v1",
        text("to_tsvector('simple'::regconfig, buildlens_lexical_normalize_v1(content))"),
        postgresql_using="gin",
    )
)
Base.metadata.tables["symbols"].append_constraint(
    Index(
        "ix_symbols_lexical_qualified_name_v1",
        text("to_tsvector('simple'::regconfig, buildlens_lexical_normalize_v1(qualified_name))"),
        postgresql_using="gin",
    )
)
Base.metadata.tables["artifact_processings"].append_constraint(
    Index(
        "ix_artifact_processings_lexical_locator_v1",
        text("to_tsvector('simple'::regconfig, buildlens_lexical_normalize_v1(locator))"),
        postgresql_using="gin",
    )
)
Index(
    "ix_project_versions_predecessor",
    *[Base.metadata.tables["project_versions"].c[c] for c in ["predecessor_id"]],
)
Index(
    "ix_versions_project_sequence",
    *[Base.metadata.tables["project_versions"].c[c] for c in ["project_id", "sequence"]],
)
Index(
    "ix_structural_analyses_version",
    *[Base.metadata.tables["structural_analyses"].c[c] for c in ["project_version_id"]],
)
Index(
    "ix_symbol_identities_artifact",
    *[Base.metadata.tables["symbol_identities"].c[c] for c in ["artifact_identity_id"]],
)
Index(
    "ix_symbols_order",
    *[
        Base.metadata.tables["symbols"].c[c]
        for c in ["artifact_analysis_id", "start_line", "ordinal"]
    ],
)
Index(
    "ix_symbols_stable_identity",
    *[Base.metadata.tables["symbols"].c[c] for c in ["stable_identity_id"]],
)
Index(
    "uq_artifacts_snapshot_locator",
    ArtifactRecord.project_version_id,
    ArtifactRecord.locator,
    unique=True,
    postgresql_where=text("inventory_status IS NOT NULL"),
)
Index(
    "ix_project_versions_latest_snapshot",
    ProjectVersionRecord.project_id,
    ProjectVersionRecord.scan_completed_at.desc(),
    ProjectVersionRecord.id.desc(),
    postgresql_where=text("scan_completed_at IS NOT NULL"),
)
Base.metadata.tables["project_versions"].append_constraint(
    UniqueConstraint(*["id", "project_id"], name="uq_versions_id_project")
)
Base.metadata.tables["chunks"].append_constraint(
    UniqueConstraint(
        *["id", "artifact_processing_id", "stable_key", "content_hash"],
        name="uq_chunks_embedding_identity",
    )
)
Base.metadata.tables["artifact_analyses"].append_constraint(
    UniqueConstraint(*["id", "artifact_id"], name="uq_artifact_analysis_id_artifact")
)
Base.metadata.tables["symbol_identities"].append_constraint(
    UniqueConstraint(*["id", "artifact_identity_id"], name="uq_symbol_identity_id_artifact")
)
Base.metadata.tables["project_versions"].append_constraint(
    ForeignKeyConstraint(
        ["predecessor_id", "project_id"],
        ["project_versions.id", "project_versions.project_id"],
        name="fk_version_predecessor",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["project_versions"].append_constraint(
    ForeignKeyConstraint(
        ["active_embedding_run_id", "id"],
        ["embedding_runs.id", "embedding_runs.project_version_id"],
        name="fk_project_versions_active_embedding_owner",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["embedding_runs"].append_constraint(
    ForeignKeyConstraint(
        ["captured_active_embedding_run_id", "project_version_id"],
        ["embedding_runs.id", "embedding_runs.project_version_id"],
        name="fk_embedding_runs_captured_active",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["project_versions"].append_constraint(
    ForeignKeyConstraint(
        ["active_analysis_id", "id"],
        ["structural_analyses.id", "structural_analyses.project_version_id"],
        name="fk_version_active_structure",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["artifacts"].append_constraint(
    ForeignKeyConstraint(
        ["stable_identity_id"],
        ["artifact_identities.id"],
        name="fk_artifact_identity",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["symbols"].append_constraint(
    ForeignKeyConstraint(
        ["stable_identity_id"],
        ["symbol_identities.id"],
        name="fk_symbol_identity",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["symbol_identities"].append_constraint(
    ForeignKeyConstraint(
        ["parent_identity_id", "artifact_identity_id"],
        ["symbol_identities.id", "symbol_identities.artifact_identity_id"],
        name="fk_identity_parent",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["processing_runs"].append_constraint(
    ForeignKeyConstraint(
        ["structural_analysis_id", "project_version_id"],
        ["structural_analyses.id", "structural_analyses.project_version_id"],
        name="fk_processing_structure",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["artifact_processings"].append_constraint(
    ForeignKeyConstraint(
        ["structural_artifact_analysis_id", "artifact_id"],
        ["artifact_analyses.id", "artifact_analyses.artifact_id"],
        name="fk_processing_artifact_structure",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["artifact_processings"].append_constraint(
    ForeignKeyConstraint(
        ["reuse_source_artifact_processing_id"],
        ["artifact_processings.id"],
        name="fk_processing_reuse",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["chunks"].append_constraint(
    ForeignKeyConstraint(
        ["symbol_id"], ["symbols.id"], name="fk_chunk_symbol", deferrable=True, initially="DEFERRED"
    )
)
Base.metadata.tables["chunks"].append_constraint(
    ForeignKeyConstraint(
        ["symbol_identity_id"],
        ["symbol_identities.id"],
        name="fk_chunk_symbol_identity",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["artifact_analyses"].append_constraint(
    ForeignKeyConstraint(
        ["baseline_artifact_analysis_id"],
        ["artifact_analyses.id"],
        name="fk_artifact_analysis_baseline",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["artifact_analyses"].append_constraint(
    ForeignKeyConstraint(
        ["reuse_source_artifact_analysis_id"],
        ["artifact_analyses.id"],
        name="fk_artifact_analysis_reuse",
        deferrable=True,
        initially="DEFERRED",
    )
)
Base.metadata.tables["project_versions"].append_constraint(
    ForeignKeyConstraint(
        ["active_processing_run_id", "id"],
        ["processing_runs.id", "processing_runs.project_version_id"],
        name="fk_project_versions_active_processing_owner",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["artifacts"].append_constraint(
    UniqueConstraint(*["id", "stable_identity_id"], name="uq_artifact_id_identity")
)

Base.metadata.tables["artifact_analyses"].append_constraint(
    UniqueConstraint(*["id", "artifact_identity_id"], name="uq_analysis_id_identity")
)

Base.metadata.tables["artifact_processings"].append_constraint(
    UniqueConstraint(*["id", "structural_artifact_analysis_id"], name="uq_processing_id_structure")
)

Base.metadata.tables["symbols"].append_constraint(
    UniqueConstraint(
        *["id", "artifact_analysis_id", "stable_identity_id"],
        name="uq_symbol_id_structure_identity",
    )
)

Base.metadata.tables["artifacts"].append_constraint(
    ForeignKeyConstraint(
        ["project_version_id", "project_id"],
        ["project_versions.id", "project_versions.project_id"],
        name="fk_artifact_project",
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["artifacts"].append_constraint(
    ForeignKeyConstraint(
        ["stable_identity_id", "project_id"],
        ["artifact_identities.id", "artifact_identities.project_id"],
        name="fk_artifact_identity_project",
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["artifact_analyses"].append_constraint(
    ForeignKeyConstraint(
        ["artifact_id", "artifact_identity_id"],
        ["artifacts.id", "artifacts.stable_identity_id"],
        name="fk_analysis_artifact_identity",
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["symbols"].append_constraint(
    ForeignKeyConstraint(
        ["artifact_analysis_id", "artifact_identity_id"],
        ["artifact_analyses.id", "artifact_analyses.artifact_identity_id"],
        name="fk_symbol_artifact_identity",
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["symbols"].append_constraint(
    ForeignKeyConstraint(
        ["stable_identity_id", "artifact_identity_id"],
        ["symbol_identities.id", "symbol_identities.artifact_identity_id"],
        name="fk_symbol_identity_artifact",
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["chunks"].append_constraint(
    ForeignKeyConstraint(
        ["artifact_processing_id", "structural_artifact_analysis_id"],
        ["artifact_processings.id", "artifact_processings.structural_artifact_analysis_id"],
        name="fk_chunk_processing_structure",
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["chunks"].append_constraint(
    ForeignKeyConstraint(
        ["symbol_id", "structural_artifact_analysis_id", "symbol_identity_id"],
        ["symbols.id", "symbols.artifact_analysis_id", "symbols.stable_identity_id"],
        name="fk_chunk_symbol_owner",
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["structural_analyses"].append_constraint(
    ForeignKeyConstraint(
        ["captured_active_analysis_id", "project_version_id"],
        ["structural_analyses.id", "structural_analyses.project_version_id"],
        name="fk_structure_captured_active",
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["processing_runs"].append_constraint(
    ForeignKeyConstraint(
        ["captured_active_processing_run_id", "project_version_id"],
        ["processing_runs.id", "processing_runs.project_version_id"],
        name="fk_processing_captured_active",
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["artifacts"].append_constraint(
    CheckConstraint("project_id IS NOT NULL", name="ck_artifact_project_required")
)

Base.metadata.tables["symbols"].append_constraint(
    CheckConstraint(
        "stable_identity_id IS NOT NULL AND artifact_identity_id IS NOT NULL",
        name="ck_symbol_identity_required",
    )
)

Base.metadata.tables["chunks"].append_constraint(
    CheckConstraint(
        "(symbol_id IS NULL AND symbol_identity_id IS NULL) OR (symbol_id IS NOT NULL AND "
        "symbol_identity_id IS NOT NULL AND structural_artifact_analysis_id IS NOT NULL)",
        name="ck_chunk_symbol_owner_required",
    )
)

Index(
    "ix_processing_artifact_lookup",
    *[Base.metadata.tables["artifact_processings"].c[c] for c in ["artifact_id"]],
)

Index(
    "ix_chunks_symbol_identity",
    *[Base.metadata.tables["chunks"].c[c] for c in ["symbol_identity_id"]],
)

Index(
    "ix_symbols_analysis",
    *[Base.metadata.tables["symbols"].c[c] for c in ["structural_analysis_id"]],
)

Index(
    "ix_imports_analysis",
    *[Base.metadata.tables["import_references"].c[c] for c in ["structural_analysis_id"]],
)

Base.metadata.tables["project_versions"].append_constraint(
    CheckConstraint(
        "scan_status IS NULL OR scan_status IN ('complete', 'partial')",
        name="ck_project_versions_stage2_status",
    )
)

Base.metadata.tables["project_versions"].append_constraint(
    CheckConstraint(
        "scan_completed_at IS NULL OR scan_started_at IS NOT NULL "
        "AND scan_completed_at >= scan_started_at",
        name="ck_project_versions_stage2_times",
    )
)

Base.metadata.tables["project_versions"].append_constraint(
    CheckConstraint(
        "scan_status IS NULL OR (canonical_root IS NOT NULL AND scan_started_at IS NOT NULL "
        "AND scan_completed_at IS NOT NULL AND policy_version IS NOT NULL "
        "AND git_metadata IS NOT NULL AND warnings IS NOT NULL AND summary IS NOT NULL)",
        name="ck_project_versions_stage2_together",
    )
)

Base.metadata.tables["artifacts"].append_constraint(
    CheckConstraint(
        "inventory_status IS NULL OR inventory_status IN ('included', 'excluded')",
        name="ck_artifacts_stage2_status",
    )
)

Base.metadata.tables["artifacts"].append_constraint(
    CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_artifacts_stage2_size")
)

Base.metadata.tables["artifacts"].append_constraint(
    CheckConstraint(
        "inventory_status IS DISTINCT FROM 'excluded' OR cardinality(exclusion_reasons) > 0",
        name="ck_artifacts_stage2_excluded_reason",
    )
)

Base.metadata.tables["artifacts"].append_constraint(
    CheckConstraint("pruned IS NOT TRUE OR kind = 'directory'", name="ck_artifacts_stage2_pruned")
)

Base.metadata.tables["artifacts"].append_constraint(
    CheckConstraint(
        "inventory_status IS NULL OR (locator <> '' AND locator !~ '(^|/)\\.\\.?(/|$)' "
        "AND octet_length(locator) <= 2048)",
        name="ck_artifacts_stage2_locator",
    )
)

Base.metadata.tables["artifact_analyses"].append_constraint(
    ForeignKeyConstraint(
        ["declaration_extraction_id", "id"],
        ["artifact_extractions.id", "artifact_extractions.artifact_analysis_id"],
        name="fk_artifact_analysis_declaration_extraction",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["project_versions"].append_constraint(
    ForeignKeyConstraint(
        ["active_semantic_catalog_run_id", "id"],
        ["semantic_catalog_runs.id", "semantic_catalog_runs.project_version_id"],
        name="fk_project_versions_active_semantic_catalog",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)

Base.metadata.tables["project_versions"].append_constraint(
    ForeignKeyConstraint(
        ["active_semantic_index_run_id", "id"],
        ["semantic_generation_runs.id", "semantic_generation_runs.project_version_id"],
        name="fk_project_versions_active_semantic_index",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)
