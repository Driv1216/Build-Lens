"""Stable source-free HTTP error contracts."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from buildlens.application.ask import (
    AskBudgetExceededError,
    AskInputError,
    AskTimeoutError,
    GenerationCitationInvalidError,
    GenerationConfigurationInvalidError,
    GenerationContextRejectedError,
    GenerationNotConfiguredError,
    GenerationOutputInvalidError,
    GenerationRateLimitedError,
    GenerationRefusedError,
    GenerationUnavailableError,
    TokenCountTimeoutError,
    TokenCountUnavailableError,
)
from buildlens.application.context import (
    ContextBusyError,
    ContextConflictError,
    ContextInputError,
    ContextLimitError,
    ContextUnavailableError,
)
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
from buildlens.application.semantic_catalog import (
    SemanticCatalogBusyError,
    SemanticCatalogConflictError,
    SemanticCatalogRequiredError,
    SemanticCatalogStaleError,
    SemanticNotFoundError,
    SemanticPrerequisiteError,
)
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

LAB_STATUS = {
    "LAB_CORPUS_CHANGED": 409,
    "LAB_EMBEDDING_BUSY": 409,
    "LAB_RERANKER_BUSY": 409,
    "LAB_GENERATION_NOT_AUTHORIZED": 403,
    "LAB_SANDBOX_REQUIRED": 403,
    "LAB_INDEX_NOT_FOUND": 404,
    "LAB_RUN_NOT_FOUND": 404,
    "LAB_EXPERIMENT_NOT_FOUND": 404,
    "LAB_ISOLATED_ENV_MISSING": 503,
    "LAB_DATABASE_NOT_CONFIGURED": 503,
    "LAB_WORKER_TIMEOUT": 504,
}


def _lab_status(code: str) -> int:
    if code in LAB_STATUS:
        return LAB_STATUS[code]
    if code.startswith(("CONFIG_", "LAB_QUESTION", "LAB_TRANSFER", "LAB_BUDGET", "LAB_RUN_FILE")):
        return 422
    if code.startswith(("LAB_WORKER", "WORKER_", "LAB_FRAMEWORK", "LAB_CALLBACK")):
        return 503
    if code.startswith(
        ("VECTOR_REUSE", "LAB_VECTOR_REUSE", "LAB_STORED_VECTOR", "LAB_EMBEDDING_CONTRACT")
    ):
        return 409
    return 422


def register_errors(app: FastAPI) -> None:
    mapping: dict[type[Exception], tuple[int, str, str]] = {
        GenerationNotConfiguredError: (
            503,
            "GENERATION_NOT_CONFIGURED",
            "Ask generation is not configured.",
        ),
        GenerationConfigurationInvalidError: (
            503,
            "GENERATION_CONFIGURATION_INVALID",
            "Generation configuration was rejected.",
        ),
        GenerationUnavailableError: (
            503,
            "GENERATION_UNAVAILABLE",
            "Generation provider is unavailable.",
        ),
        GenerationRateLimitedError: (
            429,
            "GENERATION_RATE_LIMITED",
            "Generation provider rate limit was reached.",
        ),
        AskTimeoutError: (504, "ASK_TIMEOUT", "Ask did not finish within its deadline."),
        TokenCountUnavailableError: (
            503,
            "TOKEN_COUNT_UNAVAILABLE",
            "Provider token counting is unavailable.",
        ),
        TokenCountTimeoutError: (504, "TOKEN_COUNT_TIMEOUT", "Provider token counting timed out."),
        AskBudgetExceededError: (
            422,
            "ASK_BUDGET_EXCEEDED",
            "Question or evidence exceeds the Ask token allowance.",
        ),
        GenerationContextRejectedError: (
            502,
            "GENERATION_CONTEXT_REJECTED",
            "Provider rejected the counted context.",
        ),
        GenerationOutputInvalidError: (
            502,
            "GENERATION_OUTPUT_INVALID",
            "Generation output did not satisfy the answer contract.",
        ),
        GenerationCitationInvalidError: (
            502,
            "GENERATION_CITATION_INVALID",
            "Generation cited evidence outside the supplied package.",
        ),
        GenerationRefusedError: (
            422,
            "GENERATION_REFUSED",
            "Generation provider refused the request.",
        ),
        AskInputError: (422, "ASK_INPUT_INVALID", "Ask question is invalid."),
        AnalysisNotFoundError: (404, "ANALYSIS_NOT_FOUND", "Structural run was not found."),
        AnalysisContentUnavailableError: (
            409,
            "STRUCTURE_UNAVAILABLE",
            "Compatible structural evidence is unavailable.",
        ),
        ProjectNotFoundError: (404, "PROJECT_NOT_FOUND", "Project was not found."),
        SemanticCatalogRequiredError: (
            409,
            "SEMANTIC_CATALOG_REQUIRED",
            "Build the semantic catalog for this snapshot first.",
        ),
        SemanticCatalogStaleError: (
            409,
            "SEMANTIC_CATALOG_STALE",
            "The semantic catalog is stale. Rebuild it explicitly.",
        ),
        SemanticNotFoundError: (
            404,
            "SEMANTIC_NOT_FOUND",
            "Semantic catalog record was not found.",
        ),
        SemanticPrerequisiteError: (
            409,
            "STRUCTURE_UNAVAILABLE",
            "Compatible structural evidence is unavailable.",
        ),
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
        ContextInputError: (422, "CONTEXT_INPUT_INVALID", "Context input is invalid."),
        ContextLimitError: (
            422,
            "CONTEXT_LIMIT_EXCEEDED",
            "Context construction exceeded a configured limit.",
        ),
        ContextUnavailableError: (
            503,
            "CONTEXT_UNAVAILABLE",
            "Context construction is unavailable. Inspect GET /api/v1/reranker; acquire the "
            "pinned model with 'buildlens reranking download-model' when learned ranking is used.",
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
        ContextBusyError,
        ContextConflictError,
        SemanticCatalogBusyError,
        SemanticCatalogConflictError,
    ):
        mapping[error] = (
            409,
            "OPERATION_CONFLICT",
            "Another operation changed or holds publication authority.",
        )
    for error in (
        ProcessingUnavailableError,
        AnalysisUnavailableError,
        EmbeddingUnavailableError,
    ):
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

    from buildlens.domain.lab import LabError

    async def lab_handler(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, LabError)
        message = error.message if error.message != error.code else "Laboratory operation failed."
        return JSONResponse(
            status_code=_lab_status(error.code),
            content={"detail": {"code": error.code, "message": message[:500]}},
            headers={"Cache-Control": "no-store"},
        )

    app.add_exception_handler(LabError, lab_handler)
