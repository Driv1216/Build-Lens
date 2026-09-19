from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
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
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("A PostgreSQL psycopg database URL is required")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
