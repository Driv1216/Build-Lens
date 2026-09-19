"""Content-fingerprint contracts used by snapshot-time evidence.

These values intentionally carry no source bytes.  They are immutable evidence
about an observation, not claims about the live filesystem after a snapshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

FingerprintStatus = Literal["fingerprinted", "not_eligible", "unavailable", "legacy_unverified"]


@dataclass(frozen=True, slots=True)
class ContentFingerprint:
    algorithm: Literal["sha256"]
    schema_version: Literal["artifact-bytes.v1"]
    digest: str


@dataclass(frozen=True, slots=True)
class FingerprintCoverage:
    eligible_files: int = 0
    fingerprinted_files: int = 0
    unavailable_files: int = 0
    not_eligible_files: int = 0
    bytes_consumed: int = 0
    complete: bool = False


def project_manifest_digest(entries: list[tuple[str, ContentFingerprint]]) -> ContentFingerprint:
    """Build the content-only, canonical Stage 4 project manifest digest."""
    canonical = [
        "project-content-manifest.v1",
        "sha256",
        "artifact-bytes.v1",
        [
            [locator, fingerprint.digest]
            for locator, fingerprint in sorted(entries, key=lambda v: v[0].encode("utf-8"))
        ],
    ]
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return ContentFingerprint("sha256", "artifact-bytes.v1", hashlib.sha256(payload).hexdigest())
