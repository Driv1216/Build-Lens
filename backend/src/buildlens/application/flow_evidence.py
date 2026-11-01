"""Stage 15 Milestone A: the explicit flow evidence run (D11) and its read model.

A flow evidence run is bound to the **active structural analysis** of a snapshot. It reads each
eligible Python/TS/TSX/JS artifact once, through the supervised secure reader, and requires the
bytes to equal both the snapshot fingerprint and the structural ``input_fingerprint``. It never
writes Stage 3–14 rows or pointers (INV-3), so no catalog, architecture or interpretation run
becomes stale because of it. A later structural run makes *this* run stale
(``STRUCTURAL_AUTHORITY_CHANGED``); a rebuild then reuses unchanged artifacts by
``(input_fingerprint, flow-pass contract)``, rebinding scope owners by stable identity and exact
declaration position.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from buildlens.application.source_ports import SourceReadError
from buildlens.domain.flow_evidence import (
    CALLSITES_PER_RUN,
    PYTHON_FLOW,
    TS_FLOW,
    VERSION_FILE_BYTES,
    ArtifactFlowCoverage,
    DisplayText,
    FlowEvidenceRun,
    FlowExtraction,
    FlowScope,
    FrameworkFact,
    ModuleFact,
    coverage_summary,
    flow_evidence_contract,
    flow_pass_digest,
)
from buildlens.domain.source import digest


class FlowEvidenceNotFoundError(Exception):
    pass


class FlowEvidenceRequiredError(Exception):
    pass


class FlowEvidenceStaleError(Exception):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("The flow evidence run is stale.")
        self.reasons = reasons


class FlowEvidenceBusyError(Exception):
    pass


class FlowEvidenceConflictError(Exception):
    pass


class FlowEvidenceUnavailableError(Exception):
    pass


class FlowStructureRequiredError(Exception):
    pass


class FlowEvidenceLimitError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


FLOW_LANGUAGES = {"Python": PYTHON_FLOW, "TypeScript": TS_FLOW, "JavaScript": TS_FLOW}


@dataclass(frozen=True, slots=True)
class SnapshotSource:
    canonical_root: str | None
    scan_status: str | None
    expected_root: tuple[int, int] | None
    legacy_unverified: bool


@dataclass(frozen=True, slots=True)
class OwnerKey:
    """How a scope owner is re-found in another structural run: identity plus position."""

    stable_identity_id: UUID | None
    position: tuple[int | None, int | None, int | None]


@dataclass(frozen=True, slots=True)
class ArtifactInput:
    artifact_analysis_id: UUID
    artifact_id: UUID
    locator: str
    language: str | None
    outcome: str
    input_fingerprint: str | None
    snapshot_digest: str | None
    size_bytes: int | None
    # Python: (id, start_line, start_column, name); TS/JS: (id, kind, start_offset, end_offset).
    symbols: tuple[tuple[Any, ...], ...] = ()
    owners: dict[UUID, OwnerKey] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StructuralInput:
    analysis_id: UUID
    revision: int
    artifacts: tuple[ArtifactInput, ...]


@dataclass(frozen=True, slots=True)
class VersionFileInput:
    artifact_id: UUID
    locator: str
    size_bytes: int | None
    snapshot_digest: str | None


@dataclass(frozen=True, slots=True)
class ReuseSource:
    run_id: UUID
    artifact_analysis_id: UUID
    extraction: FlowExtraction
    owners: dict[UUID, OwnerKey]


@dataclass(frozen=True, slots=True)
class FlowEvidencePayload:
    coverage: tuple[ArtifactFlowCoverage, ...]
    extractions: dict[UUID, FlowExtraction]
    facts: tuple[FrameworkFact, ...]
    version_files: tuple[dict[str, Any], ...] = ()
    module_facts: tuple[ModuleFact, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowEvidenceStatus:
    latest: FlowEvidenceRun | None
    active: FlowEvidenceRun | None
    freshness: str
    freshness_reasons: tuple[str, ...]
    contract: dict[str, Any]
    structure_available: bool


class FlowEvidenceStore(Protocol):
    def snapshot_source(self, project_id: UUID, snapshot_id: UUID) -> SnapshotSource | None: ...
    def active_structure(self, project_id: UUID, snapshot_id: UUID) -> tuple[UUID, int] | None: ...
    def structural_input(
        self, project_id: UUID, snapshot_id: UUID, analysis_id: UUID
    ) -> StructuralInput: ...
    def version_files(self, project_id: UUID, snapshot_id: UUID) -> list[VersionFileInput]: ...
    def reusable(
        self, project_id: UUID, input_fingerprint: str, contract_digest: str
    ) -> ReuseSource | None: ...
    def latest(self, project_id: UUID, snapshot_id: UUID) -> FlowEvidenceRun | None: ...
    def active(self, project_id: UUID, snapshot_id: UUID) -> FlowEvidenceRun | None: ...
    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> FlowEvidenceRun | None: ...
    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[FlowEvidenceRun], int]: ...
    def publish(self, run: FlowEvidenceRun, payload: FlowEvidencePayload) -> FlowEvidenceRun: ...
    def save_failure(self, run: FlowEvidenceRun, code: str) -> FlowEvidenceRun: ...
    def coverage_page(
        self, run_id: UUID, limit: int, offset: int, state: str | None
    ) -> tuple[list[ArtifactFlowCoverage], int]: ...
    def scopes(self, run_id: UUID, artifact_analysis_id: UUID) -> list[FlowScope]: ...
    def display_texts(self, run_id: UUID, artifact_analysis_id: UUID) -> list[DisplayText]: ...
    def framework_facts(self, run_id: UUID) -> list[FrameworkFact]: ...
    def module_facts(self, run_id: UUID) -> list[ModuleFact]: ...


class FlowSourceReader(Protocol):
    def extract_flow(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None,
        timeout: float,
        language: str,
        symbols: tuple[tuple[Any, ...], ...],
    ) -> tuple[Any, str, int]: ...
    def read_version_file(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None,
        timeout: float,
    ) -> tuple[Any, str, int]: ...
    def close(self) -> None: ...


def _failed_coverage(
    item: ArtifactInput, state: str, code: str, extractor: str | None
) -> ArtifactFlowCoverage:
    return ArtifactFlowCoverage(
        item.artifact_analysis_id,
        item.artifact_id,
        item.locator,
        item.language,
        state,  # type: ignore[arg-type]
        extractor,
        flow_pass_digest(extractor) if extractor else None,
        item.input_fingerprint,
        "none",
        None,
        None,
        0,
        0,
        ({"code": code, "severity": "error" if state == "failed" else "info"},),
    )


def rebind(source: ReuseSource, target: ArtifactInput) -> FlowExtraction | None:
    """Re-attach reused scopes to the target run's symbols; ``None`` when any owner is lost."""
    by_key: dict[OwnerKey, list[UUID]] = {}
    for symbol_id, key in target.owners.items():
        by_key.setdefault(key, []).append(symbol_id)
    scopes: list[FlowScope] = []
    for scope in source.extraction.scopes:
        if scope.owner_symbol_id is None:
            scopes.append(scope)
            continue
        owner = source.owners.get(scope.owner_symbol_id)
        matches = by_key.get(owner) if owner is not None else None
        if owner is None or owner.stable_identity_id is None or not matches or len(matches) != 1:
            return None
        scopes.append(replace(scope, owner_symbol_id=matches[0]))
    return replace(source.extraction, scopes=tuple(scopes))


