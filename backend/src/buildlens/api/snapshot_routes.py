from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from buildlens.api.dependencies import get_snapshot_service
from buildlens.api.snapshot_schemas import (
    ArtifactChangePageResponse,
    ArtifactChangeResponse,
    ArtifactPageResponse,
    ArtifactResponse,
    GitMetadataResponse,
    SnapshotResponse,
    SummaryResponse,
    WarningResponse,
)
from buildlens.application.projects import ProjectNotFoundError
from buildlens.application.snapshots import (
    ScanBusyError,
    SnapshotConflictError,
    SnapshotNotFoundError,
    SnapshotService,
)
from buildlens.domain.entities import Artifact, ProjectVersion
from buildlens.infrastructure.discovery.filesystem import InspectionError

router = APIRouter(prefix="/api/v1/projects/{project_id}/snapshots", tags=["snapshots"])


def _detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _snapshot(value: ProjectVersion) -> SnapshotResponse:
    assert (
        value.git_metadata
        and value.summary
        and value.canonical_root
        and value.scan_started_at
        and value.scan_completed_at
        and value.scan_status
        and value.policy_version
    )
    git = value.git_metadata
    summary = value.summary
    return SnapshotResponse(
        id=value.id,
        project_id=value.project_id,
        canonical_root=value.canonical_root,
        scan_started_at=value.scan_started_at,
        scan_completed_at=value.scan_completed_at,
        scan_status=value.scan_status,
        policy_version=value.policy_version,
        sequence=value.sequence,
        predecessor_id=value.predecessor_id,
        content_manifest=(
            None if value.content_manifest is None else value.content_manifest.digest
        ),
        fingerprint_coverage=(
            None
            if value.fingerprint_coverage is None
            else {
                "eligible_files": value.fingerprint_coverage.eligible_files,
                "fingerprinted_files": value.fingerprint_coverage.fingerprinted_files,
                "unavailable_files": value.fingerprint_coverage.unavailable_files,
                "not_eligible_files": value.fingerprint_coverage.not_eligible_files,
                "bytes_consumed": value.fingerprint_coverage.bytes_consumed,
                "complete": value.fingerprint_coverage.complete,
            }
        ),
        eligibility_provenance=value.eligibility_provenance,
        comparison_contract=value.comparison_contract,
        git_metadata=GitMetadataResponse(
            state=git.state,
            repository_root=git.repository_root,
            project_prefix=git.project_prefix,
            branch=git.branch,
            head_commit=git.head_commit,
            dirty=git.dirty,
            observed_at=git.observed_at,
        ),
        summary=SummaryResponse(**summary.__dict__)
        if hasattr(summary, "__dict__")
        else SummaryResponse(
            observed_by_kind=summary.observed_by_kind,
            included_regular_files=summary.included_regular_files,
            excluded_regular_files=summary.excluded_regular_files,
            pruned_directories=summary.pruned_directories,
            included_bytes=summary.included_bytes,
            language_counts=summary.language_counts,
            tag_counts=summary.tag_counts,
            exclusion_reason_counts=summary.exclusion_reason_counts,
            warning_count=summary.warning_count,
        ),
        warnings=[
            WarningResponse(code=warning.code, message=warning.message, locator=warning.locator)
            for warning in value.warnings
        ],
    )


def _artifact(value: Artifact) -> ArtifactResponse:
    assert value.metadata
    data = value.metadata
    return ArtifactResponse(
        id=value.id,
        kind=value.kind,
        locator=value.locator,
        inventory_status=data.inventory_status,
        size_bytes=data.size_bytes,
        language=data.language,
        tags=list(data.tags),
        content_hint=data.content_hint,
        ignore_status=data.ignore_status,
        exclusion_reasons=list(data.exclusion_reasons),
        pruned=data.pruned,
        stable_identity_id=value.stable_identity_id,
        continuity_reason=value.continuity_reason,
        content_fingerprint=(
            None if value.content_fingerprint is None else value.content_fingerprint.digest
        ),
        fingerprint_status=value.fingerprint_status,
        fingerprint_size_bytes=value.fingerprint_size_bytes,
        fingerprint_diagnostic=value.fingerprint_diagnostic,
    )


