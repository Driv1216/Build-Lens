from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from buildlens.application.projects import DuplicateProjectRootPathError
from buildlens.domain.entities import Project
from buildlens.infrastructure.persistence.models import ProjectRecord


def _to_domain(record: ProjectRecord) -> Project:
    return Project(
        id=record.id,
        name=record.name,
        root_path=record.root_path,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class SqlAlchemyProjectRepository:
    """PostgreSQL implementation of the application project repository port."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, project: Project) -> Project:
        record = ProjectRecord(
            id=project.id,
            name=project.name,
            root_path=project.root_path,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )
        self._session.add(record)
        try:
            self._session.commit()
        except IntegrityError as error:
            self._session.rollback()
            raise DuplicateProjectRootPathError(
                f"A project is already registered for {project.root_path!r}."
            ) from error
        self._session.refresh(record)
        return _to_domain(record)

    def list(self) -> list[Project]:
        records = self._session.scalars(
            select(ProjectRecord).order_by(ProjectRecord.created_at.desc())
        ).all()
        return [_to_domain(record) for record in records]

    def get(self, project_id: UUID) -> Project | None:
        record = self._session.get(ProjectRecord, project_id)
        return _to_domain(record) if record is not None else None
