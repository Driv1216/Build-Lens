"""Immutable metadata used by repository onboarding.

The types in this module deliberately describe observations only.  They never
contain file contents, hashes, permissions, or symlink targets.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ScanStatus = Literal["complete", "partial"]
InventoryStatus = Literal["included", "excluded"]
IgnoreStatus = Literal["ignored", "not_ignored", "unknown"]
ContentHint = Literal["text", "binary", "unknown"]


@dataclass(frozen=True, slots=True)
class GitMetadata:
    state: Literal["available", "not_repository", "unavailable"]
    repository_root: str | None
    project_prefix: str | None
    branch: str | None
    head_commit: str | None
    dirty: bool | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SnapshotWarning:
    code: str
    message: str
    locator: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotSummary:
    observed_by_kind: dict[str, int]
    included_regular_files: int
    excluded_regular_files: int
    pruned_directories: int
    included_bytes: int
    language_counts: dict[str, int]
    tag_counts: dict[str, int]
    exclusion_reason_counts: dict[str, int]
    warning_count: int


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    inventory_status: InventoryStatus
    size_bytes: int | None
    language: str | None
    tags: tuple[str, ...]
    content_hint: ContentHint
    ignore_status: IgnoreStatus
    exclusion_reasons: tuple[str, ...]
    pruned: bool
