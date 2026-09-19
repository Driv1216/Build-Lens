"""Structural adapter using the reusable, deadline-supervised source worker."""

from uuid import UUID

from buildlens.domain.structural import ArtifactAnalysis
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader


class SupervisedPythonAnalyzer:
    def __init__(self, timeout_seconds: float = 3.0) -> None:
        self._worker = SupervisedSourceReader(timeout_seconds)

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
    ) -> ArtifactAnalysis:
        return self._worker.analyze(
            analysis_id,
            version_id,
            artifact_id,
            root,
            locator,
            max_bytes,
            expected_root_identity,
            timeout,
        )

    def close(self) -> None:
        self._worker.close()
