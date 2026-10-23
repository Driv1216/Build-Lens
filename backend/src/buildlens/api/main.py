from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from buildlens import __version__
from buildlens.api.ask_routes import router as ask_router
from buildlens.api.ask_routes import runtime_router as ask_runtime_router
from buildlens.api.context_routes import router as context_router
from buildlens.api.context_routes import runtime_router as context_runtime_router
from buildlens.api.processing_routes import router as processing_router
from buildlens.api.retrieval_routes import router as embedding_router
from buildlens.api.routes import router as projects_router
from buildlens.api.schemas import HealthResponse
from buildlens.api.snapshot_routes import router as snapshots_router
from buildlens.api.structural_routes import router as structural_router
from buildlens.core.config import Settings, get_settings
from buildlens.infrastructure.runtime import LocalRuntime


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the HTTP adapter without making startup depend on database availability."""
    active_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = LocalRuntime(active_settings)
        for name, value in vars(runtime).items():
            setattr(app.state, name, value)
        try:
            yield
        finally:
            await runtime.close()

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
                rejected = JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "ORIGIN_NOT_ALLOWED",
                            "message": "Mutation origin is not allowed.",
                        }
                    },
                )
                if request.url.path.rstrip("/").endswith("/ask"):
                    rejected.headers["Cache-Control"] = "no-store"
                return rejected
            if (
                request.url.path.rstrip("/").endswith(
                    (
                        "/snapshots",
                        "/structure",
                        "/processing",
                        "/embeddings",
                        "/semantic-search",
                        "/search",
                        "/context",
                        "/ask",
                    )
                )
            ) and request.headers.get("content-type", "").split(";", 1)[
                0
            ].strip().lower() != "application/json":
                rejected = JSONResponse(
                    status_code=415,
                    content={
                        "detail": {
                            "code": "JSON_REQUIRED",
                            "message": "Mutation requires JSON.",
                        }
                    },
                )
                if request.url.path.rstrip("/").endswith("/ask"):
                    rejected.headers["Cache-Control"] = "no-store"
                return rejected
        result = await call_next(request)
        if request.url.path.rstrip("/").endswith("/ask"):
            result.headers["Cache-Control"] = "no-store"
        return result

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
    app.include_router(context_router)
    app.include_router(context_runtime_router)
    app.include_router(ask_router)
    app.include_router(ask_runtime_router)
    return app


app = create_app()
