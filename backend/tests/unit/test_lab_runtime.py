"""Stage 12 worker protocol, supervision and guard behaviour (framework-free self-test worker)."""

from __future__ import annotations

import ast
import os
import struct
import time
from pathlib import Path
from typing import Any

import pytest

from buildlens.domain.lab import LabError
from buildlens.infrastructure.lab.supervisor import (
    SupervisedFrameworkWorker,
    WorkerSpec,
    buildlens_python_path,
    module_argv,
)
from buildlens.infrastructure.lab.workers.common.wire import (
    MAX_FRAME_BYTES,
    DeadlineReader,
    WireError,
    decode,
    encode,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "buildlens"


def _worker(tmp_path: Path, **overrides: Any) -> SupervisedFrameworkWorker:
    spec = WorkerSpec(
        "selftest",
        module_argv("buildlens.infrastructure.lab.workers.common.selftest"),
        python_path=buildlens_python_path(),
        protected_roots=[str(tmp_path / "protected")],
        **overrides,
    )
    (tmp_path / "protected").mkdir(exist_ok=True)
    return SupervisedFrameworkWorker(spec, tmp_path / "worker")


# ------------------------------------------------------------------ wire


def test_wire_round_trip_and_rejections() -> None:
    frame = encode({"kind": "request", "id": 1, "op": "x", "params": {"a": [1, 2.5, "é"]}})
    (size,) = struct.unpack(">I", frame[:4])
    assert decode(frame[4 : 4 + size])["params"]["a"] == [1, 2.5, "é"]
    with pytest.raises(WireError, match="WIRE_KIND_INVALID"):
        encode({"kind": "bogus"})
    with pytest.raises(WireError, match="WIRE_UNSERIALIZABLE"):
        encode({"kind": "request", "x": float("nan")})
    with pytest.raises(WireError, match="WIRE_DUPLICATE_KEY"):
        decode(b'{"kind":"request","id":1,"id":2}')
    with pytest.raises(WireError, match="WIRE_NONFINITE_NUMBER"):
        decode(b'{"kind":"request","x":NaN}')
    with pytest.raises(WireError, match="WIRE_FRAME_TOO_LARGE"):
        encode({"kind": "request", "blob": "x" * MAX_FRAME_BYTES})


def test_deadline_reader_handles_partial_frames_and_deadlines() -> None:
    read_fd, write_fd = os.pipe()
    reader = DeadlineReader(read_fd)
    data = encode({"kind": "hello", "n": 1})
    os.write(write_fd, data[:3])
    assert reader.read(time.monotonic() + 5, poll=0.05) is None
    os.write(write_fd, data[3:])
    frame = reader.read(time.monotonic() + 5)
    assert frame is not None and frame["n"] == 1
    with pytest.raises(WireError, match="WIRE_DEADLINE"):
        reader.read(time.monotonic() + 0.1, poll=0.05) or reader.read(
            time.monotonic() + 0.05, poll=0.05
        )
    os.write(write_fd, struct.pack(">I", MAX_FRAME_BYTES + 1))
    with pytest.raises(WireError, match="WIRE_FRAME_TOO_LARGE"):
        reader.read(time.monotonic() + 5)
    os.close(write_fd)
    reader.close()
    os.close(read_fd)


# ------------------------------------------------------------------ supervision


def test_supervisor_rpc_callbacks_budget_and_allow_list(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    try:
        assert worker.request("echo", {"x": 1}, timeout_seconds=30).value == {"x": 1}
        assert worker.hello and worker.hello["guard"]["installed"]
        calls: list[str] = []

        def callbacks(method: str, params: dict[str, Any], deadline: float) -> Any:
            calls.append(method)
            return {"echo": params}

        result = worker.request(
            "callback",
            {"method": "embed_query", "params": {"t": 1}, "repeat": 2},
            timeout_seconds=10,
            callbacks=callbacks,
            allowed_methods=frozenset({"embed_query"}),
            max_calls=5,
        )
        assert result.value["results"] == [{"echo": {"t": 1}}, {"echo": {"t": 1}}]
        with pytest.raises(LabError) as caught:
            worker.request(
                "callback",
                {"method": "tool_call"},
                timeout_seconds=10,
                callbacks=callbacks,
                allowed_methods=frozenset({"embed_query"}),
                max_calls=5,
            )
        assert caught.value.code == "LAB_CALLBACK_NOT_ALLOWED"
        with pytest.raises(LabError) as caught:
            worker.request(
                "callback",
                {"method": "embed_query", "repeat": 3},
                timeout_seconds=10,
                callbacks=callbacks,
                allowed_methods=frozenset({"embed_query"}),
                max_calls=2,
            )
        assert caught.value.code == "LAB_CALLBACK_BUDGET_EXCEEDED"
        assert calls == ["embed_query", "embed_query", "embed_query", "embed_query"]

        def failing(method: str, params: dict[str, Any], deadline: float) -> Any:
            raise RuntimeError("secret detail must not cross the boundary")

        with pytest.raises(LabError) as caught:
            worker.request(
                "callback",
                {"method": "embed_query"},
                timeout_seconds=10,
                callbacks=failing,
                allowed_methods=frozenset({"embed_query"}),
                max_calls=1,
            )
        assert caught.value.code == "LAB_CALLBACK_FAILED"
        assert "secret" not in caught.value.message
    finally:
        worker.close()


def test_supervisor_failures_kill_and_replace(tmp_path: Path) -> None:
    worker = _worker(tmp_path, max_rss_bytes=400 * 1024 * 1024)
    try:
        worker.request("echo", {}, timeout_seconds=30)
        first_pid = worker.hello["pid"] if worker.hello else None
        outcomes = {}
        for op, params, timeout in (
            ("sleep", {"seconds": 5}, 1.0),
            ("big", {"bytes": 9 * 1024 * 1024}, 20),
            ("crash", {}, 20),
            ("exit", {}, 20),
            ("allocate", {"blocks": 10, "hold": 5}, 30),
        ):
            with pytest.raises(LabError) as caught:
                worker.request(op, params, timeout_seconds=timeout)
            outcomes[op] = caught.value.code
        assert outcomes == {
            "sleep": "LAB_WORKER_TIMEOUT",
            "big": "WORKER_RESPONSE_TOO_LARGE",
            "crash": "LAB_WORKER_CRASHED",
            "exit": "LAB_WORKER_CRASHED",
            "allocate": "LAB_WORKER_MEMORY_LIMIT",
        }
        assert worker.request("echo", {"ok": True}, timeout_seconds=30).value == {"ok": True}
        assert worker.hello and worker.hello["pid"] != first_pid
    finally:
        worker.close()
    assert not worker.alive()


def test_guard_and_environment_scrubbing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUILDLENS_DATABASE_URL", "postgresql+psycopg://x:y@127.0.0.1/db")
    monkeypatch.setenv("BUILDLENS_GEMINI_API_KEY", "not-a-real-key")
    secret = tmp_path / "protected" / "secret.txt"
    worker = _worker(tmp_path)
    secret.write_text("protected")
    try:
        result = worker.request(
            "probe_guard",
            {"read_path": str(secret), "write_path": str(tmp_path / "outside.txt")},
            timeout_seconds=30,
        ).value
    finally:
        worker.close()
    assert result["outcomes"] == {
        "read_protected": "denied",
        "write_outside": "denied",
        "connect_external": "denied",
        "resolve_external": "denied",
        "subprocess": "denied",
        "write_home": "allowed",
    }
    assert not [
        k
        for k in result["env_keys"]
        if "DATABASE" in k
        or "API_KEY" in k
        or k.startswith("BUILDLENS_")
        and k != "BUILDLENS_LAB_GUARD"
    ]
    assert not (tmp_path / "outside.txt").exists()


# ------------------------------------------------------------------ architecture boundaries

FRAMEWORKS = (
    "langchain",
    "langchain_core",
    "langchain_classic",
    "langchain_community",
    "langgraph",
    "llama_index",
    "langchain_postgres",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_frameworks_are_imported_only_inside_worker_packages() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith("infrastructure/lab/workers/"):
            continue
        if _imports(path) & set(FRAMEWORKS):
            offenders.append(relative)
    assert offenders == []


def test_domain_and_application_lab_modules_are_framework_and_orm_free() -> None:
    for relative in ("domain/lab.py", "application/lab.py"):
        imports = _imports(SRC / relative)
        assert not imports & {"sqlalchemy", "fastapi", *FRAMEWORKS}, relative


def test_framework_file_loaders_appear_only_in_sandbox_gated_modules() -> None:
    loaders = ("DirectoryLoader", "TextLoader", "SimpleDirectoryReader", "GenericLoader")
    for path in (SRC / "infrastructure" / "lab").rglob("*.py"):
        text = path.read_text()
        if any(name in text for name in loaders):
            assert path.name == "loaders.py", path
            assert "require_sandbox" in text, path


def test_isolated_environment_vendors_protocol_and_guard_byte_for_byte() -> None:
    vendored = (
        Path(__file__).resolve().parents[2]
        / "lab_envs"
        / "langchain_postgres"
        / "src"
        / "buildlens_lab_pg_worker"
    )
    common = SRC / "infrastructure" / "lab" / "workers" / "common"
    for name in ("wire.py", "guard.py"):
        assert (vendored / name).read_bytes() == (common / name).read_bytes(), name
    pyproject = (vendored.parents[1] / "pyproject.toml").read_text()
    assert "buildlens" not in pyproject.split("dependencies", 1)[1].split("]", 1)[0]
