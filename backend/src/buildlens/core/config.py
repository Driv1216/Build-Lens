from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed process configuration loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_prefix="BUILDLENS_",
        extra="ignore",
    )

    database_url: str = Field(...)
    environment: str = "development"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    scan_max_entries: int = 100_000
    scan_max_depth: int = 64
    scan_timeout_seconds: int = 60
    scan_git_timeout_seconds: int = 10
    fingerprint_max_file_bytes: int = 64 * 1024 * 1024
    fingerprint_max_total_bytes: int = 1024 * 1024 * 1024
    fingerprint_file_timeout_seconds: float = 10
    fingerprint_overall_timeout_seconds: float = 120
    structure_max_source_bytes: int = 1_048_576
    structure_max_total_bytes: int = 67_108_864
    structure_max_artifacts: int = 5_000
    structure_artifact_timeout_seconds: float = 3
    structure_overall_timeout_seconds: float = 60
    processing_max_source_bytes: int = 1_048_576
    processing_max_total_bytes: int = 67_108_864
    processing_max_artifacts: int = 5_000
    processing_max_chunks_per_artifact: int = 1024
    processing_max_chunks: int = 20000
    processing_overall_timeout_seconds: float = 60
    embeddings_model_revision: str = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    embeddings_cache_dir: str = "~/.cache/buildlens/embeddings"
    embeddings_max_chunks: int = 20_000
    embeddings_max_source_bytes: int = 64 * 1024 * 1024
    embeddings_content_page_size: int = 128
    embeddings_window_batch_size: int = 16
    embeddings_max_windows_per_chunk: int = 32
    embeddings_max_windows: int = 100_000
    embeddings_max_collection_bytes: int = 64 * 1024 * 1024
    embeddings_worker_max_rss_bytes: int = 2 * 1024 * 1024 * 1024
    embeddings_model_load_timeout_seconds: float = 60
    embeddings_batch_timeout_seconds: float = 30
    embeddings_preparation_timeout_seconds: float = 300
    embeddings_search_timeout_seconds: float = 15
    reranker_model_revision: str = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
    reranker_cache_dir: str = "~/.cache/buildlens/reranking"
    reranker_batch_size: int = 8
    reranker_worker_max_rss_bytes: int = 2 * 1024 * 1024 * 1024
    reranker_model_load_timeout_seconds: float = 60
    reranker_inference_timeout_seconds: float = 45
    context_request_timeout_seconds: float = 120
    context_max_source_bytes: int = 2 * 1024 * 1024
    context_max_response_bytes: int = 4 * 1024 * 1024
    context_max_bytes: int = 32_000
    context_max_items: int = 16
    ask_enabled: bool = False
    generation_provider: Literal["gemini", "openai"] = "gemini"
    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    # Stage 12 framework laboratory. Lab state defaults to <repo>/.buildlens-eval/lab.
    lab_root: str | None = None
    lab_database_url: SecretStr | None = None
    lab_generation_enabled: bool = False
    lab_build_timeout_seconds: float = 900
    lab_query_timeout_seconds: float = 120
    lab_worker_max_rss_bytes: int = 3 * 1024 * 1024 * 1024
    lab_max_index_documents: int = 50_000
    lab_max_index_bytes: int = 256 * 1024 * 1024

    @model_validator(mode="after")
    def valid_limits(self) -> Self:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if ("max_" in name or "timeout_" in name) and value <= 0:
                raise ValueError(f"{name} must be positive")
        for prefix, file_field in (
            ("fingerprint", "max_file_bytes"),
            ("structure", "max_source_bytes"),
            ("processing", "max_source_bytes"),
        ):
            if getattr(self, f"{prefix}_{file_field}") > getattr(self, f"{prefix}_max_total_bytes"):
                raise ValueError(f"{prefix} file budget exceeds operation budget")
        if self.processing_max_chunks_per_artifact > self.processing_max_chunks:
            raise ValueError("Per-artifact chunk budget exceeds operation budget")
        if self.embeddings_max_windows_per_chunk > self.embeddings_max_windows:
            raise ValueError("Per-chunk embedding window budget exceeds operation budget")
        if self.embeddings_content_page_size <= 0 or self.embeddings_window_batch_size <= 0:
            raise ValueError("Embedding batch sizes must be positive")
        if len(self.embeddings_model_revision) != 40 or any(
            value not in "0123456789abcdef" for value in self.embeddings_model_revision
        ):
            raise ValueError("Embedding model revision must be a full lowercase commit SHA")
        if len(self.reranker_model_revision) != 40 or any(
            value not in "0123456789abcdef" for value in self.reranker_model_revision
        ):
            raise ValueError("Reranker revision must be a full lowercase commit SHA")
        if self.context_max_bytes > self.context_max_response_bytes:
            raise ValueError("Context payload budget exceeds response budget")
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("A PostgreSQL psycopg database URL is required")
        if self.lab_database_url is not None:
            from sqlalchemy.engine import make_url

            lab = make_url(self.lab_database_url.get_secret_value())
            production = make_url(self.database_url)
            if not lab.drivername.startswith("postgresql"):
                raise ValueError("The lab database URL must be PostgreSQL")
            same_server = (lab.host, lab.port) == (production.host, production.port)
            if same_server and (
                lab.database == production.database or lab.username == production.username
            ):
                raise ValueError(
                    "The lab database must be separate from production (database and role)"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
