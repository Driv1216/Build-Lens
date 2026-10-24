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


@pytest.mark.lab  # needs psutil (lab extras) for RSS supervision
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


def test_memory_supervision_fails_closed_without_rss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from buildlens.infrastructure.lab import supervisor

    monkeypatch.setattr(supervisor, "_rss", lambda pid: None)
    worker = _worker(tmp_path)
    with pytest.raises(LabError) as caught:
        worker.request("echo", {}, timeout_seconds=30)
    assert caught.value.code == "LAB_MEMORY_SUPERVISION_UNAVAILABLE"
    assert not worker.alive()


@pytest.mark.lab  # needs psutil (lab extras) for RSS supervision
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


@pytest.mark.lab  # needs psutil (lab extras) for RSS supervision
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


@pytest.mark.lab  # needs psutil (lab extras) for RSS supervision
def test_guard_checks_both_paths_of_renames_replaces_and_links(tmp_path: Path) -> None:
    """S12-SEC-01: destination of rename/replace and source of hard links are validated."""
    rw, protected, outside = tmp_path / "rw", tmp_path / "protected", tmp_path / "outside"
    for directory in (rw, protected, outside):
        directory.mkdir()
    secret = protected / "secret.txt"
    secret.write_text("protected-secret")
    (outside / "o.txt").write_text("outside")
    for name in "abcdefghijkm":
        (rw / name).write_text(name)
    actions: list[tuple[str, str, list[Path | str]]] = [
        ("rename_within", "rename", [rw / "a", rw / "a2"]),
        ("replace_within", "replace", [rw / "b", rw / "c"]),
        ("rename_into_protected", "rename", [rw / "d", protected / "d"]),
        ("replace_over_protected", "replace", [rw / "e", secret]),
        ("rename_out_of_root", "rename", [rw / "f", outside / "f"]),
        ("rename_from_protected", "rename", [secret, rw / "stolen"]),
        ("link_protected_source", "link", [secret, rw / "alias"]),
        ("read_protected_alias", "read", [rw / "alias"]),
        ("link_outside_source", "link", [outside / "o.txt", rw / "alias2"]),
        ("link_within", "link", [rw / "g", rw / "g2"]),
        ("link_into_protected", "link", [rw / "h", protected / "h"]),
        ("move_into_protected", "move", [rw / "i", protected / "i"]),
        ("copy_into_protected", "copyfile", [rw / "j", protected / "j"]),
        ("symlink_to_protected", "symlink", [secret, rw / "sym"]),
        ("read_through_symlink", "read", [rw / "sym"]),
        ("chdir_into_root", "chdir", [rw]),
        ("rename_dir_fd_within", "rename_dir_fd", [rw, "k", rw, "k2"]),
        ("rename_dir_fd_out_of_root", "rename_dir_fd", [rw, "m", outside, "m"]),
    ]
    worker = _worker(tmp_path, write_roots=[str(rw)])
    try:
        outcomes = worker.request(
            "probe_fs",
            {"actions": [[n, op, [str(a) for a in args]] for n, op, args in actions]},
            timeout_seconds=30,
        ).value["outcomes"]
    finally:
        worker.close()
    assert outcomes == {
        "rename_within": "allowed",
        "replace_within": "allowed",
        "rename_into_protected": "denied",
        "replace_over_protected": "denied",
        "rename_out_of_root": "denied",
        "rename_from_protected": "denied",
        "link_protected_source": "denied",
        "read_protected_alias": "oserror:2",  # the alias was never created
        "link_outside_source": "denied",
        "link_within": "allowed",
        "link_into_protected": "denied",
        "move_into_protected": "denied",
        "copy_into_protected": "denied",
        "symlink_to_protected": "allowed",  # creating the link is a write inside the root
        "read_through_symlink": "denied",  # following it resolves into the protected root
        "chdir_into_root": "denied",
        "rename_dir_fd_within": "allowed",
        "rename_dir_fd_out_of_root": "denied",
    }
    assert sorted(p.name for p in protected.iterdir()) == ["secret.txt"]
    assert secret.read_text() == "protected-secret"
    assert sorted(p.name for p in outside.iterdir()) == ["o.txt"]
    assert sorted(p.name for p in rw.iterdir()) == sorted(
        ["a2", "c", "d", "e", "f", "g", "g2", "h", "i", "j", "k2", "m", "sym"]
    )