class FlowEvidenceService:
    def __init__(
        self,
        store: FlowEvidenceStore,
        reader_factory: Callable[[], FlowSourceReader],
        lock: threading.Lock | None = None,
        *,
        max_source_bytes: int = 1_048_576,
        max_total_bytes: int = 268_435_456,
        artifact_timeout_seconds: float = 3,
        overall_timeout_seconds: float = 300,
    ) -> None:
        if (
            min(
                max_source_bytes, max_total_bytes, artifact_timeout_seconds, overall_timeout_seconds
            )
            <= 0
        ):
            raise ValueError("Flow evidence limits must be positive.")
        self._store = store
        self._reader_factory = reader_factory
        self._lock = lock or threading.Lock()
        self._max_source_bytes = max_source_bytes
        self._max_total_bytes = max_total_bytes
        self._artifact_timeout = artifact_timeout_seconds
        self._overall_timeout = overall_timeout_seconds

    # ----------------------------------------------------------------- authority

    def require_snapshot(self, project_id: UUID, snapshot_id: UUID) -> SnapshotSource:
        source = self._store.snapshot_source(project_id, snapshot_id)
        if source is None:
            raise FlowEvidenceNotFoundError("Snapshot not found.")
        return source

    def freshness(
        self, project_id: UUID, snapshot_id: UUID, run: FlowEvidenceRun
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if run.outcome == "failed":
            reasons.append("FLOW_EVIDENCE_FAILED")
        if digest(run.contract) != digest(flow_evidence_contract()):
            reasons.append("FLOW_EVIDENCE_CONTRACT_CHANGED")
        structure = self._store.active_structure(project_id, snapshot_id)
        if structure is None or structure != (run.structural_analysis_id, run.structural_revision):
            reasons.append("STRUCTURAL_AUTHORITY_CHANGED")
        return tuple(dict.fromkeys(reasons))

    def status(self, project_id: UUID, snapshot_id: UUID) -> FlowEvidenceStatus:
        self.require_snapshot(project_id, snapshot_id)
        latest = self._store.latest(project_id, snapshot_id)
        active = self._store.active(project_id, snapshot_id)
        reasons = self.freshness(project_id, snapshot_id, active) if active else ()
        return FlowEvidenceStatus(
            latest,
            active,
            "none" if active is None else "stale" if reasons else "fresh",
            reasons,
            flow_evidence_contract(),
            self._store.active_structure(project_id, snapshot_id) is not None,
        )

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[FlowEvidenceRun], int]:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.history(project_id, snapshot_id, limit, offset)

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> FlowEvidenceRun:
        self.require_snapshot(project_id, snapshot_id)
        value = self._store.run(project_id, snapshot_id, run_id)
        if value is None:
            raise FlowEvidenceNotFoundError("Flow evidence run not found.")
        return value

    def select(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None
    ) -> tuple[FlowEvidenceRun, tuple[str, ...]]:
        """Current reads need a fresh active run; explicit history is labelled (INV-4)."""
        if run_id is not None:
            run = self.run(project_id, snapshot_id, run_id)
            if run.outcome == "failed":
                raise FlowEvidenceNotFoundError("Failed flow evidence runs hold no evidence.")
            return run, self.freshness(project_id, snapshot_id, run)
        self.require_snapshot(project_id, snapshot_id)
        active = self._store.active(project_id, snapshot_id)
        if active is None:
            raise FlowEvidenceRequiredError("Run flow evidence extraction for this snapshot.")
        reasons = self.freshness(project_id, snapshot_id, active)
        if reasons:
            raise FlowEvidenceStaleError(reasons)
        return active, ()

    # ----------------------------------------------------------------- extraction

    def extract(self, project_id: UUID, snapshot_id: UUID) -> FlowEvidenceRun:
        source = self.require_snapshot(project_id, snapshot_id)
        if not source.canonical_root or source.scan_status not in {"complete", "partial"}:
            raise ValueError("Snapshot is not eligible for flow evidence extraction.")
        if os.name != "posix":
            raise FlowEvidenceUnavailableError(
                "No secure source-reader adapter is available on this platform."
            )
        if not self._lock.acquire(blocking=False):
            raise FlowEvidenceBusyError("A flow evidence run is already in progress.")
        try:
            structure = self._store.active_structure(project_id, snapshot_id)
            if structure is None:
                raise FlowStructureRequiredError("Run structural analysis for this snapshot first.")
            analysis_id, structural_revision = structure
            contract = flow_evidence_contract()
            authority = digest(
                {
                    "snapshot": str(snapshot_id),
                    "structure": [str(analysis_id), structural_revision],
                    "contract": contract,
                }
            )
            active = self._store.active(project_id, snapshot_id)
            if (
                active is not None
                and active.input_authority_digest == authority
                and not self.freshness(project_id, snapshot_id, active)
            ):
                return active
            latest = self._store.latest(project_id, snapshot_id)
            started = datetime.now(UTC)
            run = FlowEvidenceRun(
                uuid4(),
                project_id,
                snapshot_id,
                0,
                started,
                started,
                "failed",
                analysis_id,
                structural_revision,
                contract,
                digest(contract),
                authority,
                None,
                captured_active_run_id=active.id if active else None,
                captured_latest_revision=latest.revision if latest else 0,
            )
            try:
                payload = self._collect(project_id, snapshot_id, source, analysis_id)
            except FlowEvidenceLimitError as error:
                self._store.save_failure(replace(run, completed_at=datetime.now(UTC)), error.code)
                raise
            states = {row.state for row in payload.coverage}
            outcome = "complete" if states <= {"extracted", "not_applicable"} else "partial"
            evidence = digest(
                {
                    "artifacts": sorted(
                        ([row.locator, row.state, row.evidence_digest] for row in payload.coverage),
                        key=json.dumps,
                    ),
                    "facts": sorted(
                        (
                            [
                                f.ecosystem,
                                f.package,
                                f.version,
                                f.constraint,
                                f.source_kind,
                                f.locator,
                                f.scope,
                            ]
                            for f in payload.facts
                        ),
                        key=json.dumps,
                    ),
                    "modules": sorted(
                        [m.kind, m.locator, m.input_fingerprint, digest(m.facts)]
                        for m in payload.module_facts
                    ),
                }
            )
            summary = coverage_summary(list(payload.coverage))
            summary["framework_facts"] = len(payload.facts)
            summary["module_facts"] = len(payload.module_facts)
            summary["version_files"] = list(payload.version_files)
            run = replace(
                run,
                completed_at=datetime.now(UTC),
                outcome=outcome,  # type: ignore[arg-type]
                evidence_digest=evidence,
                coverage=summary,
            )
            try:
                return self._store.publish(run, payload)
            except FlowEvidenceConflictError:
                self._store.save_failure(run, "FLOW_AUTHORITY_CHANGED")
                raise
        finally:
            self._lock.release()

    def _collect(
        self, project_id: UUID, snapshot_id: UUID, source: SnapshotSource, analysis_id: UUID
    ) -> FlowEvidencePayload:
        inputs = self._store.structural_input(project_id, snapshot_id, analysis_id)
        deadline = time.monotonic() + self._overall_timeout
        total_bytes = 0
        callsites = 0
        coverage: list[ArtifactFlowCoverage] = []
        extractions: dict[UUID, FlowExtraction] = {}
        assert source.canonical_root is not None
        reader = self._reader_factory()
        try:
            for item in inputs.artifacts:
                extractor = FLOW_LANGUAGES.get(item.language or "")
                if extractor is None:
                    coverage.append(
                        _failed_coverage(item, "not_applicable", "LANGUAGE_NOT_COVERED", None)
                    )
                    continue
                contract_digest = flow_pass_digest(extractor)
                if item.outcome != "parsed" or item.input_fingerprint is None:
                    coverage.append(
                        _failed_coverage(item, "not_extracted", "STRUCTURE_NOT_PARSED", extractor)
                    )
                    continue
                if contract_digest is None:
                    coverage.append(
                        _failed_coverage(item, "failed", "LANGUAGE_UNAVAILABLE", extractor)
                    )
                    continue
                if source.legacy_unverified or item.snapshot_digest is None:
                    coverage.append(
                        _failed_coverage(
                            item, "failed", "SNAPSHOT_FINGERPRINT_UNAVAILABLE", extractor
                        )
                    )
                    continue
                if item.snapshot_digest != item.input_fingerprint:
                    coverage.append(
                        _failed_coverage(
                            item, "not_current", "SOURCE_DIFFERS_FROM_SNAPSHOT", extractor
                        )
                    )
                    continue
                reused = self._reuse(project_id, item, contract_digest)
                extraction: FlowExtraction | None = reused[0] if reused else None
                mode = "reused" if reused else "extracted"
                if extraction is None:
                    if time.monotonic() > deadline:
                        coverage.append(
                            _failed_coverage(item, "failed", "OPERATION_LIMIT", extractor)
                        )
                        continue
                    size = item.size_bytes or 0
                    if size > self._max_source_bytes:
                        coverage.append(
                            _failed_coverage(item, "failed", "SOURCE_TOO_LARGE", extractor)
                        )
                        continue
                    if total_bytes + size > self._max_total_bytes:
                        coverage.append(
                            _failed_coverage(item, "failed", "TOTAL_SOURCE_LIMIT", extractor)
                        )
                        continue
                    try:
                        value, fingerprint, consumed = reader.extract_flow(
                            source.canonical_root,
                            item.locator,
                            min(self._max_source_bytes, self._max_total_bytes - total_bytes),
                            source.expected_root,
                            min(self._artifact_timeout, max(0.001, deadline - time.monotonic())),
                            item.language or "",
                            item.symbols,
                        )
                    except SourceReadError as error:
                        total_bytes += error.bytes_consumed
                        coverage.append(_failed_coverage(item, "failed", error.code, extractor))
                        continue
                    total_bytes += consumed
                    if fingerprint != item.input_fingerprint:
                        coverage.append(
                            _failed_coverage(
                                item, "not_current", "SOURCE_DIFFERS_FROM_SNAPSHOT", extractor
                            )
                        )
                        continue
                    extraction = value
                assert extraction is not None
                if extraction.outcome == "failed":
                    code = (
                        extraction.diagnostics[0].code
                        if extraction.diagnostics
                        else "EXTRACTOR_FAILED"
                    )
                    coverage.append(_failed_coverage(item, "failed", code, extractor))
                    continue
                callsites += extraction.callsite_count
                if callsites > CALLSITES_PER_RUN:
                    raise FlowEvidenceLimitError("FLOW_RUN_CALLSITE_LIMIT")
                extractions[item.artifact_analysis_id] = extraction
                coverage.append(
                    ArtifactFlowCoverage(
                        item.artifact_analysis_id,
                        item.artifact_id,
                        item.locator,
                        item.language,
                        "extracted" if extraction.outcome == "extracted" else "partial",
                        extraction.extractor,
                        extraction.contract_digest,
                        extraction.input_fingerprint,
                        mode,  # type: ignore[arg-type]
                        reused[1] if reused else None,
                        extraction.evidence_digest,
                        len(extraction.scopes),
                        extraction.callsite_count,
                        tuple(
                            {"code": d.code, "severity": d.severity} for d in extraction.diagnostics
                        ),
                    )
                )
            facts, modules, version_files = self._versions(
                reader, project_id, snapshot_id, source, deadline
            )
        finally:
            reader.close()
        return FlowEvidencePayload(
            tuple(coverage), extractions, tuple(facts), tuple(version_files), tuple(modules)
        )

    def _reuse(
        self, project_id: UUID, item: ArtifactInput, contract_digest: str
    ) -> tuple[FlowExtraction, UUID] | None:
        assert item.input_fingerprint is not None
        found = self._store.reusable(project_id, item.input_fingerprint, contract_digest)
        if found is None:
            return None
        rebound = rebind(found, item)
        return (rebound, found.run_id) if rebound is not None else None

    def _versions(
        self,
        reader: FlowSourceReader,
        project_id: UUID,
        snapshot_id: UUID,
        source: SnapshotSource,
        deadline: float,
    ) -> tuple[list[FrameworkFact], list[ModuleFact], list[dict[str, Any]]]:
        """Version files read as data (D17). An unreadable file leaves no facts, only a state."""
        assert source.canonical_root is not None
        facts: list[FrameworkFact] = []
        modules: list[ModuleFact] = []
        states: list[dict[str, Any]] = []
        for item in self._store.version_files(project_id, snapshot_id):
            code: str | None = None
            if item.snapshot_digest is None:
                code = "SNAPSHOT_FINGERPRINT_UNAVAILABLE"
            elif (item.size_bytes or 0) > VERSION_FILE_BYTES:
                code = "VERSION_FILE_TOO_LARGE"
            elif time.monotonic() > deadline:
                code = "OPERATION_LIMIT"
            if code is None:
                try:
                    value, fingerprint, _ = reader.read_version_file(
                        source.canonical_root,
                        item.locator,
                        VERSION_FILE_BYTES,
                        source.expected_root,
                        min(self._artifact_timeout * 4, max(0.001, deadline - time.monotonic())),
                    )
                except SourceReadError as error:
                    code = error.code
                else:
                    if fingerprint != item.snapshot_digest:
                        code = "SOURCE_DIFFERS_FROM_SNAPSHOT"
                    elif isinstance(value, str):
                        code = value
                    else:
                        version_facts, module = value
                        facts.extend(version_facts)
                        if module is not None:
                            modules.append(
                                ModuleFact(
                                    module["kind"],
                                    item.locator,
                                    fingerprint,
                                    module["facts"],
                                    bool(module.get("truncated")),
                                )
                            )
            states.append(
                {
                    "locator": item.locator,
                    "state": "read" if code is None else "not_read",
                    "code": code,
                }
            )
        return facts, modules, states

    # ----------------------------------------------------------------- reads

    def coverage(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        state: str | None,
        limit: int,
        offset: int,
    ) -> tuple[FlowEvidenceRun, tuple[str, ...], list[ArtifactFlowCoverage], int]:
        run, reasons = self.select(project_id, snapshot_id, run_id)
        rows, total = self._store.coverage_page(run.id, limit, offset, state)
        return run, reasons, rows, total

    def artifact(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None, artifact_analysis_id: UUID
    ) -> tuple[FlowEvidenceRun, tuple[str, ...], list[FlowScope], list[DisplayText]]:
        run, reasons = self.select(project_id, snapshot_id, run_id)
        scopes = self._store.scopes(run.id, artifact_analysis_id)
        if not scopes:
            raise FlowEvidenceNotFoundError("No flow evidence for that artifact in this run.")
        return run, reasons, scopes, self._store.display_texts(run.id, artifact_analysis_id)

    def framework_facts(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None
    ) -> tuple[FlowEvidenceRun, tuple[str, ...], list[FrameworkFact]]:
        run, reasons = self.select(project_id, snapshot_id, run_id)
        return run, reasons, self._store.framework_facts(run.id)
