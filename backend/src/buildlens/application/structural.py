"""Explicit structural-analysis use case and its platform-neutral ports."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock
from typing import Literal, Protocol
from uuid import UUID, uuid4

from buildlens.application.projects import ProjectNotFoundError
from buildlens.application.snapshots import SnapshotNotFoundError
from buildlens.application.source_ports import SourceReadError
from buildlens.domain.architecture_literals import ROWS_PER_RUN
from buildlens.domain.eligibility import exclusion_reason
from buildlens.domain.entities import Artifact, Project, ProjectVersion
from buildlens.domain.extractors import (
    LITERAL_EXTRACTORS,
    PYTHON_FACTS,
    extractor_contract_digest,
    language_for,
)
from buildlens.domain.source import PARSER_VERSION, structural_contract
from buildlens.domain.structural import (
    ArtifactAnalysis,
    ArtifactExtraction,
    ImportReference,
    ParseDiagnostic,
    StructuralAnalysis,
    Symbol,
)


class AnalysisNotFoundError(Exception):
    pass


class AnalysisContentUnavailableError(Exception):
    pass


class AnalysisBusyError(Exception):
    pass


class AnalysisConflictError(Exception):
    pass


class AnalysisUnavailableError(Exception):
    pass


class StructuralAnalysisStore(Protocol):
    def get_project(self, project_id: UUID) -> Project | None: ...
    def get_snapshot(self, project_id: UUID, snapshot_id: UUID) -> ProjectVersion | None: ...
    def candidate_artifacts(self, project_id: UUID, snapshot_id: UUID) -> list[Artifact]: ...
    def latest_attempt(self, project_id: UUID, snapshot_id: UUID) -> StructuralAnalysis | None: ...
    def run(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID
    ) -> StructuralAnalysis | None: ...
    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[StructuralAnalysis], int]: ...
    def latest_analysis(self, project_id: UUID, snapshot_id: UUID) -> StructuralAnalysis | None: ...
    def reusable_artifact(
        self, project_id: UUID, snapshot_id: UUID, artifact_id: UUID
    ) -> ArtifactAnalysis | None: ...
    def baseline_artifact(
        self, project_id: UUID, snapshot_id: UUID, artifact_id: UUID
    ) -> ArtifactAnalysis | None: ...
    def save_replacement(
        self, project_id: UUID, snapshot_id: UUID, analysis: StructuralAnalysis
    ) -> StructuralAnalysis: ...
    def artifact_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        limit: int,
        offset: int,
        outcome: str | None,
        run_id: UUID | None = None,
    ) -> tuple[list[ArtifactAnalysis], int, UUID | None]: ...
    def symbol_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        limit: int,
        offset: int,
        artifact_id: UUID | None,
        kind: str | None,
        name: str | None,
        run_id: UUID | None = None,
        current_only: bool = False,
    ) -> tuple[list[Symbol], int, UUID | None]: ...
    def symbol(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        symbol_id: UUID,
        run_id: UUID | None = None,
        current_only: bool = False,
    ) -> Symbol | None: ...
    def import_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        limit: int,
        offset: int,
        artifact_id: UUID | None,
        owner_id: UUID | None,
        run_id: UUID | None = None,
        current_only: bool = False,
    ) -> tuple[list[ImportReference], int, UUID | None]: ...


class ArtifactAnalyzer(Protocol):
    def analyze(
        self,
        analysis_id: UUID,
        version_id: UUID,
        artifact_id: UUID,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None,
        timeout: float,
        language: str = "Python",
    ) -> ArtifactAnalysis: ...
    def extract_facts(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None,
        timeout: float,
        symbols: tuple[Symbol, ...],
    ) -> tuple[ArtifactExtraction, str, int]: ...
    def extract_additional(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None,
        timeout: float,
        language: str,
        extractors: tuple[str, ...],
    ) -> tuple[tuple[ArtifactExtraction, ...], str, int]: ...
    def close(self) -> None: ...


def _language(artifact: Artifact) -> str | None:
    language = artifact.metadata.language if artifact.metadata else None
    return language if language_for(language, artifact.locator) else None


def _reused(extraction: ArtifactExtraction) -> ArtifactExtraction:
    return replace(
        extraction,
        id=uuid4(),
        execution_mode="reused",
        reuse_source_extraction_id=extraction.id,
    )


class StructuralAnalysisService:
    def __init__(
        self,
        store: StructuralAnalysisStore,
        analyzer: ArtifactAnalyzer,
        lock: Lock | None = None,
        *,
        max_source_bytes: int = 1_048_576,
        max_total_bytes: int = 67_108_864,
        max_artifacts: int = 5_000,
        artifact_timeout_seconds: float = 3,
        overall_timeout_seconds: float = 60,
    ) -> None:
        if (
            min(
                max_source_bytes,
                max_total_bytes,
                max_artifacts,
                artifact_timeout_seconds,
                overall_timeout_seconds,
            )
            <= 0
            or max_source_bytes > max_total_bytes
        ):
            raise ValueError("Structural limits must be positive and consistent.")
        self._artifact_timeout_seconds = artifact_timeout_seconds
        self._store, self._analyzer, self._lock = (
            store,
            analyzer,
            lock or Lock(),
        )
        (
            self._max_source_bytes,
            self._max_total_bytes,
            self._max_artifacts,
            self._overall_timeout_seconds,
        ) = max_source_bytes, max_total_bytes, max_artifacts, overall_timeout_seconds

    def analyse(self, project_id: UUID, snapshot_id: UUID) -> StructuralAnalysis:
        project = self._store.get_project(project_id)
        if project is None:
            raise ProjectNotFoundError(f"Project {project_id} was not found.")
        snapshot = self._store.get_snapshot(project_id, snapshot_id)
        if snapshot is None:
            raise SnapshotNotFoundError("Repository snapshot was not found.")
        if not snapshot.canonical_root or snapshot.scan_status not in {"complete", "partial"}:
            raise ValueError("Snapshot is not eligible for structural analysis.")
        if os.name != "posix":
            raise AnalysisUnavailableError(
                "No secure source-reader adapter is available on this platform."
            )
        if not self._lock.acquire(blocking=False):
            raise AnalysisBusyError("Another structural analysis is already running.")
        try:
            started = datetime.now(UTC)
            deadline = time.monotonic() + self._overall_timeout_seconds
            captured = self._store.latest_attempt(project_id, snapshot_id)
            predecessor_analysis = (
                self._store.latest_analysis(project_id, snapshot.predecessor_id)
                if snapshot.predecessor_id
                else None
            )
            analysis_id = uuid4()
            total = 0
            outcomes: list[ArtifactAnalysis] = []
            candidates = self._store.candidate_artifacts(project_id, snapshot_id)
            eligible = 0
            literal_rows = [0]
            for artifact in candidates:
                metadata = artifact.metadata
                if (
                    not metadata
                    or exclusion_reason(artifact.locator, False)
                    or metadata.ignore_status != "not_ignored"
                ):
                    outcomes.append(
                        self._skipped(analysis_id, snapshot_id, artifact, "CURRENT_POLICY_EXCLUDED")
                    )
                    continue
                support = language_for(_language(artifact), artifact.locator)
                if support is None:
                    code = (
                        "GENERATED_ARTIFACT"
                        if artifact.locator.lower().endswith(".min.js")
                        else "UNSUPPORTED_LANGUAGE"
                    )
                    outcomes.append(self._skipped(analysis_id, snapshot_id, artifact, code))
                    continue
                eligible += 1
                if eligible > self._max_artifacts or time.monotonic() > deadline:
                    outcomes.append(
                        self._skipped(analysis_id, snapshot_id, artifact, "OPERATION_LIMIT")
                    )
                    continue
                metadata = artifact.metadata
                if (
                    not metadata
                    or metadata.size_bytes is None
                    or metadata.size_bytes > self._max_source_bytes
                ):
                    outcomes.append(
                        self._skipped(analysis_id, snapshot_id, artifact, "SOURCE_TOO_LARGE")
                    )
                    continue
                if total + metadata.size_bytes > self._max_total_bytes:
                    outcomes.append(
                        self._skipped(analysis_id, snapshot_id, artifact, "TOTAL_SOURCE_LIMIT")
                    )
                    continue
                baseline = self._store.baseline_artifact(project_id, snapshot_id, artifact.id)
                reusable = self._store.reusable_artifact(project_id, snapshot_id, artifact.id)
                expected_root = (
                    (
                        int(str(snapshot.root_observation["device"])),
                        int(str(snapshot.root_observation["inode"])),
                    )
                    if snapshot.root_observation
                    else None
                )
                if reusable is not None:
                    extractions: list[ArtifactExtraction] = []
                    declaration_id: UUID | None = None
                    source_declaration = reusable.declaration_extraction
                    if source_declaration is not None:
                        copied = _reused(source_declaration)
                        extractions.append(copied)
                        declaration_id = copied.id
                    missing: list[str] = []
                    for name in support.additional_extractors:
                        current = next(
                            (
                                item
                                for item in reusable.extractions
                                if item.extractor == name
                                and item.outcome == "extracted"
                                and item.contract_digest == extractor_contract_digest(name)
                                and item.input_fingerprint == reusable.input_fingerprint
                            ),
                            None,
                        )
                        if current is not None:
                            reused = _reused(current)
                            extractions.append(self._cap(reused, literal_rows))
                            literal_rows[0] += len(extractions[-1].literals)
                            continue
                        if name in LITERAL_EXTRACTORS:
                            missing.append(name)
                            continue
                        # Declarations stay reused (identities preserved); only the additive
                        # pass reads the source again.
                        extracted, consumed = self._additional(
                            name,
                            snapshot.canonical_root,
                            artifact,
                            reusable,
                            min(self._max_source_bytes, self._max_total_bytes - total),
                            expected_root,
                            min(
                                self._artifact_timeout_seconds,
                                max(0.001, deadline - time.monotonic()),
                            ),
                        )
                        total += consumed
                        extractions.append(extracted)
                    if missing:
                        # Stage 14: one verified read for every missing literal pass.
                        added, consumed = self._literals(
                            tuple(missing),
                            support.language,
                            snapshot.canonical_root,
                            artifact,
                            reusable,
                            min(self._max_source_bytes, self._max_total_bytes - total),
                            expected_root,
                            min(
                                self._artifact_timeout_seconds,
                                max(0.001, deadline - time.monotonic()),
                            ),
                        )
                        total += consumed
                        for value in added:
                            extractions.append(self._cap(value, literal_rows))
                            literal_rows[0] += len(extractions[-1].literals)
                    outcomes.append(
                        self._owned(
                            replace(
                                reusable,
                                locator=artifact.locator,
                                artifact_id=artifact.id,
                                read_started_at=None,
                                read_completed_at=None,
                                execution_mode="reused",
                                baseline_artifact_analysis_id=reusable.id,
                                reuse_source_artifact_analysis_id=reusable.id,
                                language=support.language,
                                declaration_extraction_id=declaration_id,
                                extractions=tuple(extractions),
                            ),
                            analysis_id,
                            snapshot_id,
                        )
                    )
                    continue
                try:
                    result = self._analyzer.analyze(
                        analysis_id,
                        snapshot_id,
                        artifact.id,
                        snapshot.canonical_root,
                        artifact.locator,
                        min(self._max_source_bytes, self._max_total_bytes - total),
                        expected_root,
                        min(
                            self._artifact_timeout_seconds, max(0.001, deadline - time.monotonic())
                        ),
                        language=support.language,
                    )
                    total += result.bytes_consumed
                except SourceReadError as error:
                    total += error.bytes_consumed
                    outcomes.append(
                        replace(
                            self._skipped(analysis_id, snapshot_id, artifact, error.code),
                            outcome="failed",
                            language=support.language,
                        )
                    )
                    continue
                if (
                    snapshot.legacy_unverified
                    or artifact.content_fingerprint is None
                    or artifact.fingerprint_status != "fingerprinted"
                ):
                    result = replace(
                        result,
                        outcome="failed",
                        symbols=(),
                        imports=(),
                        extractions=(),
                        declaration_extraction_id=None,
                        snapshot_verified=False,
                        diagnostics=(
                            *result.diagnostics,
                            ParseDiagnostic(
                                "SNAPSHOT_FINGERPRINT_UNAVAILABLE",
                                "Snapshot-time source fingerprint is unavailable.",
                                "error",
                            ),
                        ),
                    )
                elif result.input_fingerprint != artifact.content_fingerprint.digest:
                    result = replace(
                        result,
                        outcome="failed",
                        symbols=(),
                        imports=(),
                        extractions=(),
                        declaration_extraction_id=None,
                        snapshot_verified=False,
                        diagnostics=(
                            *result.diagnostics,
                            ParseDiagnostic(
                                "SOURCE_DIFFERS_FROM_SNAPSHOT",
                                "Source changed after the snapshot was observed.",
                                "error",
                            ),
                        ),
                    )
                else:
                    capped = []
                    for value in result.extractions:
                        capped.append(self._cap(value, literal_rows))
                        literal_rows[0] += len(capped[-1].literals)
                    result = replace(
                        result,
                        snapshot_verified=True,
                        baseline_artifact_analysis_id=baseline.id if baseline else None,
                        extractions=tuple(capped),
                    )
                outcomes.append(self._owned(result, analysis_id, snapshot_id))
            parsed = sum(item.outcome == "parsed" for item in outcomes)
            issue = (
                any(item.outcome != "parsed" for item in outcomes)
                or snapshot.scan_status == "partial"
            )
            outcome: Literal["complete", "partial", "failed"] = (
                "complete" if not issue else "partial" if parsed else "failed"
            )
            analysis = StructuralAnalysis(
                analysis_id,
                snapshot_id,
                started,
                datetime.now(UTC),
                outcome,
                PARSER_VERSION,
                sys.version.split()[0],
                snapshot.policy_version or "unknown",
                {
                    "max_source_bytes": self._max_source_bytes,
                    "max_total_bytes": self._max_total_bytes,
                    "max_artifacts": self._max_artifacts,
                },
                (),
                tuple(outcomes),
                captured_predecessor_analysis_id=(
                    predecessor_analysis.id if predecessor_analysis else None
                ),
                producer_contract=structural_contract(),
                captured_active_analysis_id=snapshot.active_analysis_id,
                captured_latest_revision=captured.revision if captured else 0,
            )
            try:
                return self._store.save_replacement(project_id, snapshot_id, analysis)
            except AnalysisConflictError:
                raise
            except Exception as error:
                raise AnalysisUnavailableError(
                    "Structural analysis could not be persisted."
                ) from error
        finally:
            self._analyzer.close()
            self._lock.release()

    def _additional(
        self,
        extractor: str,
        root: str,
        artifact: Artifact,
        reusable: ArtifactAnalysis,
        max_bytes: int,
        expected_root: tuple[int, int] | None,
        timeout: float,
    ) -> tuple[ArtifactExtraction, int]:
        """Run one additive pass for reused declarations, verified against the snapshot."""
        if extractor != PYTHON_FACTS:
            raise ValueError(f"No additive runner for {extractor!r}")
        try:
            extraction, fingerprint, consumed = self._analyzer.extract_facts(
                root, artifact.locator, max_bytes, expected_root, timeout, reusable.symbols
            )
        except SourceReadError as error:
            return self._failed_extraction(extractor, error.code), error.bytes_consumed
        if (
            artifact.content_fingerprint is None
            or fingerprint != artifact.content_fingerprint.digest
            or fingerprint != reusable.input_fingerprint
        ):
            return self._failed_extraction(extractor, "SOURCE_DIFFERS_FROM_SNAPSHOT"), consumed
        return extraction, consumed

    def _literals(
        self,
        extractors: tuple[str, ...],
        language: str,
        root: str,
        artifact: Artifact,
        reusable: ArtifactAnalysis,
        max_bytes: int,
        expected_root: tuple[int, int] | None,
        timeout: float,
    ) -> tuple[tuple[ArtifactExtraction, ...], int]:
        """Stage 14 literal passes for reused declarations; failures stay confined to the pass."""
        try:
            values, fingerprint, consumed = self._analyzer.extract_additional(
                root, artifact.locator, max_bytes, expected_root, timeout, language, extractors
            )
        except SourceReadError as error:
            return (
                tuple(self._failed_extraction(name, error.code) for name in extractors),
                error.bytes_consumed,
            )
        if (
            artifact.content_fingerprint is None
            or fingerprint != artifact.content_fingerprint.digest
            or fingerprint != reusable.input_fingerprint
        ):
            return (
                tuple(
                    self._failed_extraction(name, "SOURCE_DIFFERS_FROM_SNAPSHOT")
                    for name in extractors
                ),
                consumed,
            )
        return values, consumed

    def _cap(self, extraction: ArtifactExtraction, used: list[int]) -> ArtifactExtraction:
        """Fail a literal pass that would exceed the per-run row budget (never truncate it)."""
        if extraction.extractor not in LITERAL_EXTRACTORS:
            return extraction
        if used[0] + len(extraction.literals) <= ROWS_PER_RUN:
            return extraction
        return self._failed_extraction(extraction.extractor, "LITERAL_RUN_LIMIT")

    @staticmethod
    def _failed_extraction(extractor: str, code: str) -> ArtifactExtraction:
        from buildlens.domain.extractors import extractor_contract

        contract = extractor_contract(extractor) or {}
        digest_value = extractor_contract_digest(extractor) or "0" * 64
        return ArtifactExtraction(
            uuid4(),
            extractor,
            contract,
            digest_value,
            "failed",
            None,
            (ParseDiagnostic(code, "Additive extraction did not complete.", "error"),),
        )

    @staticmethod
    def _skipped(
        analysis_id: UUID, version_id: UUID, artifact: Artifact, code: str
    ) -> ArtifactAnalysis:
        return ArtifactAnalysis(
            uuid4(),
            analysis_id,
            version_id,
            artifact.id,
            artifact.locator,
            "skipped",
            None,
            None,
            None,
            None,
            None,
            (ParseDiagnostic(code, "Artifact was not read during this analysis.", "warning"),),
            language=_language(artifact),
        )

    @staticmethod
    def _owned(result: ArtifactAnalysis, analysis_id: UUID, version_id: UUID) -> ArtifactAnalysis:
        artifact_id = uuid4()
        symbol_ids: dict[UUID, UUID] = {}
        for symbol in result.symbols:
            symbol_ids[symbol.id] = uuid4()
        symbols = tuple(
            replace(
                value,
                id=symbol_ids[value.id],
                artifact_analysis_id=artifact_id,
                parent_id=symbol_ids.get(value.parent_id) if value.parent_id else None,
            )
            for value in result.symbols
        )
        imports = tuple(
            replace(
                value,
                id=uuid4(),
                artifact_analysis_id=artifact_id,
                owner_symbol_id=symbol_ids.get(value.owner_symbol_id)
                if value.owner_symbol_id
                else None,
            )
            for value in result.imports
        )

        def remap(value: UUID | None) -> UUID | None:
            return symbol_ids.get(value) if value else None

        extractions = tuple(
            replace(
                item,
                symbol_facts=tuple(
                    replace(fact, symbol_id=symbol_ids[fact.symbol_id])
                    for fact in item.symbol_facts
                    if fact.symbol_id in symbol_ids
                ),
                references=tuple(
                    replace(ref, owner_symbol_id=remap(ref.owner_symbol_id))
                    for ref in item.references
                ),
                exports=tuple(
                    replace(value, owner_symbol_id=remap(value.owner_symbol_id))
                    for value in item.exports
                ),
            )
            for item in result.extractions
        )
        return replace(
            result,
            id=artifact_id,
            structural_analysis_id=analysis_id,
            project_version_id=version_id,
            symbols=symbols,
            imports=imports,
            extractions=extractions,
        )

    def require_snapshot(self, project_id: UUID, snapshot_id: UUID) -> None:
        if self._store.get_project(project_id) is None:
            raise ProjectNotFoundError("Project was not found.")
        if self._store.get_snapshot(project_id, snapshot_id) is None:
            raise SnapshotNotFoundError("Repository snapshot was not found.")

    def latest_analysis(self, project_id: UUID, snapshot_id: UUID) -> StructuralAnalysis | None:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.latest_analysis(project_id, snapshot_id)

    def latest_attempt(self, project_id: UUID, snapshot_id: UUID) -> StructuralAnalysis | None:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.latest_attempt(project_id, snapshot_id)

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[StructuralAnalysis], int]:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.history(project_id, snapshot_id, limit, offset)

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> StructuralAnalysis | None:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.run(project_id, snapshot_id, run_id)

    def artifact_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        limit: int,
        offset: int,
        outcome: str | None,
        run_id: UUID | None = None,
    ) -> tuple[list[ArtifactAnalysis], int, UUID | None]:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.artifact_page(
            project_id,
            snapshot_id,
            limit,
            offset,
            outcome,
            self.content_run(project_id, snapshot_id, run_id),
        )

    def symbol_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        limit: int,
        offset: int,
        artifact_id: UUID | None,
        kind: str | None,
        name: str | None,
        run_id: UUID | None = None,
    ) -> tuple[list[Symbol], int, UUID | None]:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.symbol_page(
            project_id,
            snapshot_id,
            limit,
            offset,
            artifact_id,
            kind,
            name,
            self.content_run(project_id, snapshot_id, run_id),
            current_only=run_id is None,
        )

    def symbol(
        self, project_id: UUID, snapshot_id: UUID, symbol_id: UUID, run_id: UUID | None = None
    ) -> Symbol | None:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.symbol(
            project_id,
            snapshot_id,
            symbol_id,
            self.content_run(project_id, snapshot_id, run_id),
            current_only=run_id is None,
        )

    def import_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        limit: int,
        offset: int,
        artifact_id: UUID | None,
        owner_id: UUID | None,
        run_id: UUID | None = None,
    ) -> tuple[list[ImportReference], int, UUID | None]:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.import_page(
            project_id,
            snapshot_id,
            limit,
            offset,
            artifact_id,
            owner_id,
            self.content_run(project_id, snapshot_id, run_id),
            current_only=run_id is None,
        )

    def content_run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None) -> UUID:
        value = (
            self._store.run(project_id, snapshot_id, run_id)
            if run_id
            else self._store.latest_analysis(project_id, snapshot_id)
        )
        if run_id and value is None:
            raise AnalysisNotFoundError("Structural run was not found.")
        if value is None or value.quarantined or value.producer_contract != structural_contract():
            raise AnalysisContentUnavailableError("Compatible structural evidence is unavailable.")
        return value.id
