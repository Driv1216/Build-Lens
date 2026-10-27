"""Worker-side serve loop, parent callbacks, tracing and the embedding ledger.

Imports only the stdlib and the pure `buildlens.domain.lab` module, so it can run before
any framework import. The guard must be installed before this module serves requests.
"""

from __future__ import annotations

import os
import struct
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from buildlens.domain.lab import LabError, sha256_hex
from buildlens.infrastructure.lab.workers.common import guard
from buildlens.infrastructure.lab.workers.common.wire import (
    PROTOCOL,
    WireError,
    read_frame,
    write_frame,
)

Handler = Callable[["WorkerSession", dict[str, Any]], Any]
MAX_TRACE_STEPS = 2000


class Tracer:
    """Collects normalized LabStep dictionaries for one request."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self._seq = 0
        self._stack: list[int] = []

    def add(
        self,
        kind: str,
        name: str,
        event: str,
        duration_ms: float = 0.0,
        attributes: dict[str, Any] | None = None,
        item_refs: list[str] | None = None,
    ) -> int:
        self._seq += 1
        if len(self.steps) < MAX_TRACE_STEPS:
            self.steps.append(
                {
                    "seq": self._seq,
                    "kind": kind,
                    "name": name,
                    "framework_event": event,
                    "duration_ms": round(duration_ms, 3),
                    "attributes": attributes or {},
                    "item_refs": (item_refs or [])[:200],
                    "parent": self._stack[-1] if self._stack else None,
                }
            )
        return self._seq

    @contextmanager
    def step(self, kind: str, name: str, event: str) -> Iterator[dict[str, Any]]:
        attributes: dict[str, Any] = {}
        seq = self.add(kind, name, event, 0.0, attributes)
        self._stack.append(seq)
        started = time.perf_counter()
        try:
            yield attributes
        finally:
            self._stack.pop()
            for entry in self.steps:
                if entry["seq"] == seq:
                    entry["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
                    entry["item_refs"] = list(attributes.pop("_refs", []))[:200]
                    break


class EmbeddingLedger:
    """Records every text a framework asked to embed and what happened to it.

    `reuse` maps sha256(exact embed text) -> vector supplied by the parent from the stored
    embedding run (verified there). In strict mode a miss is an error, so a framework that
    silently changes the embedded text (metadata injection, rewriting) cannot fall back to
    fresh inference unnoticed.
    """

    def __init__(self, session: WorkerSession, source: str, reuse: dict[str, list[float]]):
        self.session = session
        self.source = source
        self.reuse = reuse
        self.reused = 0
        self.computed = 0
        self.misses: list[str] = []
        self.transformations: dict[str, int] = {}
        self.records: list[dict[str, Any]] = []

    def note(self, ref: str, raw_text: str, embed_text: str, transformation: str) -> None:
        self.transformations[transformation] = self.transformations.get(transformation, 0) + 1
        if len(self.records) < 50_000:
            self.records.append(
                {
                    "ref": ref,
                    "raw_sha256": sha256_hex(raw_text),
                    "embed_sha256": sha256_hex(embed_text),
                    "transformation": transformation,
                }
            )

    def documents(self, texts: list[str]) -> list[list[float]]:
        if self.source == "deterministic-fake" or self.source == "compute":
            return self._compute(texts)
        vectors: list[list[float] | None] = []
        missing: list[int] = []
        for index, text in enumerate(texts):
            vector = self.reuse.get(sha256_hex(text))
            vectors.append(vector)
            if vector is None:
                missing.append(index)
        if missing and self.source == "reuse-stored":
            self.misses.extend(sha256_hex(texts[i]) for i in missing[:20])
            raise LabError(
                "VECTOR_REUSE_MISS",
                f"{len(missing)} embed text(s) have no verified stored vector; the framework "
                "embedded text that differs from the stored chunk content",
            )
        self.reused += len(texts) - len(missing)
        if missing:
            computed = self._compute([texts[i] for i in missing])
            for index, vector in zip(missing, computed, strict=True):
                vectors[index] = vector
        return [v for v in vectors if v is not None]

    def _compute(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for start in range(0, len(texts), 64):
            batch = texts[start : start + 64]
            result.extend(self.session.call("embed_documents", {"texts": batch})["vectors"])
        self.computed += len(texts)
        return result

    def query(self, text: str) -> list[float]:
        return list(self.session.call("embed_query", {"text": text})["vector"])

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "reused": self.reused,
            "computed_via_bridge": self.computed,
            "reuse_misses": len(self.misses),
            "transformations": dict(sorted(self.transformations.items())),
        }


class WorkerSession:
    def __init__(self, inp: Any, out: Any) -> None:
        self._inp = inp
        self._out = out
        self._call_id = 0
        self.state: dict[str, Any] = {}
        self.tracer = Tracer()
        self.call_counts: dict[str, int] = {}

    def call(self, method: str, params: dict[str, Any]) -> Any:
        self._call_id += 1
        call_id = self._call_id
        self.call_counts[method] = self.call_counts.get(method, 0) + 1
        started = time.perf_counter()
        write_frame(self._out, {"kind": "call", "id": call_id, "method": method, "params": params})
        frame = read_frame(self._inp)
        if frame.get("kind") != "reply" or frame.get("id") != call_id:
            raise WireError("WIRE_SEQUENCE_INVALID")
        elapsed = (time.perf_counter() - started) * 1000
        self.tracer.add("bridge", method, "bridge.call", elapsed, {"ok": bool(frame.get("ok"))})
        if not frame.get("ok"):
            error = frame.get("error") or {}
            raise LabError(str(error.get("code", "BRIDGE_FAILED")), str(error.get("message", "")))
        return frame.get("result")


def _redirect_stdio() -> tuple[Any, Any]:
    """Keep fds 0/1 for the protocol; route stray prints (frameworks) to stderr."""
    inp = os.fdopen(os.dup(0), "rb", buffering=0)
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return inp, out


def serve(
    framework: str, versions: Callable[[], dict[str, str]], handlers: dict[str, Handler]
) -> None:
    inp, out = _redirect_stdio()
    session = WorkerSession(inp, out)
    write_frame(
        out,
        {
            "kind": "hello",
            "protocol": PROTOCOL,
            "framework": framework,
            "versions": versions(),
            "pid": os.getpid(),
            "guard": guard.policy(),
        },
    )
    while True:
        try:
            frame = read_frame(inp)
        except WireError as error:
            if error.code == "WIRE_EOF":
                return
            raise
        if frame.get("kind") != "request":
            raise WireError("WIRE_SEQUENCE_INVALID")
        request_id = frame.get("id")
        op = str(frame.get("op"))
        session.tracer = Tracer()
        session.call_counts = {}
        if op == "shutdown":
            write_frame(out, {"kind": "response", "id": request_id, "ok": True, "result": {}})
            return
        handler = handlers.get(op)
        try:
            if handler is None:
                raise LabError("WORKER_OPERATION_UNKNOWN", op)
            result = handler(session, dict(frame.get("params") or {}))
            response: dict[str, Any] = {"kind": "response", "id": request_id, "ok": True}
            response["result"] = {
                "value": result,
                "steps": session.tracer.steps,
                "bridge_calls": session.call_counts,
                "guard_denials": guard.denials(),
            }
        except LabError as error:
            response = _error(request_id, error.code, error.message, session)
        except guard.GuardViolation as error:
            response = _error(request_id, "LAB_GUARD_DENIED", str(error), session)
        except WireError:
            raise
        except Exception as error:  # framework failure: type name only, traceback to stderr
            traceback.print_exc(file=sys.stderr)
            response = _error(request_id, "WORKER_EXCEPTION", type(error).__name__, session)
        try:
            write_frame(out, response)
        except WireError as error:
            if error.code != "WIRE_FRAME_TOO_LARGE":
                raise
            write_frame(out, _error(request_id, "WORKER_RESPONSE_TOO_LARGE", "", session))


def _error(request_id: Any, code: str, message: str, session: WorkerSession) -> dict[str, Any]:
    return {
        "kind": "response",
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message[:500]},
        "result": {"steps": session.tracer.steps[-50:], "guard_denials": guard.denials()},
    }


def float32_digest(vector: list[float]) -> str:
    return sha256_hex(struct.pack(f"<{len(vector)}f", *vector))
