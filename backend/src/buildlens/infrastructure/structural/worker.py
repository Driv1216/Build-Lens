"""Structural adapter using the reusable, deadline-supervised source worker."""

from uuid import UUID

from buildlens.domain.structural import ArtifactAnalysis, ArtifactExtraction, Symbol
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader

DEFAULT_MAX_RSS_BYTES = 1_073_741_824


class SupervisedArtifactAnalyzer:
    """Frozen Python declarations plus Stage 13 additive passes, in one supervised worker."""

    def __init__(
        self, timeout_seconds: float = 3.0, max_rss_bytes: int = DEFAULT_MAX_RSS_BYTES
    ) -> None:
        self._worker = SupervisedSourceReader(timeout_seconds, max_rss_bytes)

    def analyze(
        self,
        analysis_id: UUID,
        version_id: UUID,
        artifact_id: UUID,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None = None,
        timeout: float = 3,
        language: str = "Python",
    ) -> ArtifactAnalysis:
        return self._worker.extract(
            analysis_id,
            version_id,
            artifact_id,
            root,
            locator,
            max_bytes,
            expected_root_identity,
            timeout,
            language,
        )

    def extract_facts(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None,
        timeout: float,
        symbols: tuple[Symbol, ...],
    ) -> tuple[ArtifactExtraction, str, int]:
        return self._worker.extract_facts(
            root, locator, max_bytes, expected_root_identity, timeout, symbols
        )

    def extract_additional(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None,
        timeout: float,
        language: str,
        extractors: tuple[str, ...],
    ) -> tuple[tuple[ArtifactExtraction, ...], str, int]:
        return self._worker.extract_additional(
            root, locator, max_bytes, expected_root_identity, timeout, language, extractors
        )

    def close(self) -> None:
        self._worker.close()


# Retained name for existing callers; it now dispatches every registered language.
SupervisedPythonAnalyzer = SupervisedArtifactAnalyzer
