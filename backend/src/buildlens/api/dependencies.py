from collections.abc import Generator
from typing import cast

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from buildlens.application.architecture import ArchitectureService
from buildlens.application.architecture_queries import ArchitectureQueries
from buildlens.application.ask import AskService
from buildlens.application.context import ContextService
from buildlens.application.embeddings import EmbeddingService
from buildlens.application.fingerprinting import SnapshotFingerprinter
from buildlens.application.processing import ProcessingService
from buildlens.application.projects import ProjectService
from buildlens.application.retrieval import RetrievalService
from buildlens.application.semantic_catalog import SemanticCatalogService
from buildlens.application.semantic_generation import SemanticGenerationService
from buildlens.application.snapshots import SnapshotService
from buildlens.application.structural import StructuralAnalysisService
from buildlens.core.config import Settings
from buildlens.infrastructure.persistence.database import get_session
from buildlens.infrastructure.persistence.embeddings import SqlAlchemyEmbeddingStore
from buildlens.infrastructure.persistence.repositories import SqlAlchemyProjectRepository
from buildlens.infrastructure.persistence.snapshots import SqlAlchemySnapshotStore
from buildlens.infrastructure.persistence.structural import SqlAlchemyStructuralAnalysisStore
from buildlens.infrastructure.runtime import (
    architecture_service,
    ask_service,
    context_service,
    processing_service,
    retrieval_service,
    semantic_catalog_service,
    semantic_generation_service,
)
from buildlens.infrastructure.structural.supervisor import (
    SupervisedRepositoryInspector,
    SupervisedSourceReader,
)
from buildlens.infrastructure.structural.worker import SupervisedPythonAnalyzer


def get_session_factory(request: Request) -> sessionmaker[Session]:
    return cast(sessionmaker[Session], request.app.state.session_factory)


def get_database_session(
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
) -> Generator[Session, None, None]:
    yield from get_session(session_factory)


def get_project_service(session: Session = Depends(get_database_session)) -> ProjectService:
    return ProjectService(SqlAlchemyProjectRepository(session))


def get_snapshot_service(request: Request) -> SnapshotService:
    settings: Settings = request.app.state.settings
    return SnapshotService(
        SqlAlchemySnapshotStore(request.app.state.session_factory),
        SupervisedRepositoryInspector(
            max_entries=settings.scan_max_entries,
            max_depth=settings.scan_max_depth,
            timeout_seconds=settings.scan_timeout_seconds,
            git_timeout_seconds=settings.scan_git_timeout_seconds,
        ),
        SnapshotFingerprinter(
            reader=SupervisedSourceReader(),
            max_file_bytes=settings.fingerprint_max_file_bytes,
            max_total_bytes=settings.fingerprint_max_total_bytes,
            file_timeout_seconds=settings.fingerprint_file_timeout_seconds,
            overall_timeout_seconds=settings.fingerprint_overall_timeout_seconds,
        ),
        request.app.state.scan_lock,
    )


def get_structural_analysis_service(request: Request) -> StructuralAnalysisService:
    settings: Settings = request.app.state.settings
    return StructuralAnalysisService(
        SqlAlchemyStructuralAnalysisStore(request.app.state.session_factory),
        SupervisedPythonAnalyzer(settings.structure_artifact_timeout_seconds),
        request.app.state.analysis_lock,
        max_source_bytes=settings.structure_max_source_bytes,
        max_total_bytes=settings.structure_max_total_bytes,
        max_artifacts=settings.structure_max_artifacts,
        artifact_timeout_seconds=settings.structure_artifact_timeout_seconds,
        overall_timeout_seconds=settings.structure_overall_timeout_seconds,
    )


def get_processing_service(request: Request) -> ProcessingService:
    return processing_service(request.app.state)


def get_architecture_service(request: Request) -> ArchitectureService:
    return architecture_service(request.app.state)


def get_architecture_queries(request: Request) -> ArchitectureQueries:
    return ArchitectureQueries(architecture_service(request.app.state))


def get_semantic_catalog_service(request: Request) -> SemanticCatalogService:
    return semantic_catalog_service(request.app.state)


def get_semantic_generation_service(request: Request) -> SemanticGenerationService:
    return semantic_generation_service(request.app.state)


def get_embedding_service(request: Request) -> EmbeddingService:
    settings: Settings = request.app.state.settings
    return EmbeddingService(
        get_processing_service(request),
        request.app.state.embedding_runtime,
        SqlAlchemyEmbeddingStore(request.app.state.session_factory),
        max_chunks=settings.embeddings_max_chunks,
        max_source_bytes=settings.embeddings_max_source_bytes,
        content_page_size=settings.embeddings_content_page_size,
        max_windows_per_chunk=settings.embeddings_max_windows_per_chunk,
        max_windows=settings.embeddings_max_windows,
        max_collection_bytes=settings.embeddings_max_collection_bytes,
        preparation_timeout_seconds=settings.embeddings_preparation_timeout_seconds,
        search_timeout_seconds=settings.embeddings_search_timeout_seconds,
    )


def get_retrieval_service(request: Request) -> RetrievalService:
    return retrieval_service(request.app.state)


def get_context_service(request: Request) -> ContextService:
    return context_service(request.app.state)


def get_ask_service(request: Request) -> AskService:
    return ask_service(request.app.state)
