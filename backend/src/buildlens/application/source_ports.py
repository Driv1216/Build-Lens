"""Typed source access ports shared by application services and adapters."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from buildlens.domain.processing import Chunk, ProcessingDiagnostic, ProcessorContract
from buildlens.domain.structural import Symbol


class SourceReadError(Exception):
    def __init__(
        self,
        code: str,
        message: str = "Artifact could not be read safely.",
        bytes_consumed: int = 0,
    ):
        self.code, self.message, self.bytes_consumed = code, message, bytes_consumed
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SourceRead:
    data: bytes
    size_bytes: int


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    algorithm: str
    schema_version: str
    digest: str
    size_bytes: int


class SecureSourceReader(Protocol):
    def read(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> SourceRead:
        raise NotImplementedError

    def fingerprint(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        timeout_seconds: float,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> SourceFingerprint:
        raise NotImplementedError

    def close(self) -> None:
        return None


class ProcessingWorker(SecureSourceReader, Protocol):
    timeout: float

    def process(
        self,
        source: str,
        suffix: str,
        processing_id: UUID,
        anchor: str,
        symbols: tuple[Symbol, ...],
        max_chunks: int,
        timeout: float,
    ) -> tuple[tuple[Chunk, ...], tuple[ProcessingDiagnostic, ...], ProcessorContract]: ...
