"""One reusable trusted worker per explicit operation, with hard parent deadlines."""

from __future__ import annotations

import hashlib
import multiprocessing as mp
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from multiprocessing.connection import Connection
from queue import Empty, Queue
from threading import Thread
from typing import Any, cast
from uuid import UUID, uuid4

from buildlens.application.processors import process_source
from buildlens.domain.extractors import PYTHON_LITERALS, TS_LITERALS, extractor_contract
from buildlens.domain.inspection import InspectionError, InspectionResult
from buildlens.domain.processing import Chunk, ProcessingDiagnostic, ProcessorContract
from buildlens.domain.source import ContentRejectedError, digest
from buildlens.domain.structural import (
    ArtifactAnalysis,
    ArtifactExtraction,
    ParseDiagnostic,
    Symbol,
)
from buildlens.infrastructure.structural.parser import PythonAstParser
from buildlens.infrastructure.structural.reader import (
    PosixSecureSourceReader,
    SecureSourceReader,
    SourceFingerprint,
    SourceRead,
    SourceReadError,
)


def _peak_rss_bytes() -> int:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS and KiB on Linux.
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _extract(
    reader: PosixSecureSourceReader,
    args: tuple[Any, ...],
    canary: list[str | None],
) -> object:
    """Stage 13 language dispatch: frozen Python declarations plus additive passes."""
    (
        analysis_id,
        version_id,
        artifact_id,
        root,
        locator,
        max_bytes,
        expected,
        language,
        reused_symbols,
    ) = args
    started = datetime.now(UTC)
    raw = reader.read(root, locator, max_bytes, expected)
    fingerprint = hashlib.sha256(raw.data).hexdigest()
    if language == "Python":
        from buildlens.infrastructure.structural.python_facts import extract_python_facts

        if reused_symbols is not None:
            return extract_python_facts(raw.data, reused_symbols), fingerprint, raw.size_bytes
        parsed = PythonAstParser().parse(
            analysis_id=analysis_id,
            version_id=version_id,
            artifact_id=artifact_id,
            locator=locator,
            data=raw.data,
        )
        extractions = (
            (
                extract_python_facts(raw.data, parsed.symbols),
                _confined(PYTHON_LITERALS, lambda: _python_literals(raw.data)),
            )
            if parsed.outcome == "parsed"
            else ()
        )
        return replace(
            parsed,
            read_started_at=started,
            read_completed_at=datetime.now(UTC),
            input_fingerprint=fingerprint,
            bytes_consumed=raw.size_bytes,
            language="Python",
            extractions=extractions,
        )
    from buildlens.infrastructure.structural import typescript

    if not canary:
        canary.append(typescript.verify_canary())
    if canary[0] is not None:
        extraction = typescript.failure(locator, raw.data, canary[0])
    else:
        extraction = typescript.extract(locator, raw.data)
    return ArtifactAnalysis(
        uuid4(),
        analysis_id,
        version_id,
        artifact_id,
        locator,
        extraction.outcome,
        started,
        datetime.now(UTC),
        extraction.encoding,
        extraction.line_count,
        False,
        extraction.diagnostics,
        extraction.symbols,
        extraction.imports,
        input_fingerprint=fingerprint,
        bytes_consumed=raw.size_bytes,
        language=language,
        declaration_extraction_id=extraction.extraction.id
        if extraction.outcome == "parsed"
        else None,
        extractions=(extraction.extraction,)
        + (
            (_confined(TS_LITERALS, lambda: _ts_literals(locator, raw.data)),)
            if extraction.outcome == "parsed" and canary[0] is None
            else ()
        ),
    )


def _python_literals(data: bytes) -> ArtifactExtraction:
    from buildlens.infrastructure.structural.architecture_literals_python import (
        extract_python_literals,
    )

    return extract_python_literals(data)


def _ts_literals(locator: str, data: bytes) -> ArtifactExtraction:
    from buildlens.infrastructure.structural.architecture_literals_typescript import (
        extract_ts_literals,
    )

    return extract_ts_literals(locator, data)