@pytest.mark.lab  # needs psutil (lab extras) for RSS supervision
def test_failed_operations_kill_and_replace_the_worker(tmp_path: Path) -> None:
    """S12-SUP-02: an ok=false response never leaves a reusable process behind."""
    worker = _worker(tmp_path)
    try:
        first = worker.request("remember", {"value": "kept"}, timeout_seconds=30).value["pid"]
        with pytest.raises(LabError) as caught:
            worker.request("no_such_operation", {}, timeout_seconds=30)
        assert caught.value.code == "WORKER_OPERATION_UNKNOWN"
        assert not worker.alive() and worker.hello is None
        recalled = worker.request("recall", {}, timeout_seconds=30).value
        assert recalled["pid"] != first and recalled["value"] is None
        second = recalled["pid"]
        with pytest.raises(LabError) as caught:
            worker.request("poison", {}, timeout_seconds=30)  # mutates state, then fails
        assert caught.value.code == "WORKER_EXCEPTION"
        assert not worker.alive()
        recalled = worker.request("recall", {}, timeout_seconds=30).value
        assert recalled["pid"] not in {first, second} and recalled["value"] is None

        def oversized(method: str, params: dict[str, Any], deadline: float) -> Any:
            return "x" * MAX_FRAME_BYTES  # reply cannot be framed; worker is mid-request

        with pytest.raises(LabError) as caught:
            worker.request(
                "callback",
                {"method": "embed_query"},
                timeout_seconds=30,
                callbacks=oversized,
                allowed_methods=frozenset({"embed_query"}),
                max_calls=1,
            )
        assert caught.value.code == "LAB_TRANSFER_TOO_LARGE"
        assert not worker.alive()
        assert worker.request("echo", {"ok": 1}, timeout_seconds=30).value == {"ok": 1}
    finally:
        worker.close()


@pytest.mark.lab  # needs psutil (lab extras) for RSS supervision
def test_lab_database_authority_is_scoped_to_database_workers(tmp_path: Path) -> None:
    """S12-PRIV-04: with a lab DB configured, only database-scoped workers get its authority."""
    import dataclasses

    from buildlens.infrastructure.lab.service import LabWorkerPool
    from buildlens.infrastructure.lab.store import LabStore

    url = "postgresql+psycopg://lab_role:not-a-real-password@127.0.0.1:55412/buildlens_lab"
    pool = LabWorkerPool(
        LabStore(tmp_path / ".buildlens-eval" / "lab"),
        max_rss_bytes=3 * 1024**3,
        lab_db=("127.0.0.1", 55412, url),
    )
    for family in ("langchain", "llamaindex"):
        plain, scoped = pool.spec(family), pool.spec(family, database=True)
        assert plain.secret_env == {} and plain.allow_net == []
        assert scoped.secret_env == {"BUILDLENS_LAB_PG_URL": url}
        assert scoped.allow_net == [("127.0.0.1", 55412)]
        assert pool.key(family, False) != pool.key(family, True)
    with pytest.raises(LabError) as caught:
        pool.spec("selftest", database=True)
    assert caught.value.code == "LAB_DATABASE_SCOPE_INVALID"
    observed = {}
    for database in (False, True):
        # The exact environment and guard policy the pool builds, run in a real process.
        spec = dataclasses.replace(
            pool.spec("langchain", database=database),
            framework="selftest",
            argv=module_argv("buildlens.infrastructure.lab.workers.common.selftest"),
        )
        worker = SupervisedFrameworkWorker(spec, tmp_path / f"worker-{database}")
        try:
            probe = worker.request("probe_guard", {}, timeout_seconds=30).value
            observed[database] = (
                "BUILDLENS_LAB_PG_URL" in probe["env_keys"],
                worker.hello["guard"]["allow_net"] if worker.hello else None,
            )
        finally:
            worker.close()
    assert observed == {False: (False, []), True: (True, ["127.0.0.1:55412"])}


def test_memory_supervision_fails_closed_when_lost_mid_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S12-SUP-03: losing the RSS measurement of a live worker kills it."""
    from buildlens.infrastructure.lab import supervisor

    readings = iter([123_456_789])  # the startup check succeeds, every later read fails
    monkeypatch.setattr(supervisor, "_rss", lambda pid: next(readings, None))
    worker = _worker(tmp_path)
    try:
        with pytest.raises(LabError) as caught:
            worker.request("sleep", {"seconds": 3}, timeout_seconds=30)
        assert caught.value.code == "LAB_MEMORY_SUPERVISION_LOST"
        assert not worker.alive()
    finally:
        worker.close()


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
