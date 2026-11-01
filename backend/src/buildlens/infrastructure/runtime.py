"""Shared local RAG construction and ownership, independent of FastAPI."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any

from buildlens.application.architecture import ArchitectureGraphCache, ArchitectureService
from buildlens.application.architecture_evaluation import ArchitectureEvaluationService
from buildlens.application.architecture_queries import ArchitectureQueries
from buildlens.application.architecture_rules import ArchitectureRulesService
from buildlens.application.ask import AskService
from buildlens.application.context import ContextService, RerankerRuntime
from buildlens.application.embeddings import EmbeddingRuntime
from buildlens.application.flow import FlowService
from buildlens.application.flow_evidence import FlowEvidenceService
from buildlens.application.flow_trace_service import FlowTraceService, TraceGraphCache
from buildlens.application.outbound_gate import StructuredGenerator
from buildlens.application.processing import ProcessingService
from buildlens.application.retrieval import RetrievalService
from buildlens.application.semantic_catalog import SemanticCatalogService
from buildlens.application.semantic_generation import (
    GenerationSettings,
    SemanticGenerationService,
)
from buildlens.core.config import Settings
from buildlens.domain.ask import GenerationPolicy
from buildlens.infrastructure.embeddings.supervisor import SupervisedSentenceTransformer
from buildlens.infrastructure.generation.gemini import GeminiProvider
from buildlens.infrastructure.generation.openai import OpenAIProvider
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from buildlens.infrastructure.persistence.architecture_rules import SqlAlchemyArchitectureRuleStore
from buildlens.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory_from_engine,
)
from buildlens.infrastructure.persistence.embeddings import SqlAlchemyEmbeddingStore
from buildlens.infrastructure.persistence.flow import SqlAlchemyFlowStore
from buildlens.infrastructure.persistence.flow_evidence import SqlAlchemyFlowEvidenceStore
from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore
from buildlens.infrastructure.persistence.retrieval import (
    SqlAlchemyContextEvidenceStore,
    SqlAlchemyRetrievalStore,
)
from buildlens.infrastructure.persistence.semantic import SqlAlchemySemanticCatalogStore
from buildlens.infrastructure.persistence.semantic_generation import SqlAlchemyGenerationStore
from buildlens.infrastructure.persistence.structural import SqlAlchemyStructuralAnalysisStore
from buildlens.infrastructure.reranking.supervisor import SupervisedCrossEncoder
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
from buildlens.infrastructure.structural.worker import DEFAULT_MAX_RSS_BYTES


def processing_service(state: Any) -> ProcessingService:
    s = state.settings
    return ProcessingService(
        SqlAlchemyProcessingStore(state.session_factory),
        state.processing_lock,
        reader=SupervisedSourceReader(),
        max_source_bytes=s.processing_max_source_bytes,
        max_total_bytes=s.processing_max_total_bytes,
        max_artifacts=s.processing_max_artifacts,
        max_chunks_per_artifact=s.processing_max_chunks_per_artifact,
        max_chunks=s.processing_max_chunks,
        overall_timeout_seconds=s.processing_overall_timeout_seconds,
    )


def semantic_catalog_service(state: Any) -> SemanticCatalogService:
    """DB-only catalog construction: processing is used for authority only (zero source reads)."""
    return SemanticCatalogService(
        SqlAlchemySemanticCatalogStore(state.session_factory),
        SqlAlchemyStructuralAnalysisStore(state.session_factory),
        processing_service(state),
        state.semantic_lock,
    )


def architecture_service(state: Any) -> ArchitectureService:
    """DB-only architecture model over the active semantic catalog (zero source reads)."""
    return ArchitectureService(
        SqlAlchemyArchitectureStore(state.session_factory),
        semantic_catalog_service(state),
        state.architecture_lock,
        state.architecture_cache,
    )


def flow_evidence_service(state: Any) -> FlowEvidenceService:
    """Stage 15: the only Stage 15 operation that reads source (supervised, fingerprint-checked)."""
    s = state.settings

    def reader() -> SupervisedSourceReader:
        return SupervisedSourceReader(s.structure_artifact_timeout_seconds, DEFAULT_MAX_RSS_BYTES)

    return FlowEvidenceService(
        SqlAlchemyFlowEvidenceStore(state.session_factory),
        reader,
        state.flow_evidence_lock,
        max_source_bytes=s.structure_max_source_bytes,
        artifact_timeout_seconds=s.structure_artifact_timeout_seconds,
    )


def flow_trace_service(state: Any) -> FlowTraceService:
    """Stage 15 traces over the verified flow model (per-run graph cache, DB only)."""
    return FlowTraceService(
        flow_service(state),
        SqlAlchemyFlowStore(state.session_factory),
        retrieval_service(state),
        state.flow_trace_cache,
    )


def flow_service(state: Any) -> FlowService:
    """Stage 15 flow model: a DB-only projection (zero source reads)."""
    return FlowService(
        SqlAlchemyFlowStore(state.session_factory),
        flow_evidence_service(state),
        semantic_catalog_service(state),
        architecture_service(state),
        state.flow_lock,
    )


def architecture_evaluation_service(state: Any) -> ArchitectureEvaluationService:
    """Read-only exports of a verified run for the pre-registered evaluation (Milestone D)."""
    return ArchitectureEvaluationService(
        architecture_service(state), SqlAlchemyArchitectureStore(state.session_factory)
    )


def architecture_rules_service(state: Any) -> ArchitectureRulesService:
    """Owner-declared rule sets (append-only) evaluated over verified architecture runs."""
    return ArchitectureRulesService(
        SqlAlchemyArchitectureRuleStore(state.session_factory),
        ArchitectureQueries(architecture_service(state)),
    )


def semantic_generator(settings: Settings) -> StructuredGenerator:
    """Construct (never contact) the configured semantic provider. There is no fallback."""
    tokens = settings.semantic_max_output_tokens
    if settings.semantic_provider == "gemini":
        from buildlens.infrastructure.generation.semantic_gemini import (
            DEFAULT_MODEL as GEMINI_MODEL,
        )
        from buildlens.infrastructure.generation.semantic_gemini import GeminiSemanticGenerator

        key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None
        return GeminiSemanticGenerator(key, settings.semantic_model or GEMINI_MODEL, tokens)
    if settings.semantic_provider == "openai":
        from buildlens.infrastructure.generation.semantic_openai import (
            DEFAULT_MODEL as OPENAI_MODEL,
        )
        from buildlens.infrastructure.generation.semantic_openai import OpenAISemanticGenerator

        key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
        return OpenAISemanticGenerator(key, settings.semantic_model or OPENAI_MODEL, tokens)
    from buildlens.infrastructure.generation.semantic_local import LoopbackGenerator

    return LoopbackGenerator(settings.semantic_base_url, settings.semantic_model or "local", tokens)


def generation_settings(settings: Settings) -> GenerationSettings:
    return GenerationSettings(
        enabled=settings.semantic_generation_enabled,
        max_output_tokens=settings.semantic_max_output_tokens,
        request_byte_ceiling=settings.semantic_request_byte_ceiling,
        max_outbound_requests=settings.semantic_max_outbound_requests,
        max_attempts_per_entity=settings.semantic_max_attempts_per_entity,
        max_consecutive_provider_failures=settings.semantic_max_consecutive_provider_failures,
        deadline_seconds=settings.semantic_deadline_seconds,
        plan_ttl_seconds=settings.semantic_plan_ttl_seconds,
        request_timeout_seconds=settings.semantic_request_timeout_seconds,
        retain_rejected_output=settings.semantic_retain_rejected_output,
    )


def semantic_generation_service(state: Any) -> SemanticGenerationService:
    settings = state.settings
    return SemanticGenerationService(
        semantic_catalog_service(state),
        SqlAlchemySemanticCatalogStore(state.session_factory),
        SqlAlchemyGenerationStore(state.session_factory),
        lambda: semantic_generator(settings),
        generation_settings(settings),
    )


def retrieval_service(state: Any) -> RetrievalService:
    return RetrievalService(
        processing_service(state),
        state.embedding_runtime,
        SqlAlchemyEmbeddingStore(state.session_factory),
        SqlAlchemyRetrievalStore(state.session_factory),
        search_timeout_seconds=state.settings.embeddings_search_timeout_seconds,
    )


def context_service(state: Any) -> ContextService:
    s = state.settings
    return ContextService(
        retrieval_service(state),
        state.reranker_runtime,
        SqlAlchemyContextEvidenceStore(state.session_factory),
        request_timeout_seconds=s.context_request_timeout_seconds,
        rerank_timeout_seconds=s.reranker_inference_timeout_seconds,
        byte_limit=s.context_max_bytes,
        item_limit=s.context_max_items,
        source_byte_limit=s.context_max_source_bytes,
        response_byte_limit=s.context_max_response_bytes,
    )


def ask_service(state: Any) -> AskService:
    return AskService(
        context_service(state),
        state.generation_provider,
        enabled=state.settings.ask_enabled,
        policy=GenerationPolicy.for_provider(state.settings.generation_provider),
    )


class LocalRuntime:
    """One worker/lock set per CLI invocation or HTTP lifespan; no eager model loading."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_database_engine(settings)
        self.session_factory = create_session_factory_from_engine(self.engine)
        self.scan_lock = Lock()
        self.analysis_lock = Lock()
        self.processing_lock = Lock()
        self.semantic_lock = Lock()
        self.architecture_lock = Lock()
        self.architecture_cache = ArchitectureGraphCache()
        self.flow_evidence_lock = Lock()
        self.flow_lock = Lock()
        self.flow_trace_cache = TraceGraphCache()
        self.embedding_runtime = EmbeddingRuntime(
            SupervisedSentenceTransformer(
                Path(settings.embeddings_cache_dir).expanduser(),
                revision=settings.embeddings_model_revision,
                model_load_timeout_seconds=settings.embeddings_model_load_timeout_seconds,
                batch_timeout_seconds=settings.embeddings_batch_timeout_seconds,
                window_batch_size=settings.embeddings_window_batch_size,
                max_rss_bytes=settings.embeddings_worker_max_rss_bytes,
            )
        )
        self.reranker_runtime = RerankerRuntime(
            SupervisedCrossEncoder(
                Path(settings.reranker_cache_dir).expanduser(),
                revision=settings.reranker_model_revision,
                model_load_timeout_seconds=settings.reranker_model_load_timeout_seconds,
                batch_timeout_seconds=settings.reranker_inference_timeout_seconds,
                batch_size=settings.reranker_batch_size,
                max_rss_bytes=settings.reranker_worker_max_rss_bytes,
            )
        )
        # Stage 12: the lab store and framework workers are created lazily on first lab use.
        self.lab_workers: Any = None
        self.lab_service_instance: Any = None
        self.generation_provider = (
            GeminiProvider(
                settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None
            )
            if settings.generation_provider == "gemini"
            else OpenAIProvider(
                settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
            )
        )

    async def close(self) -> None:
        # Release every owner even when one cleanup fails.
        try:
            if self.lab_workers is not None:
                self.lab_workers.close()
        except Exception:
            pass
        try:
            self.embedding_runtime.close()
        finally:
            try:
                self.reranker_runtime.close()
            finally:
                try:
                    await self.generation_provider.close()
                finally:
                    self.engine.dispose()
