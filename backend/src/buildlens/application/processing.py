"""Explicit processing: compatible evidence, bounded work and retained attempts."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from threading import Lock
from typing import Literal, Protocol
from uuid import UUID, uuid4

from buildlens.application.projects import ProjectNotFoundError
from buildlens.application.snapshots import SnapshotNotFoundError
from buildlens.application.source_ports import ProcessingWorker, SourceReadError
from buildlens.domain.eligibility import exclusion_reason
from buildlens.domain.entities import Artifact, Project, ProjectVersion
from buildlens.domain.processing import (
    ArtifactProcessing,
    ArtifactProcessingOutcome,
    Chunk,
    ProcessingDiagnostic,
    ProcessingRun,
)
from buildlens.domain.source import (
    ContentRejectedError,
    decode_source,
    digest,
    processing_contract,
    screen_content,
    structural_contract,
)
from buildlens.domain.structural import ArtifactAnalysis, StructuralAnalysis


class ProcessingBusyError(Exception):
    pass


class ProcessingUnavailableError(Exception):
    pass


class ProcessingConflictError(Exception):
    pass


class ProcessingNotFoundError(Exception):
    pass


class CurrentContentUnavailableError(Exception):
    pass


class ProcessingStore(Protocol):
    def get_project(self, project_id: UUID) -> Project | None: ...
    def get_snapshot(self, project_id: UUID, snapshot_id: UUID) -> ProjectVersion | None: ...
    def artifacts(self, project_id: UUID, snapshot_id: UUID) -> list[Artifact]: ...
    def active_structural(
        self, project_id: UUID, snapshot_id: UUID
    ) -> StructuralAnalysis | None: ...
    def structural_artifacts(self, analysis_id: UUID) -> dict[UUID, ArtifactAnalysis]: ...
    def latest(self, project_id: UUID, snapshot_id: UUID) -> ProcessingRun | None: ...
    def active(self, project_id: UUID, snapshot_id: UUID) -> ProcessingRun | None: ...
    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> ProcessingRun | None: ...
    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[ProcessingRun], int]: ...
    def reusable(
        self, project_id: UUID, snapshot: ProjectVersion
    ) -> dict[UUID, ArtifactProcessing]: ...
    def dependencies(self, run_id: UUID) -> list[tuple[UUID, str]]: ...
    def page_artifacts(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID, limit: int, offset: int
    ) -> tuple[list[ArtifactProcessing], int]: ...
    def page_chunks(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID, limit: int, offset: int
    ) -> tuple[list[Chunk], int]: ...
    def content_stats(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID
    ) -> tuple[int, int]: ...
    def page_chunk_content(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID, limit: int, offset: int
    ) -> list[Chunk]: ...
    def chunks_by_id(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID,
        chunk_ids: Sequence[UUID],
    ) -> list[Chunk]: ...
    def chunk(
        self, project_id: UUID, snapshot_id: UUID, chunk_id: UUID, run_id: UUID
    ) -> Chunk | None: ...
    def save(
        self, project_id: UUID, snapshot_id: UUID, run: ProcessingRun, activate: bool
    ) -> ProcessingRun: ...


@dataclass(frozen=True)
class ProcessingStatus:
    latest_attempt: ProcessingRun | None
    active_run: ProcessingRun | None
    active_freshness: Literal["fresh", "stale", "none"]
    freshness_reasons: tuple[str, ...] = ()


def processing_freshness(
    run: ProcessingRun,
    dependencies: list[tuple[UUID, str]],
    observed: dict[UUID, ArtifactAnalysis],
) -> tuple[str, ...]:
    """Pure Stage 5 freshness rule shared by later evidence publication gates."""
    if run.quarantined:
        return ("EVIDENCE_QUARANTINED",)
    if run.processing_contract != processing_contract():
        return ("PROCESSOR_CONTRACT_CHANGED",)
    if any(
        artifact_id not in observed
        or observed[artifact_id].outcome != "parsed"
        or dependency_fingerprint(observed[artifact_id]) != expected
        for artifact_id, expected in dependencies
    ):
        return ("STRUCTURAL_DEPENDENCY_CHANGED",)
    return ()


def dependency_fingerprint(artifact: ArtifactAnalysis) -> str:
    return digest(
        {
            "input": artifact.input_fingerprint,
            "contract": structural_contract(),
            "symbols": [
                [
                    str(s.stable_identity_id),
                    s.kind,
                    s.state_fingerprint,
                    s.full_start_line,
                    s.declaration_span.start_line,
                    s.declaration_span.end_line,
                ]
                for s in artifact.symbols
            ],
        }
    )


_TEXT_EXT = {"txt", "rst", "xml", "ini", "cfg", "conf"}


class ProcessingService:
    def __init__(
        self,
        store: ProcessingStore,
        lock: Lock | None = None,
        *,
        max_source_bytes: int = 1_048_576,
        max_total_bytes: int = 67_108_864,
        max_artifacts: int = 5000,
        max_chunks_per_artifact: int = 1024,
        max_chunks: int = 20000,
        overall_timeout_seconds: float = 60,
        reader: ProcessingWorker,
    ) -> None:
        if (
            min(
                max_source_bytes,
                max_total_bytes,
                max_artifacts,
                max_chunks_per_artifact,
                max_chunks,
                overall_timeout_seconds,
            )
            <= 0
            or max_source_bytes > max_total_bytes
            or max_chunks_per_artifact > max_chunks
        ):
            raise ValueError("Processing limits must be positive.")
        self._store, self._lock = store, lock or Lock()
        self._limits = {
            "max_source_bytes": max_source_bytes,
            "max_total_bytes": max_total_bytes,
            "max_artifacts": max_artifacts,
            "max_chunk_chars": 4096,
            "max_chunks_per_artifact": max_chunks_per_artifact,
            "max_chunks": max_chunks,
        }
        self._overall = overall_timeout_seconds
        self._reader = reader

    def require_snapshot(self, project_id: UUID, snapshot_id: UUID) -> ProjectVersion:
        if self._store.get_project(project_id) is None:
            raise ProjectNotFoundError("Project was not found.")
        snapshot = self._store.get_snapshot(project_id, snapshot_id)
        if snapshot is None:
            raise SnapshotNotFoundError("Repository snapshot was not found.")
        return snapshot

    def process(self, project_id: UUID, snapshot_id: UUID) -> ProcessingRun:
        snapshot = self.require_snapshot(project_id, snapshot_id)
        if snapshot.legacy_unverified or not snapshot.canonical_root:
            raise ValueError("Snapshot has no authoritative source evidence.")
        if os.name != "posix":
            raise ProcessingUnavailableError("No secure reader is available on this platform.")
        if not self._lock.acquire(False):
            raise ProcessingBusyError("Another processing operation is running.")
        try:
            started = datetime.now(UTC)
            deadline = time.monotonic() + self._overall
            active, latest = (
                self._store.active(project_id, snapshot_id),
                self._store.latest(project_id, snapshot_id),
            )
            structural = self._store.active_structural(project_id, snapshot_id)
            structures = self._store.structural_artifacts(structural.id) if structural else {}
            reusable = self._store.reusable(project_id, snapshot)
            results: list[ArtifactProcessing] = []
            consumed = eligible = count = 0
            run_id = uuid4()
            for artifact in self._store.artifacts(project_id, snapshot_id):
                metadata = artifact.metadata
                if (
                    not metadata
                    or artifact.kind != "file"
                    or metadata.inventory_status != "included"
                    or metadata.pruned
                    or metadata.ignore_status != "not_ignored"
                    or metadata.exclusion_reasons
                    or exclusion_reason(artifact.locator, False)
                ):
                    results.append(
                        self._outcome(snapshot_id, artifact, "excluded", "SNAPSHOT_INELIGIBLE")
                    )
                    continue
                suffix = PurePosixPath(artifact.locator).suffix.lstrip(".").lower()
                if suffix not in {
                    "py",
                    "md",
                    "json",
                    "toml",
                    "yaml",
                    "yml",
                    *_TEXT_EXT,
                } and PurePosixPath(artifact.locator).name.upper() not in {
                    "README",
                    "LICENSE",
                    "NOTICE",
                    "CHANGELOG",
                }:
                    results.append(
                        self._outcome(snapshot_id, artifact, "unsupported", "UNSUPPORTED_ARTIFACT")
                    )
                    continue
                eligible += 1
                if eligible > self._limits["max_artifacts"] or time.monotonic() >= deadline:
                    results.append(
                        self._outcome(snapshot_id, artifact, "skipped", "OPERATION_LIMIT")
                    )
                    continue
                if (
                    not artifact.content_fingerprint
                    or artifact.fingerprint_status != "fingerprinted"
                ):
                    results.append(
                        self._outcome(
                            snapshot_id, artifact, "skipped", "SNAPSHOT_FINGERPRINT_UNAVAILABLE"
                        )
                    )
                    continue
                structure = structures.get(artifact.id) if suffix == "py" else None
                if suffix == "py" and (
                    not structure
                    or not structure.snapshot_verified
                    or structure.outcome != "parsed"
                ):
                    results.append(
                        self._outcome(
                            snapshot_id, artifact, "failed", "STRUCTURAL_PREREQUISITE_MISSING"
                        )
                    )
                    continue
                if (
                    artifact.fingerprint_size_bytes is None
                    or artifact.fingerprint_size_bytes > self._limits["max_source_bytes"]
                ):
                    results.append(
                        self._outcome(snapshot_id, artifact, "skipped", "SOURCE_TOO_LARGE")
                    )
                    continue
                dependency = dependency_fingerprint(structure) if structure else None
                cached = (
                    reusable.get(artifact.stable_identity_id)
                    if artifact.stable_identity_id
                    else None
                )
                processing_id = uuid4()
                remaining_chunks = min(
                    self._limits["max_chunks_per_artifact"], self._limits["max_chunks"] - count
                )
                if (
                    cached
                    and cached.input_fingerprint == artifact.content_fingerprint.digest
                    and cached.dependency_fingerprint == dependency
                    and len(cached.chunks) <= remaining_chunks
                ):
                    symbols = (
                        {s.stable_identity_id: s.id for s in structure.symbols} if structure else {}
                    )
                    results.append(
                        replace(
                            cached,
                            id=processing_id,
                            processing_run_id=run_id,
                            project_version_id=snapshot_id,
                            artifact_id=artifact.id,
                            locator=artifact.locator,
                            execution_mode="reused",
                            reuse_source_artifact_processing_id=cached.id,
                            structural_artifact_analysis_id=structure.id if structure else None,
                            chunks=tuple(
                                replace(
                                    c,
                                    id=uuid4(),
                                    artifact_processing_id=processing_id,
                                    symbol_id=symbols.get(c.symbol_identity_id)
                                    if c.symbol_id
                                    else None,
                                )
                                for c in cached.chunks
                            ),
                        )
                    )
                    count += len(cached.chunks)
                    continue
                if remaining_chunks <= 0:
                    results.append(self._outcome(snapshot_id, artifact, "skipped", "CHUNK_LIMIT"))
                    continue
                remaining_bytes = min(
                    self._limits["max_source_bytes"], self._limits["max_total_bytes"] - consumed
                )
                if remaining_bytes <= 0:
                    results.append(
                        self._outcome(snapshot_id, artifact, "skipped", "TOTAL_SOURCE_LIMIT")
                    )
                    continue
                observed = snapshot.root_observation
                expected = (
                    (int(str(observed["device"])), int(str(observed["inode"])))
                    if observed
                    else None
                )
                try:
                    self._reader.timeout = min(3, max(0.001, deadline - time.monotonic()))
                    raw = self._reader.read(
                        snapshot.canonical_root, artifact.locator, remaining_bytes, expected
                    ).data
                    consumed += len(raw)
                    if hashlib.sha256(raw).hexdigest() != artifact.content_fingerprint.digest:
                        results.append(
                            self._outcome(
                                snapshot_id, artifact, "failed", "SOURCE_DIFFERS_FROM_SNAPSHOT"
                            )
                        )
                        continue
                    source, encoding = decode_source(raw, suffix == "py")
                    screen_content(source)
                    chunks, diagnostics, contract = self._reader.process(
                        source,
                        suffix,
                        processing_id,
                        str(artifact.stable_identity_id or artifact.id),
                        structure.symbols if structure else (),
                        remaining_chunks,
                        min(3, max(0.001, deadline - time.monotonic())),
                    )
                except SourceReadError as error:
                    consumed += error.bytes_consumed
                    results.append(
                        self._outcome(
                            snapshot_id,
                            artifact,
                            "skipped"
                            if error.code.endswith("LIMIT") or error.code == "SOURCE_TOO_LARGE"
                            else "failed",
                            error.code,
                        )
                    )
                    continue
                except ContentRejectedError as error:
                    results.append(self._outcome(snapshot_id, artifact, "excluded", error.code))
                    continue
                except (UnicodeError, LookupError, SyntaxError):
                    results.append(
                        self._outcome(snapshot_id, artifact, "failed", "INVALID_ENCODING")
                    )
                    continue
                outcome: ArtifactProcessingOutcome = (
                    "failed"
                    if any(d.severity == "error" for d in diagnostics)
                    else "processed"
                    if chunks
                    else "empty"
                )
                if outcome == "failed":
                    chunks = ()
                count += len(chunks)
                results.append(
                    ArtifactProcessing(
                        processing_id,
                        run_id,
                        snapshot_id,
                        artifact.id,
                        artifact.locator,
                        outcome,
                        contract,
                        artifact.content_fingerprint.digest,
                        encoding,
                        diagnostics,
                        chunks,
                        result_digest=digest(
                            [[c.stable_key, c.content_hash, c.span.start_offset] for c in chunks]
                        ),
                        structural_artifact_analysis_id=structure.id if structure else None,
                        dependency_fingerprint=dependency,
                    )
                )
            owned = tuple(replace(value, processing_run_id=run_id) for value in results)
            successful = sum(x.outcome in {"processed", "empty"} for x in owned)
            gaps = sum(
                x.outcome in {"failed", "skipped"}
                or (x.outcome == "processed" and bool(x.diagnostics))
                for x in owned
            )
            outcome_run: Literal["complete", "partial", "failed"] = (
                "complete"
                if successful and not gaps and snapshot.scan_status == "complete"
                else "partial"
                if successful
                else "failed"
            )
            run = ProcessingRun(
                run_id,
                snapshot_id,
                started,
                datetime.now(UTC),
                outcome_run,
                structural.id if structural else None,
                dict(processing_contract()),
                self._limits,
                {
                    "artifacts": len(owned),
                    "eligible": eligible,
                    "successful": successful,
                    "chunks": count,
                    "gaps": gaps,
                    "bytes_consumed": consumed,
                    "reused": sum(x.execution_mode == "reused" for x in owned),
                },
                (),
                owned,
                captured_active_processing_run_id=active.id if active else None,
                captured_latest_revision=latest.revision if latest else 0,
            )
            return self._store.save(project_id, snapshot_id, run, successful > 0)
        finally:
            self._reader.close()
            self._lock.release()

    @staticmethod
    def _outcome(
        version: UUID, artifact: Artifact, outcome: ArtifactProcessingOutcome, code: str
    ) -> ArtifactProcessing:
        return ArtifactProcessing(
            uuid4(),
            UUID(int=0),
            version,
            artifact.id,
            artifact.locator,
            outcome,
            None,
            artifact.content_fingerprint.digest if artifact.content_fingerprint else None,
            None,
            (
                ProcessingDiagnostic(
                    code,
                    "Artifact was not processed.",
                    "error" if outcome == "failed" else "warning",
                ),
            ),
        )

    def freshness(self, project_id: UUID, snapshot_id: UUID, run: ProcessingRun) -> tuple[str, ...]:
        dependencies = self._store.dependencies(run.id)
        structural = self._store.active_structural(project_id, snapshot_id)
        observed = self._store.structural_artifacts(structural.id) if structural else {}
        return processing_freshness(run, dependencies, observed)

    def status(self, project_id: UUID, snapshot_id: UUID) -> ProcessingStatus:
        self.require_snapshot(project_id, snapshot_id)
        latest, active = (
            self._store.latest(project_id, snapshot_id),
            self._store.active(project_id, snapshot_id),
        )
        reasons = self.freshness(project_id, snapshot_id, active) if active else ()
        return ProcessingStatus(
            latest, active, "stale" if reasons else "fresh" if active else "none", reasons
        )

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> ProcessingRun:
        self.require_snapshot(project_id, snapshot_id)
        run = self._store.run(project_id, snapshot_id, run_id)
        if run is None:
            raise ProcessingNotFoundError("Processing run was not found.")
        return run

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[ProcessingRun], int]:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.history(project_id, snapshot_id, limit, offset)

    def select_run(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None, content: bool
    ) -> ProcessingRun:
        self.require_snapshot(project_id, snapshot_id)
        run = (
            self.run(project_id, snapshot_id, run_id)
            if run_id
            else self._store.active(project_id, snapshot_id)
            if content
            else self._store.latest(project_id, snapshot_id)
        )
        if run is None:
            raise CurrentContentUnavailableError("No active evidence is available.")
        if content and (
            run.quarantined
            or run.processing_contract != processing_contract()
            or (run_id is None and self.freshness(project_id, snapshot_id, run))
        ):
            raise CurrentContentUnavailableError(
                "Evidence is quarantined or incompatible; regenerate explicitly."
            )
        return run

    def artifact_page(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None, limit: int, offset: int
    ) -> tuple[ProcessingRun, list[ArtifactProcessing], int]:
        run = self.select_run(project_id, snapshot_id, run_id, False)
        values, total = self._store.page_artifacts(project_id, snapshot_id, run.id, limit, offset)
        return run, values, total

    def chunk_page(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None, limit: int, offset: int
    ) -> tuple[ProcessingRun, list[Chunk], int]:
        run = self.select_run(project_id, snapshot_id, run_id, True)
        values, total = self._store.page_chunks(project_id, snapshot_id, run.id, limit, offset)
        return run, values, total

    def chunk(
        self, project_id: UUID, snapshot_id: UUID, chunk_id: UUID, run_id: UUID | None = None
    ) -> Chunk:
        run = self.select_run(project_id, snapshot_id, run_id, True)
        chunk = self._store.chunk(project_id, snapshot_id, chunk_id, run.id)
        if not chunk or chunk.run_id != run.id:
            raise ProcessingNotFoundError("Chunk was not found in the selected run.")
        return replace(chunk, freshness_reasons=self.freshness(project_id, snapshot_id, run))

    def current_authority(self, project_id: UUID, snapshot_id: UUID) -> ProcessingRun:
        """Return current Stage 5 content authority through the public freshness gate."""
        return self.select_run(project_id, snapshot_id, None, True)

    def current_content_stats(
        self, project_id: UUID, snapshot_id: UUID, expected_run: ProcessingRun
    ) -> tuple[int, int]:
        run = self.current_authority(project_id, snapshot_id)
        self._require_same_authority(run, expected_run)
        return self._store.content_stats(project_id, snapshot_id, run.id)

    def current_content_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        expected_run: ProcessingRun,
        limit: int,
        offset: int,
    ) -> list[Chunk]:
        if limit < 1 or offset < 0:
            raise ValueError("Content page bounds are invalid.")
        run = self.current_authority(project_id, snapshot_id)
        self._require_same_authority(run, expected_run)
        return self._store.page_chunk_content(project_id, snapshot_id, run.id, limit, offset)

    def current_chunks(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        expected_run: ProcessingRun,
        chunk_ids: Sequence[UUID],
    ) -> list[Chunk]:
        run = self.current_authority(project_id, snapshot_id)
        self._require_same_authority(run, expected_run)
        if len(chunk_ids) > 50:
            raise ValueError("At most 50 current chunks may be read at once.")
        return self._store.chunks_by_id(project_id, snapshot_id, run.id, chunk_ids)

    @staticmethod
    def _require_same_authority(current: ProcessingRun, expected: ProcessingRun) -> None:
        if (
            current.id != expected.id
            or current.revision != expected.revision
            or current.processing_contract != expected.processing_contract
        ):
            raise ProcessingConflictError("Current processing authority changed.")
