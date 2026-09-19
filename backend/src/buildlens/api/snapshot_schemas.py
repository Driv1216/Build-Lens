from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class GitMetadataResponse(BaseModel):
    state: Literal["available", "not_repository", "unavailable"]
    repository_root: str | None
    project_prefix: str | None
    branch: str | None
    head_commit: str | None
    dirty: bool | None
    observed_at: datetime


class WarningResponse(BaseModel):
    code: str
    message: str
    locator: str | None


class SummaryResponse(BaseModel):
    observed_by_kind: dict[str, int]
    included_regular_files: int
    excluded_regular_files: int
    pruned_directories: int
    included_bytes: int
    language_counts: dict[str, int]
    tag_counts: dict[str, int]
    exclusion_reason_counts: dict[str, int]
    warning_count: int


class SnapshotResponse(BaseModel):
    id: UUID
    project_id: UUID
    canonical_root: str
    scan_started_at: datetime
    scan_completed_at: datetime
    scan_status: Literal["complete", "partial"]
    policy_version: str
    sequence: int = 0
    predecessor_id: UUID | None = None
    content_manifest: str | None = None
    fingerprint_coverage: dict[str, int | bool] | None = None
    eligibility_provenance: dict[str, object] | None = None
    comparison_contract: str = "stage4.comparison.v1"
    git_metadata: GitMetadataResponse
    summary: SummaryResponse
    warnings: list[WarningResponse]


class ArtifactResponse(BaseModel):
    id: UUID
    kind: str
    locator: str
    inventory_status: Literal["included", "excluded"]
    size_bytes: int | None
    language: str | None
    tags: list[str]
    content_hint: Literal["text", "binary", "unknown"]
    ignore_status: Literal["ignored", "not_ignored", "unknown"]
    exclusion_reasons: list[str]
    pruned: bool
    stable_identity_id: UUID | None = None
    continuity_reason: str | None = None
    content_fingerprint: str | None = None
    fingerprint_status: str = "legacy_unverified"
    fingerprint_size_bytes: int | None = None
    fingerprint_diagnostic: str | None = None


class ArtifactPageResponse(BaseModel):
    items: list[ArtifactResponse]
    total: int
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)


class ArtifactChangeResponse(BaseModel):
    state: Literal["ADDED", "UNCHANGED", "CHANGED", "REMOVED", "UNKNOWN"]
    reason: str
    artifact_id: UUID | None
    previous_artifact_id: UUID | None
    locator: str
    stable_identity_id: UUID | None


class ArtifactChangePageResponse(BaseModel):
    items: list[ArtifactChangeResponse]
    total: int
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)
