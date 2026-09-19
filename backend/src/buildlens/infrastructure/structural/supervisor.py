"""One reusable trusted worker per explicit operation, with hard parent deadlines."""

from __future__ import annotations

import hashlib
import multiprocessing as mp
from dataclasses import replace
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from queue import Empty, Queue
from threading import Thread
from typing import Any, cast
from uuid import UUID

from buildlens.application.processors import process_source
from buildlens.domain.inspection import InspectionError, InspectionResult
from buildlens.domain.processing import Chunk, ProcessingDiagnostic, ProcessorContract
from buildlens.domain.source import ContentRejectedError
from buildlens.domain.structural import ArtifactAnalysis, Symbol
from buildlens.infrastructure.structural.parser import PythonAstParser
from buildlens.infrastructure.structural.reader import (
    PosixSecureSourceReader,
    SecureSourceReader,
    SourceFingerprint,
    SourceRead,
    SourceReadError,
)


def _worker(connection: Connection, progress: Any) -> None:
    def observe(count: int) -> None:
        progress.value = count

    reader = PosixSecureSourceReader(observe)
    try:
        while True:
            operation, args = connection.recv()
            try:
                result: object
                if operation == "read":
                    result = reader.read(*args)
                elif operation == "fingerprint":
                    result = reader.fingerprint(*args)
                elif operation == "inspect":
                    from buildlens.infrastructure.discovery.filesystem import (
                        LocalRepositoryInspector,
                    )

                    root, previous, maximum, depth, timeout, git_timeout = args
                    result = LocalRepositoryInspector(
                        max_entries=maximum,
                        max_depth=depth,
                        timeout_seconds=timeout,
                        git_timeout_seconds=git_timeout,
                    ).inspect(root, previous)
                elif operation == "analyze":
                    analysis_id, version_id, artifact_id, root, locator, max_bytes, expected = args
                    started = datetime.now(UTC)
                    raw = reader.read(root, locator, max_bytes, expected)
                    parsed = PythonAstParser().parse(
                        analysis_id=analysis_id,
                        version_id=version_id,
                        artifact_id=artifact_id,
                        locator=locator,
                        data=raw.data,
                    )
                    result = replace(
                        parsed,
                        read_started_at=started,
                        read_completed_at=datetime.now(UTC),
                        input_fingerprint=hashlib.sha256(raw.data).hexdigest(),
                        bytes_consumed=raw.size_bytes,
                    )
                elif operation == "process":
                    result = process_source(*args)
                else:
                    raise ValueError("Unknown trusted operation")
                connection.send((True, result))
            except InspectionError as error:
                connection.send((False, (error.code, 0)))
            except SourceReadError as error:
                connection.send((False, (error.code, int(progress.value))))
            except ContentRejectedError as error:
                connection.send((False, (error.code, int(progress.value))))
            except Exception:
                connection.send((False, ("WORKER_FAILED", int(progress.value))))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class SupervisedSourceReader(SecureSourceReader):
    def __init__(self, timeout: float = 3) -> None:
        self.timeout = timeout
        self._process: Any = None
        self._connection: Connection | None = None
        self._progress: Any = None

    def _run(self, operation: str, args: tuple[object, ...], timeout: float) -> Any:
        try:
            if self._connection is None:
                context = mp.get_context("spawn")
                parent, child = context.Pipe()
                # A terminated worker must never leave a progress mutex locked in the parent.
                self._progress = context.RawValue("Q", 0)
                self._process = context.Process(
                    target=_worker, args=(child, self._progress), daemon=True
                )
                self._process.start()
                child.close()
                self._connection = parent
            self._progress.value = 0
            replies: Queue[tuple[bool, object]] = Queue(maxsize=1)
            connection = self._connection

            def exchange() -> None:
                try:
                    connection.send((operation, args))
                    replies.put((True, connection.recv()))
                except (OSError, EOFError):
                    replies.put((False, None))

            exchange_thread = Thread(target=exchange, daemon=True)
            exchange_thread.start()
            try:
                received, response = replies.get(timeout=max(0.001, timeout))
            except Empty:
                consumed = int(self._progress.value)
                self.close()
                exchange_thread.join(0.2)
                raise SourceReadError("ARTIFACT_TIMEOUT", bytes_consumed=consumed) from None
            if not received:
                raise EOFError
            ok, result = cast(tuple[bool, Any], response)
            if not ok:
                raise SourceReadError(result[0], bytes_consumed=result[1])
            return result
        except (OSError, EOFError, RuntimeError):
            consumed = int(self._progress.value) if self._progress else 0
            self.close()
            raise SourceReadError("WORKER_UNAVAILABLE", bytes_consumed=consumed) from None

    def read(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> SourceRead:
        return cast(
            SourceRead,
            self._run("read", (root, locator, max_bytes, expected_root_identity), self.timeout),
        )

    def fingerprint(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        timeout_seconds: float,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> SourceFingerprint:
        return cast(
            SourceFingerprint,
            self._run(
                "fingerprint",
                (root, locator, max_bytes, timeout_seconds, expected_root_identity),
                timeout_seconds,
            ),
        )

    def process(
        self,
        source: str,
        suffix: str,
        processing_id: UUID,
        anchor: str,
        symbols: tuple[Symbol, ...],
        max_chunks: int,
        timeout: float,
    ) -> tuple[tuple[Chunk, ...], tuple[ProcessingDiagnostic, ...], ProcessorContract]:
        return cast(
            tuple[tuple[Chunk, ...], tuple[ProcessingDiagnostic, ...], ProcessorContract],
            self._run(
                "process",
                (source, suffix, processing_id, anchor, symbols, max_chunks, timeout),
                timeout,
            ),
        )

    def analyze(
        self,
        analysis_id: UUID,
        version_id: UUID,
        artifact_id: UUID,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None,
        timeout: float,
    ) -> ArtifactAnalysis:
        return cast(
            ArtifactAnalysis,
            self._run(
                "analyze",
                (
                    analysis_id,
                    version_id,
                    artifact_id,
                    root,
                    locator,
                    max_bytes,
                    expected_root_identity,
                ),
                timeout,
            ),
        )

    def close(self) -> None:
        if self._connection:
            self._connection.close()
            self._connection = None
        if self._process:
            self._process.join(0.05)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(0.2)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(0.2)
            self._process.close()
            self._process = None


class SupervisedRepositoryInspector:
    """Bound inventory and ignore-control filesystem operations from the parent."""

    def __init__(
        self,
        max_entries: int = 100000,
        max_depth: int = 64,
        timeout_seconds: int = 60,
        git_timeout_seconds: int = 10,
    ) -> None:
        self._limits = (max_entries, max_depth, timeout_seconds, git_timeout_seconds)

    def inspect(
        self, root_path: str, previous_canonical_root: str | None = None
    ) -> InspectionResult:
        worker = SupervisedSourceReader()
        try:
            return cast(
                InspectionResult,
                worker._run(
                    "inspect", (root_path, previous_canonical_root, *self._limits), self._limits[2]
                ),
            )
        except SourceReadError as error:
            raise InspectionError(
                error.code,
                "Repository inspection could not complete safely.",
                409 if error.code == "ROOT_TARGET_CHANGED" else 422,
            ) from None
        finally:
            worker.close()