@router.post("", response_model=SnapshotResponse, status_code=status.HTTP_201_CREATED)
def create_snapshot(
    project_id: UUID, service: SnapshotService = Depends(get_snapshot_service)
) -> SnapshotResponse:
    try:
        return _snapshot(service.create(project_id))
    except ProjectNotFoundError as error:
        raise HTTPException(404, _detail("PROJECT_NOT_FOUND", "Project was not found.")) from error
    except ScanBusyError as error:
        raise HTTPException(409, _detail("SCAN_BUSY", str(error))) from error
    except SnapshotConflictError as error:
        raise HTTPException(409, _detail("SNAPSHOT_CONFLICT", str(error))) from error
    except InspectionError as error:
        raise HTTPException(error.status_code, _detail(error.code, error.message)) from error
    except Exception as error:
        raise HTTPException(
            503, _detail("SNAPSHOT_PERSISTENCE_FAILED", "Snapshot could not be saved.")
        ) from error


@router.get("/latest", response_model=SnapshotResponse)
def latest_snapshot(
    project_id: UUID, service: SnapshotService = Depends(get_snapshot_service)
) -> SnapshotResponse:
    try:
        return _snapshot(service.latest(project_id))
    except ProjectNotFoundError as error:
        raise HTTPException(404, _detail("PROJECT_NOT_FOUND", "Project was not found.")) from error
    except SnapshotNotFoundError as error:
        raise HTTPException(404, _detail("SNAPSHOT_NOT_FOUND", str(error))) from error


@router.get("/{snapshot_id}", response_model=SnapshotResponse)
def get_snapshot(
    project_id: UUID, snapshot_id: UUID, service: SnapshotService = Depends(get_snapshot_service)
) -> SnapshotResponse:
    try:
        return _snapshot(service.get(project_id, snapshot_id))
    except ProjectNotFoundError as error:
        raise HTTPException(404, _detail("PROJECT_NOT_FOUND", "Project was not found.")) from error
    except SnapshotNotFoundError as error:
        raise HTTPException(404, _detail("SNAPSHOT_NOT_FOUND", str(error))) from error


@router.get("/{snapshot_id}/artifacts", response_model=ArtifactPageResponse)
def get_artifacts(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: SnapshotService = Depends(get_snapshot_service),
) -> ArtifactPageResponse:
    try:
        page = service.artifact_page(project_id, snapshot_id, limit, offset)
        return ArtifactPageResponse(
            items=[_artifact(item) for item in page.items],
            total=page.total,
            limit=limit,
            offset=offset,
        )
    except ProjectNotFoundError as error:
        raise HTTPException(404, _detail("PROJECT_NOT_FOUND", "Project was not found.")) from error
    except SnapshotNotFoundError as error:
        raise HTTPException(404, _detail("SNAPSHOT_NOT_FOUND", str(error))) from error


@router.get("/{snapshot_id}/changes", response_model=ArtifactChangePageResponse)
def get_changes(
    project_id: UUID,
    snapshot_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: SnapshotService = Depends(get_snapshot_service),
) -> ArtifactChangePageResponse:
    try:
        items, total = service.change_page(project_id, snapshot_id, limit, offset)
        return ArtifactChangePageResponse(
            items=[ArtifactChangeResponse.model_validate(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )
    except ProjectNotFoundError as error:
        raise HTTPException(404, _detail("PROJECT_NOT_FOUND", "Project was not found.")) from error
    except SnapshotNotFoundError as error:
        raise HTTPException(404, _detail("SNAPSHOT_NOT_FOUND", str(error))) from error