def _confined(extractor: str, run: Callable[[], ArtifactExtraction]) -> ArtifactExtraction:
    """Stage 14 literal passes never fail the artifact or the run: any error fails the pass."""
    try:
        return run()
    except Exception:
        contract = extractor_contract(extractor) or {}
        return ArtifactExtraction(
            uuid4(),
            extractor,
            contract,
            digest(contract) if contract else "0" * 64,
            "failed",
            None,
            (
                ParseDiagnostic(
                    "EXTRACTOR_FAILED", "Additive extraction did not complete.", "error"
                ),
            ),
        )


def _additional(
    reader: PosixSecureSourceReader,
    args: tuple[Any, ...],
    canary: list[str | None],
) -> tuple[tuple[ArtifactExtraction, ...], str, int]:
    """Stage 14 reuse path: one verified read for every missing literal pass of an artifact."""
    root, locator, max_bytes, expected, language, extractors = args
    raw = reader.read(root, locator, max_bytes, expected)
    fingerprint = hashlib.sha256(raw.data).hexdigest()
    values: list[ArtifactExtraction] = []
    for name in extractors:
        if name == PYTHON_LITERALS and language == "Python":
            values.append(_confined(name, lambda: _python_literals(raw.data)))
        elif name == TS_LITERALS and language in {"TypeScript", "JavaScript"}:
            from buildlens.infrastructure.structural import typescript

            if not canary:
                canary.append(typescript.verify_canary())
            code = canary[0]
            if code is not None:
                values.append(_confined(name, partial(_raise, code)))
            else:
                values.append(_confined(name, lambda: _ts_literals(locator, raw.data)))
        else:
            raise ValueError("Unknown additive extractor")
    return tuple(values), fingerprint, raw.size_bytes


def _raise(code: str) -> ArtifactExtraction:
    raise RuntimeError(code)


def _worker(connection: Connection, progress: Any, max_rss_bytes: int = 0) -> None:
    def observe(count: int) -> None:
        progress.value = count

    reader = PosixSecureSourceReader(observe)
    canary: list[str | None] = []
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
                elif operation == "extract":
                    result = _extract(reader, args, canary)
                    if max_rss_bytes and _peak_rss_bytes() > max_rss_bytes:
                        # Peak RSS never decreases; the parent recycles this worker.
                        connection.send((False, ("RESOURCE_LIMIT", int(progress.value))))
                        continue
                elif operation == "additional":
                    result = _additional(reader, args, canary)
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
    def __init__(self, timeout: float = 3, max_rss_bytes: int = 0) -> None:
        self.timeout = timeout
        self.max_rss_bytes = max_rss_bytes
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
                    target=_worker, args=(child, self._progress, self.max_rss_bytes), daemon=True
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
                if result[0] == "RESOURCE_LIMIT":
                    self.close()
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

    def extract(
        self,
        analysis_id: UUID,
        version_id: UUID,
        artifact_id: UUID,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None,
        timeout: float,
        language: str,
    ) -> ArtifactAnalysis:
        return cast(
            ArtifactAnalysis,
            self._run(
                "extract",
                (
                    analysis_id,
                    version_id,
                    artifact_id,
                    root,
                    locator,
                    max_bytes,
                    expected_root_identity,
                    language,
                    None,
                ),
                timeout,
            ),
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
        zero = UUID(int=0)
        return cast(
            tuple[ArtifactExtraction, str, int],
            self._run(
                "extract",
                (
                    zero,
                    zero,
                    zero,
                    root,
                    locator,
                    max_bytes,
                    expected_root_identity,
                    "Python",
                    symbols,
                ),
                timeout,
            ),
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
        return cast(
            tuple[tuple[ArtifactExtraction, ...], str, int],
            self._run(
                "additional",
                (root, locator, max_bytes, expected_root_identity, language, extractors),
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
