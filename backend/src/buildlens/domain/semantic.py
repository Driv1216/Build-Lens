"""Stage 13 deterministic semantic catalog: contracts and immutable records.

The catalog is a pure function of persisted structural observations, chunk metadata and the
inventory. It never reads source files and never contains AI text (invariant F4). Every fact
is rendered by a versioned renderer from the exact structural rows it cites (``source_refs``)
and is re-verified from those rows at read time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, get_args
from uuid import UUID

from buildlens.domain.extractors import PYTHON_FACTS, current_declaration_digests
from buildlens.domain.extractors import extractor_contract_digest as _extractor_digest
from buildlens.domain.source import digest

CATALOG_VERSION = "semantic-catalog.v2"
RENDERER_VERSION = "fact-renderer.v1"
RESOLUTION_VERSION = "import-resolution.v1"
PACKAGE_VERSION = "semantic-package.v1"
RECOGNIZERS = (
    "fastapi-route.v1",
    "flask-route.v1",
    "sqlalchemy-declarative.v1",
    "alembic-table.v1",
    "react-component.v1",
    "react-hook.v1",
)
# Placeholder product policy (plan §10.7): conservative bytes, not a tokenizer guarantee.
PACKAGE_BYTE_CEILING = 48_000
NEIGHBOR_FACT_LIMIT = 20
REFERENCE_FACT_LIMIT = 64
RELATION_ROW_LIMIT = 16

EntityKind = Literal["file", "symbol", "component", "hook", "api_endpoint", "db_table"]
StoredCoverage = Literal["complete", "incomplete", "unavailable", "coordinate_mismatch"]
ContextCoverage = Literal["full", "outline", "not_eligible"]
FactKind = Literal[
    "signature",
    "import",
    "export",
    "reference",
    "base",
    "recognizer_match",
    "relation",
    "limitation",
]
RelationKind = Literal[
    "contains",
    "imports",
    "exports",
    "references",
    "renders",
    "uses_hook",
    "inherits",
    "defines_route",
    "maps_table",
]
Resolution = Literal[
    "resolved",
    "ambiguous",
    "not_found",
    "external",
    "not_attempted",
    "same_file",
    "imported",
    "unresolved",
]
CatalogOutcome = Literal["complete", "partial", "failed"]

ENTITY_KINDS: tuple[str, ...] = get_args(EntityKind)
FACT_KINDS: tuple[str, ...] = get_args(FactKind)
RELATION_KINDS: tuple[str, ...] = get_args(RelationKind)
RESOLUTIONS: tuple[str, ...] = get_args(Resolution)

Row = dict[str, Any]


def catalog_contract() -> dict[str, object]:
    """Everything the catalog output depends on besides its persisted inputs."""
    extractors = dict(current_declaration_digests())
    facts = _extractor_digest(PYTHON_FACTS)
    if facts is not None:
        extractors[PYTHON_FACTS] = facts
    return {
        "catalog": CATALOG_VERSION,
        "renderer": RENDERER_VERSION,
        "resolution": RESOLUTION_VERSION,
        "package": PACKAGE_VERSION,
        "recognizers": list(RECOGNIZERS),
        "package_byte_ceiling": PACKAGE_BYTE_CEILING,
        "neighbor_fact_limit": NEIGHBOR_FACT_LIMIT,
        "extractors": dict(sorted(extractors.items())),
    }


def package_digest_of(
    mode: str,
    chunks: Sequence[tuple[str, str]],
    facts: Sequence[str],
    neighbors: Sequence[str],
    omitted: Sequence[tuple[str, int | None, int | None]],
) -> str:
    """Canonical package projection (§4.2): chunk keys and hashes, fact digests, omitted regions."""
    return digest(
        {
            "version": PACKAGE_VERSION,
            "mode": mode,
            "chunks": [[key, value] for key, value in chunks],
            "facts": sorted(facts),
            "neighbors": list(neighbors),
            "omitted": [[key, start, end] for key, start, end in omitted],
        }
    )


def fact_digest(
    renderer_version: str, rendered_text: str, source_refs: list[Row], derived: Row
) -> str:
    return digest(
        {
            "renderer": renderer_version,
            "text": rendered_text,
            "refs": source_refs,
            "derived": derived,
        }
    )


@dataclass(frozen=True, slots=True)
class SemanticFact:
    fact_digest: str
    ordinal: int
    subject_entity_key: str
    fact_kind: FactKind
    source_refs: tuple[Row, ...]
    derived: Row
    renderer_version: str
    rendered_text: str
    # Read-time verification: ``verified`` or an integrity code; ``None`` until checked.
    verification: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticRelation:
    ordinal: int
    kind: RelationKind
    source_entity_key: str
    target_entity_key: str | None
    target_text: str | None
    resolution_status: Resolution | None
    fact_digest: str


@dataclass(frozen=True, slots=True)
class EvidenceChunk:
    chunk_id: UUID
    stable_key: str
    content_hash: str
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    symbol_identity_id: UUID | None
    content: str | None = None
    verified: bool | None = None


@dataclass(frozen=True, slots=True)
class OmittedRegion:
    entity_key: str
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True, slots=True)
class SemanticEntity:
    entity_key: str
    kind: EntityKind
    language: str | None
    locator: str
    artifact_identity_id: UUID | None
    symbol_identity_id: UUID | None
    subject_entity_key: str | None
    name: str
    qualified_name: str | None
    symbol_kind: str | None
    start_line: int | None
    end_line: int | None
    ordinal: int
    portable_key: str
    stored_coverage: StoredCoverage
    coverage_reason: str | None
    context_coverage: ContextCoverage
    context_reason: str | None
    package_digest: str | None
    package_bytes: int
    limitations: tuple[str, ...] = ()
    evidence: tuple[EvidenceChunk, ...] = ()
    gaps: Row = field(default_factory=dict)
    package_chunks: tuple[str, ...] = ()
    package_facts: tuple[str, ...] = ()
    neighbor_facts: tuple[str, ...] = ()
    omitted: tuple[OmittedRegion, ...] = ()
    # Content projection of the package (chunk hashes and positions, rendered fact texts,
    # omitted regions by portable key). Stable across snapshots; keys source freshness.
    content_digest: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticCatalogRun:
    id: UUID
    project_id: UUID
    snapshot_id: UUID
    revision: int
    started_at: datetime
    completed_at: datetime
    outcome: CatalogOutcome
    structural_analysis_id: UUID
    structural_revision: int
    processing_run_id: UUID
    processing_revision: int
    processing_contract: dict[str, object]
    catalog_contract: dict[str, object]
    input_authority_digest: str
    catalog_digest: str | None
    portable_digest: str | None
    coverage: dict[str, object]
    diagnostics: tuple[str, ...] = ()
    superseded: bool = False
    quarantined: bool = False
    captured_active_catalog_run_id: UUID | None = None
    captured_latest_revision: int = 0


@dataclass(frozen=True, slots=True)
class CatalogBuild:
    """The deterministic output of one catalog build, before persistence assigns ids."""

    entities: tuple[SemanticEntity, ...]
    facts: tuple[SemanticFact, ...]
    relations: tuple[SemanticRelation, ...]
    catalog_digest: str
    portable_digest: str
    coverage: dict[str, object]
    outcome: CatalogOutcome
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntityDetail:
    run: SemanticCatalogRun
    entity: SemanticEntity
    facts: tuple[SemanticFact, ...]
    neighbor_facts: tuple[SemanticFact, ...]
    outgoing: tuple[SemanticRelation, ...]
    incoming: tuple[SemanticRelation, ...]
    integrity_errors: tuple[str, ...]
    freshness_reasons: tuple[str, ...]
    # Display labels ("kind name · locator") for every entity named by a relation.
    related: dict[str, str] = field(default_factory=dict)
