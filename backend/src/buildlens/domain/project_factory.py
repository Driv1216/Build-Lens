from datetime import UTC, datetime
from uuid import uuid4

from buildlens.domain.entities import Project


def new_project(*, name: str, root_path: str) -> Project:
    """Build a new project entity without tying domain creation to a database."""
    now = datetime.now(UTC)
    return Project(id=uuid4(), name=name, root_path=root_path, created_at=now, updated_at=now)
