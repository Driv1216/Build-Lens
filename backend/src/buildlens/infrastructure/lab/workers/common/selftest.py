"""Framework-free worker used to verify the supervisor, wire protocol and guard."""

from __future__ import annotations

import os
import socket
import sys
import time
from typing import Any

from buildlens.infrastructure.lab.workers.common import guard

guard.install_from_environment()

from buildlens.infrastructure.lab.workers.common.runtime import WorkerSession, serve  # noqa: E402


def _echo(session: WorkerSession, params: dict[str, Any]) -> Any:
    return params


def _callback(session: WorkerSession, params: dict[str, Any]) -> Any:
    results = [session.call(str(params["method"]), dict(params.get("params") or {}))]
    for _ in range(int(params.get("repeat", 1)) - 1):
        results.append(session.call(str(params["method"]), dict(params.get("params") or {})))
    return {"results": results}


def _sleep(session: WorkerSession, params: dict[str, Any]) -> Any:
    time.sleep(float(params["seconds"]))
    return {}


def _big(session: WorkerSession, params: dict[str, Any]) -> Any:
    return {"blob": "x" * int(params["bytes"])}


def _crash(session: WorkerSession, params: dict[str, Any]) -> Any:
    os._exit(3)


def _allocate(session: WorkerSession, params: dict[str, Any]) -> Any:
    blocks = [bytearray(64 * 1024 * 1024) for _ in range(int(params["blocks"]))]
    for block in blocks:
        block[::4096] = b"\x01" * len(block[::4096])
    time.sleep(float(params.get("hold", 2.0)))
    return {"allocated": len(blocks)}


def _garbage(session: WorkerSession, params: dict[str, Any]) -> Any:
    raise SystemExit(0)  # exits mid-request without a response frame


def _probe_guard(session: WorkerSession, params: dict[str, Any]) -> Any:
    outcomes: dict[str, str] = {}

    def attempt(name: str, action: Any) -> None:
        try:
            action()
            outcomes[name] = "allowed"
        except PermissionError:
            outcomes[name] = "denied"
        except OSError as error:
            outcomes[name] = f"oserror:{error.errno}"

    target = params.get("read_path")
    if target:
        attempt("read_protected", lambda: open(target, "rb").close())
    write_path = params.get("write_path")
    if write_path:
        attempt("write_outside", lambda: open(write_path, "wb").close())
    attempt(
        "connect_external",
        lambda: socket.create_connection(("93.184.216.34", 443), timeout=1).close(),
    )
    attempt("resolve_external", lambda: socket.getaddrinfo("example.com", 443))
    attempt("subprocess", lambda: __import__("subprocess").run(["/bin/echo", "x"]))
    attempt("write_home", lambda: open(os.path.join(os.environ["HOME"], "ok.txt"), "w").close())
    return {"outcomes": outcomes, "env_keys": sorted(os.environ)}


if __name__ == "__main__":
    serve(
        "selftest",
        lambda: {"python": sys.version.split()[0]},
        {
            "echo": _echo,
            "callback": _callback,
            "sleep": _sleep,
            "big": _big,
            "crash": _crash,
            "allocate": _allocate,
            "probe_guard": _probe_guard,
            "exit": _garbage,
        },
    )
