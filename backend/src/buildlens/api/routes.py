from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from buildlens.api.dependencies import get_project_service
from buildlens.api.schemas import CreateProjectRequest, ProjectResponse
from buildlens.application.projects import (
    CreateProjectCommand,
    DuplicateProjectRootPathError,
    ProjectNotFoundError,
    ProjectService,
)
from buildlens.domain.entities import Project

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        root_path=project.root_path,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    request: CreateProjectRequest,
    service: ProjectService = Depends(get_project_service),
) -> ProjectResponse:
    try:
        project = service.create(
            CreateProjectCommand(name=request.name, root_path=request.root_path)
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except DuplicateProjectRootPathError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _response(project)


@router.get("", response_model=list[ProjectResponse])
def list_projects(service: ProjectService = Depends(get_project_service)) -> list[ProjectResponse]:
    return [_response(project) for project in service.list()]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: UUID,
    service: ProjectService = Depends(get_project_service),
) -> ProjectResponse:
    try:
        return _response(service.get(project_id))
    except ProjectNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
