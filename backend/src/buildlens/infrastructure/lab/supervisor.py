"""Parent-side supervision of framework worker processes.

Each framework environment runs in its own spawned process (own session/process group).
The parent enforces: a scrubbed environment allow-list, a handshake with protocol check,
bounded frames, an absolute deadline per request, a per-request callback allow-list and
call budget, sampled RSS limits, and kill-and-replace on any failure. A failed worker is
never reused; the next request starts a fresh process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from buildlens.domain.lab import LabError
from buildlens.infrastructure.lab.workers.common.wire import (
    PROTOCOL,
    DeadlineReader,
    WireError,
    encode,
)

CallbackHandler = Callable[[str, dict[str, Any], float], Any]


@dataclass(slots=True)
class WorkerSpec:
    framework: str
    argv: list[str]  # interpreter + module invocation
    python_path: list[str] = field(default_factory=list)
    write_roots: list[str] = field(default_factory=list)
    read_roots: list[str] = field(default_factory=list)
    protected_roots: list[str] = field(default_factory=list)
    allow_net: list[tuple[str, int]] = field(default_factory=list)
    secret_env: dict[str, str] = field(default_factory=dict)  # only for authorized runs
    max_rss_bytes: int = 3 * 1024 * 1024 * 1024
    startup_timeout_seconds: float = 60.0


@dataclass(slots=True)
class WorkerCall:
    value: Any
    steps: list[dict[str, Any]]
    bridge_calls: dict[str, int]
    guard_denials: list[dict[str, str]]
    elapsed_ms: float
    bridge_ms: float


def _rss(pid: int) -> int | None:
    try:
        import psutil

        process = psutil.Process(pid)
        total = process.memory_info().rss
        for child in process.children(recursive=True):
            total += child.memory_info().rss
        return int(total)
    except Exception:
        return None


class SupervisedFrameworkWorker:
    def __init__(self, spec: WorkerSpec, home: Path) -> None:
        self.spec = spec
        self.home = home
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: DeadlineReader | None = None
        self._stderr: deque[bytes] = deque(maxlen=256)
        self._lock = threading.Lock()
        self.hello: dict[str, Any] | None = None
        self.spawn_ms: float | None = None
        self.peak_rss: int | None = None
        self._request_id = 0

    # ------------------------------------------------------------------ lifecycle

    def _environment(self) -> dict[str, str]:
        home = self.home / "home"
        tmp = self.home / "tmp"
        for directory in (home, tmp):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        guard = {
            "write_roots": [str(home), str(tmp), *self.spec.write_roots],
            "read_roots": [str(home), str(tmp), *self.spec.read_roots, *self.spec.write_roots],
            "protected_roots": self.spec.protected_roots,
            "allow_net": [[h, p] for h, p in self.spec.allow_net],
            "trace": os.environ.get("BUILDLENS_LAB_GUARD_TRACE") == "1",
        }
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "ANONYMIZED_TELEMETRY": "False",
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
            "LANGSMITH_TRACING_V2": "false",
            "TOKENIZERS_PARALLELISM": "false",
            "BUILDLENS_LAB_GUARD": json.dumps(guard),
        }
        if self.spec.python_path:
            env["PYTHONPATH"] = os.pathsep.join(self.spec.python_path)
        env.update(self.spec.secret_env)
        return env

    def _start(self, deadline: float) -> None:
        started = time.perf_counter()
        self._process = subprocess.Popen(
            self.spec.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment(),
            cwd=str(self.home),
            start_new_session=True,
            close_fds=True,
        )
        assert self._process.stdout and self._process.stderr
        self._reader = DeadlineReader(self._process.stdout.fileno())
        threading.Thread(target=self._drain, args=(self._process.stderr,), daemon=True).start()
        try:
            hello = self._read(min(deadline, time.monotonic() + self.spec.startup_timeout_seconds))
        except LabError:
            self.kill()
            raise
        if hello.get("kind") != "hello" or hello.get("protocol") != PROTOCOL:
            self.kill()
            raise LabError("LAB_WORKER_PROTOCOL_MISMATCH")
        if hello.get("framework") != self.spec.framework:
            self.kill()
            raise LabError("LAB_WORKER_PROTOCOL_MISMATCH")
        if not (hello.get("guard") or {}).get("installed"):
            self.kill()
            raise LabError("LAB_WORKER_GUARD_MISSING")
        self.hello = hello
        self.spawn_ms = (time.perf_counter() - started) * 1000

    def _drain(self, stream: Any) -> None:
        try:
            for line in iter(stream.readline, b""):
                self._stderr.append(line[:2000])
        except (OSError, ValueError):
            return

    def stderr_tail(self) -> str:
        return b"".join(self._stderr).decode("utf-8", "replace")[-16000:]

    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def kill(self) -> None:
        process = self._process
        self._process = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, PermissionError):
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self.alive():
                try:
                    self._send({"kind": "request", "id": 0, "op": "shutdown", "params": {}})
                    self._read(time.monotonic() + 3)
                except LabError:
                    pass
            self.kill()

    # ------------------------------------------------------------------ transport

    def _send(self, message: dict[str, Any]) -> None:
        assert self._process and self._process.stdin
        try:
            data = encode(message)
        except WireError as error:
            code = "LAB_TRANSFER_TOO_LARGE" if "LARGE" in error.code else error.code
            raise LabError(code) from error
        try:
            self._process.stdin.write(data)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            self.kill()
            raise LabError("LAB_WORKER_CRASHED") from error

    def _read(self, deadline: float) -> dict[str, Any]:
        while True:
            if self._reader is None:
                raise LabError("LAB_WORKER_CRASHED")
            try:
                frame = self._reader.read(deadline)
            except WireError as error:
                code = {
                    "WIRE_DEADLINE": "LAB_WORKER_TIMEOUT",
                    "WIRE_EOF": "LAB_WORKER_CRASHED",
                    "WIRE_FRAME_TOO_LARGE": "LAB_TRANSFER_TOO_LARGE",
                }.get(error.code, "LAB_WORKER_PROTOCOL_ERROR")
                self.kill()
                raise LabError(code) from error
            if frame is not None:
                return frame
            if self._process is not None:
                rss = _rss(self._process.pid)
                if rss is not None:
                    self.peak_rss = max(self.peak_rss or 0, rss)
                    if rss > self.spec.max_rss_bytes:
                        self.kill()
                        raise LabError("LAB_WORKER_MEMORY_LIMIT")
                if self._process.poll() is not None:
                    self.kill()
                    raise LabError("LAB_WORKER_CRASHED")

    # ------------------------------------------------------------------ RPC

    def request(
        self,
        op: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
        callbacks: CallbackHandler | None = None,
        allowed_methods: frozenset[str] = frozenset(),
        max_calls: int = 0,
    ) -> WorkerCall:
        with self._lock:
            deadline = time.monotonic() + timeout_seconds
            if not self.alive():
                self.kill()
                self._start(deadline)
            self._request_id += 1
            request_id = self._request_id
            started = time.perf_counter()
            bridge_ms = 0.0
            calls = 0
            self._send({"kind": "request", "id": request_id, "op": op, "params": params})
            while True:
                frame = self._read(deadline)
                kind = frame.get("kind")
                if kind == "call":
                    calls += 1
                    method = str(frame.get("method"))
                    call_id = frame.get("id")
                    if method not in allowed_methods or callbacks is None:
                        reply = _reply_error(call_id, "LAB_CALLBACK_NOT_ALLOWED", method)
                    elif calls > max_calls:
                        reply = _reply_error(call_id, "LAB_CALLBACK_BUDGET_EXCEEDED", method)
                    else:
                        call_started = time.perf_counter()
                        try:
                            result = callbacks(method, dict(frame.get("params") or {}), deadline)
                            reply = {"kind": "reply", "id": call_id, "ok": True, "result": result}
                        except LabError as error:
                            reply = _reply_error(call_id, error.code, error.message)
                        except Exception as error:  # never leak details into the worker
                            reply = _reply_error(
                                call_id, "LAB_CALLBACK_FAILED", type(error).__name__
                            )
                        bridge_ms += (time.perf_counter() - call_started) * 1000
                    self._send(reply)
                    continue
                if kind != "response" or frame.get("id") != request_id:
                    self.kill()
                    raise LabError("LAB_WORKER_PROTOCOL_ERROR")
                result = frame.get("result") or {}
                if not frame.get("ok"):
                    failure = frame.get("error") or {}
                    raise LabWorkerError(
                        str(failure.get("code", "WORKER_FAILED")),
                        str(failure.get("message", "")),
                        result.get("steps") or [],
                        result.get("guard_denials") or [],
                    )
                return WorkerCall(
                    value=result.get("value"),
                    steps=list(result.get("steps") or []),
                    bridge_calls=dict(result.get("bridge_calls") or {}),
                    guard_denials=list(result.get("guard_denials") or []),
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    bridge_ms=bridge_ms,
                )


class LabWorkerError(LabError):
    def __init__(
        self,
        code: str,
        message: str,
        steps: list[dict[str, Any]],
        guard_denials: list[dict[str, str]],
    ) -> None:
        super().__init__(code, message)
        self.steps = steps
        self.guard_denials = guard_denials


def _reply_error(call_id: Any, code: str, message: str) -> dict[str, Any]:
    return {
        "kind": "reply",
        "id": call_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def buildlens_python_path() -> list[str]:
    """The directory containing the importable `buildlens` package for the worker."""
    import buildlens

    return [str(Path(buildlens.__file__).resolve().parents[1])]


def default_worker_home(root: Path, framework: str) -> Path:
    path = root / "workers" / framework
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def module_argv(module: str, python: str | None = None) -> list[str]:
    return [python or sys.executable, "-s", "-m", module]


def scratch_home(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))
