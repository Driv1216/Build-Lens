"""Snapshot-time streaming fingerprinting through the secure reader boundary."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from buildlens.application.source_ports import SecureSourceReader, SourceReadError
from buildlens.domain.entities import Artifact
from buildlens.domain.fingerprints import (
    ContentFingerprint,
    FingerprintCoverage,
    project_manifest_digest,
)
from buildlens.domain.source import READER_VERSION


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FingerprintResult:
    artifacts: list[Artifact]
    coverage: FingerprintCoverage
    manifest: ContentFingerprint | None
    root_observation: dict[str, object] | None


class SnapshotFingerprinter:
    """Hashes eligible files synchronously without retaining source buffers."""

    def __init__(
        self,
        *,
        max_file_bytes: int = 64 * 1024 * 1024,
        max_total_bytes: int = 1024 * 1024 * 1024,
        file_timeout_seconds: float = 10,
        overall_timeout_seconds: float = 120,
        reader: SecureSourceReader,
    ) -> None:
        self._max_file_bytes = max_file_bytes
        self._max_total_bytes = max_total_bytes
        self._file_timeout_seconds = file_timeout_seconds
        self._overall_timeout_seconds = overall_timeout_seconds
        self._reader = reader

    def fingerprint(
        self, root: str, artifacts: list[Artifact], expected_root: dict[str, object] | None = None
    ) -> FingerprintResult:
        try:
            return self._fingerprint(root, artifacts, expected_root)
        finally:
            self._reader.close()

    def _fingerprint(
        self, root: str, artifacts: list[Artifact], expected_root: dict[str, object] | None
    ) -> FingerprintResult:
        if os.name != "posix":
            return self._unavailable(artifacts, "UNSUPPORTED_PLATFORM")
        try:
            root_stat = os.stat(root, follow_symlinks=False)
            if not os.path.isdir(root) or os.path.islink(root):
                return self._unavailable(artifacts, "ROOT_UNREADABLE")
            root_observation: dict[str, object] | None = {
                "device": root_stat.st_dev,
                "inode": root_stat.st_ino,
            }
        except OSError:
            return self._unavailable(artifacts, "ROOT_UNREADABLE")

        if expected_root is not None and root_observation != expected_root:
            return self._unavailable(artifacts, "ROOT_CHANGED")
        deadline = time.monotonic() + self._overall_timeout_seconds
        total = 0
        eligible = fingerprinted = unavailable = not_eligible = 0
        output: list[Artifact] = []
        for artifact in sorted(artifacts, key=lambda item: item.locator.encode("utf-8")):
            metadata = artifact.metadata
            is_eligible = bool(
                metadata
                and artifact.kind == "file"
                and metadata.inventory_status == "included"
                and metadata.ignore_status == "not_ignored"
                and not metadata.pruned
                and not metadata.exclusion_reasons
            )
            if not is_eligible:
                not_eligible += 1
                output.append(replace(artifact, fingerprint_status="not_eligible"))
                continue
            eligible += 1
            started = _now()
            size = metadata.size_bytes if metadata else None
            if time.monotonic() > deadline:
                unavailable += 1
                output.append(
                    self._unavailable_artifact(artifact, started, "OVERALL_FINGERPRINT_TIMEOUT")
                )
                continue
            if size is None or size > self._max_file_bytes or total + size > self._max_total_bytes:
                unavailable += 1
                code = (
                    "FINGERPRINT_TOO_LARGE"
                    if size is None or size > self._max_file_bytes
                    else "TOTAL_FINGERPRINT_LIMIT"
                )
                output.append(self._unavailable_artifact(artifact, started, code))
                continue
            try:
                result = self._reader.fingerprint(
                    root,
                    artifact.locator,
                    min(self._max_file_bytes, self._max_total_bytes - total),
                    min(self._file_timeout_seconds, max(0.001, deadline - time.monotonic())),
                    (root_stat.st_dev, root_stat.st_ino),
                )
                total += result.size_bytes
                fingerprinted += 1
                output.append(
                    replace(
                        artifact,
                        content_fingerprint=ContentFingerprint(
                            "sha256", "artifact-bytes.v1", result.digest
                        ),
                        fingerprint_status="fingerprinted",
                        fingerprint_size_bytes=result.size_bytes,
                        fingerprint_read_started_at=started,
                        fingerprint_read_completed_at=_now(),
                        fingerprint_reader_version=READER_VERSION,
                        fingerprint_diagnostic=None,
                    )
                )
            except SourceReadError as error:
                total += error.bytes_consumed
                unavailable += 1
                output.append(self._unavailable_artifact(artifact, started, error.code))
        coverage = FingerprintCoverage(
            eligible, fingerprinted, unavailable, not_eligible, total, unavailable == 0
        )
        entries = [
            (item.locator, item.content_fingerprint)
            for item in output
            if item.fingerprint_status == "fingerprinted" and item.content_fingerprint is not None
        ]
        manifest = project_manifest_digest(entries) if coverage.complete else None
        return FingerprintResult(output, coverage, manifest, root_observation)

    @staticmethod
    def _unavailable_artifact(artifact: Artifact, started: datetime, code: str) -> Artifact:
        return replace(
            artifact,
            fingerprint_status="unavailable",
            fingerprint_read_started_at=started,
            fingerprint_read_completed_at=_now(),
            fingerprint_reader_version=READER_VERSION,
            fingerprint_diagnostic=code,
        )

    def _unavailable(self, artifacts: list[Artifact], code: str) -> FingerprintResult:
        now = _now()
        output = [
            self._unavailable_artifact(item, now, code)
            if item.kind == "file"
            and item.metadata
            and item.metadata.inventory_status == "included"
            else replace(item, fingerprint_status="not_eligible")
            for item in artifacts
        ]
        eligible = sum(item.fingerprint_status == "unavailable" for item in output)
        return FingerprintResult(
            output,
            FingerprintCoverage(eligible, 0, eligible, len(output) - eligible, 0, False),
            None,
            None,
        )
