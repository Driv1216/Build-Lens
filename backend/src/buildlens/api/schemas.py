from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    environment: str


class CreateProjectRequest(BaseModel):
    name: str = Field(max_length=200)
    root_path: str


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    root_path: str
    created_at: datetime
    updated_at: datetime
