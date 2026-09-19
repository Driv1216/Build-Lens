from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from buildlens import __version__
from buildlens.api.processing_routes import router as processing_router
from buildlens.api.retrieval_routes import router as embedding_router
from buildlens.api.routes import router as projects_router
from buildlens.api.schemas import HealthResponse
from buildlens.api.snapshot_routes import router as snapshots_router
from buildlens.api.structural_routes import router as structural_router
from buildlens.application.embeddings import EmbeddingRuntime
from buildlens.core.config import Settings, get_settings
from buildlens.infrastructure.embeddings.supervisor import SupervisedSentenceTransformer
from buildlens.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory_from_engine,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the HTTP adapter without making startup depend on database availability."""
    active_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(active_settings)
        app.state.settings = active_settings
        app.state.session_factory = create_session_factory_from_engine(engine)
        app.state.scan_lock = Lock()
        app.state.analysis_lock = Lock()
        app.state.processing_lock = Lock()
        app.state.embedding_runtime = EmbeddingRuntime(
            SupervisedSentenceTransformer(
                Path(active_settings.embeddings_cache_dir).expanduser(),
                revision=active_settings.embeddings_model_revision,
                model_load_timeout_seconds=(active_settings.embeddings_model_load_timeout_seconds),
                batch_timeout_seconds=active_settings.embeddings_batch_timeout_seconds,
                window_batch_size=active_settings.embeddings_window_batch_size,
                max_rss_bytes=active_settings.embeddings_worker_max_rss_bytes,
            )
        )
        try:
            yield
        finally:
            app.state.embedding_runtime.close()
            engine.dispose()

    app = FastAPI(title="BuildLens API", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "testserver"],
    )

    @app.middleware("http")
    async def local_mutation_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            allowed = {"http://localhost:5173", "http://127.0.0.1:5173"}
            if origin and origin not in allowed:
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "ORIGIN_NOT_ALLOWED",
                            "message": "Mutation origin is not allowed.",
                        }
                    },
                )
            if (
                request.url.path.rstrip("/").endswith(
                    (
                        "/snapshots",
                        "/structure",
                        "/processing",
                        "/embeddings",
                        "/semantic-search",
                        "/search",
                    )
                )
            ) and request.headers.get("content-type", "").split(";", 1)[
                0
            ].strip().lower() != "application/json":
                return JSONResponse(
                    status_code=415,
                    content={
                        "detail": {
                            "code": "JSON_REQUIRED",
                            "message": "Mutation requires JSON.",
                        }
                    },
                )
        return await call_next(request)

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["health"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok", environment=active_settings.environment)

    from buildlens.api.errors import register_errors

    register_errors(app)

    app.include_router(projects_router)
    app.include_router(snapshots_router)
    app.include_router(structural_router)
    app.include_router(processing_router)
    app.include_router(embedding_router)
    return app


app = create_app()
