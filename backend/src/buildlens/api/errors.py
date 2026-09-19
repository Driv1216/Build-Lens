"""Stable source-free HTTP error contracts."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from buildlens.application.embeddings import (
    EmbeddingBusyError,
    EmbeddingConflictError,
    EmbeddingInputError,
    EmbeddingLimitError,
    EmbeddingNotFoundError,
    EmbeddingPreparationRequiredError,
    EmbeddingUnavailableError,
)
from buildlens.application.processing import (
    CurrentContentUnavailableError,
    ProcessingBusyError,
    ProcessingConflictError,
    ProcessingNotFoundError,
    ProcessingUnavailableError,
)
from buildlens.application.projects import ProjectNotFoundError
from buildlens.application.snapshots import (
    ScanBusyError,
    SnapshotConflictError,
    SnapshotNotFoundError,
)
from buildlens.application.structural import (
    AnalysisBusyError,
    AnalysisConflictError,
    AnalysisContentUnavailableError,
    AnalysisNotFoundError,
    AnalysisUnavailableError,
)


def register_errors(app: FastAPI) -> None:
    mapping: dict[type[Exception], tuple[int, str, str]] = {
        AnalysisNotFoundError: (404, "ANALYSIS_NOT_FOUND", "Structural run was not found."),
        AnalysisContentUnavailableError: (
            409,
            "STRUCTURE_UNAVAILABLE",
            "Compatible structural evidence is unavailable.",
        ),
        ProjectNotFoundError: (404, "PROJECT_NOT_FOUND", "Project was not found."),
        SnapshotNotFoundError: (404, "SNAPSHOT_NOT_FOUND", "Repository snapshot was not found."),
        ProcessingNotFoundError: (
            404,
            "PROCESSING_NOT_FOUND",
            "Requested processing evidence was not found.",
        ),
        CurrentContentUnavailableError: (
            409,
            "CURRENT_CONTENT_UNAVAILABLE",
            "Compatible content is unavailable. Regenerate evidence explicitly.",
        ),
        EmbeddingPreparationRequiredError: (
            409,
            "EMBEDDING_PREPARATION_REQUIRED",
            "Prepare embeddings for the current snapshot before searching.",
        ),
        EmbeddingNotFoundError: (404, "EMBEDDING_RUN_NOT_FOUND", "Embedding run was not found."),
        EmbeddingInputError: (422, "SEMANTIC_QUERY_INVALID", "Semantic-search input is invalid."),
        EmbeddingLimitError: (
            422,
            "EMBEDDING_LIMIT_EXCEEDED",
            "Current content exceeds a configured embedding limit.",
        ),
        SQLAlchemyError: (
            503,
            "PERSISTENCE_UNAVAILABLE",
            "Database operation could not be completed.",
        ),
        ValueError: (422, "PREREQUISITE_INVALID", "Operation prerequisites are invalid."),
    }
    error: type[Exception]
    for error in (
        AnalysisBusyError,
        AnalysisConflictError,
        ProcessingBusyError,
        ProcessingConflictError,
        EmbeddingBusyError,
        EmbeddingConflictError,
        SnapshotConflictError,
        ScanBusyError,
    ):
        mapping[error] = (
            409,
            "OPERATION_CONFLICT",
            "Another operation changed or holds publication authority.",
        )
    for error in (ProcessingUnavailableError, AnalysisUnavailableError, EmbeddingUnavailableError):
        mapping[error] = (503, "OPERATION_UNAVAILABLE", "Operation infrastructure is unavailable.")

    async def handler(request: Request, error: Exception) -> JSONResponse:
        status, code, message = next(
            (value for cls, value in mapping.items() if isinstance(error, cls)),
            (500, "INTERNAL_ERROR", "Operation failed."),
        )
        return JSONResponse(
            status_code=status, content={"detail": {"code": code, "message": message}}
        )

    for error in mapping:
        app.add_exception_handler(error, handler)

    async def validation_handler(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "REQUEST_INVALID",
                    "message": "Request validation failed.",
                }
            },
        )

    app.add_exception_handler(RequestValidationError, validation_handler)
