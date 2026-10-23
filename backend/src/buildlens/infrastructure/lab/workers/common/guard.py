"""Process-wide audit-hook guard installed first thing in every framework worker.

This is defense in depth, NOT a security sandbox:

* Primary isolation is the separate process: no database credentials, no provider keys
  (unless an authorized run injects exactly one), a scrubbed environment, a private HOME
  and TMPDIR, and parent-enforced deadlines and memory limits.
* PEP 578 audit hooks only observe Python-level operations. C extensions that open files
  or sockets directly (libpq, gRPC core, native parsers) and ctypes calls are not seen.
  Audit hooks cannot be removed but can be bypassed by code that is already malicious.

Policy (from BUILDLENS_LAB_GUARD, a JSON object set by the parent):

* network: deny every socket connect/bind/getaddrinfo except listed (host, port) pairs;
* subprocesses/exec: always denied;
* writes, deletes, renames, chmod, mkdir: only beneath write_roots;
* reads beneath protected_roots (e.g. the user's home and registered repositories) are
  denied unless they are also beneath read_roots (venv, package source, index, sandbox).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_FS_MUTATIONS = {
    "os.remove": 0,
    "os.rename": 0,
    "os.rmdir": 0,
    "os.mkdir": 0,
    "os.chmod": 0,
    "os.chown": 0,
    "os.symlink": 1,
    "os.link": 1,
    "os.truncate": 0,
    "os.utime": 0,
    "shutil.rmtree": 0,
    "shutil.copyfile": 1,
    "shutil.move": 1,
}
_EXEC_EVENTS = {
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.fork",
    "os.forkpty",
    "pty.spawn",
}

_state: dict[str, Any] = {
    "installed": False,
    "write_roots": (),
    "read_roots": (),
    "protected_roots": (),
    "allow_net": set(),
    "allow_hosts": set(),
    "denials": [],
}


class GuardViolation(PermissionError):
    pass


def _norm(path: Any) -> str | None:
    if isinstance(path, int):
        return None  # already-open descriptor
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, (str, os.PathLike)):
        return None
    try:
        return os.path.realpath(os.fspath(path))
    except (OSError, ValueError):
        return os.path.abspath(os.fspath(path))


def _beneath(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


def _deny(event: str, detail: str) -> None:
    denials = _state["denials"]
    if len(denials) < 200:
        entry = {"event": event, "detail": detail[:200]}
        if _state.get("trace"):
            import traceback

            entry["stack"] = "".join(traceback.format_stack(limit=12)[:-2])[-3000:]
        denials.append(entry)
    raise GuardViolation(f"BuildLens lab guard denied {event}")


def _relative(path: str) -> str:
    for root in _state["protected_roots"]:
        if _beneath(path, (root,)):
            return "<protected>/" + os.path.relpath(path, root)
    return path


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event == "open":
        path = _norm(args[0])
        if path is None:
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        )
        if writing:
            if path != os.devnull and not _beneath(path, _state["write_roots"]):
                _deny("open-write", _relative(path))
            return
        if _beneath(path, _state["protected_roots"]) and not _beneath(path, _state["read_roots"]):
            _deny("open-read", _relative(path))
        return
    if event in _FS_MUTATIONS:
        index = _FS_MUTATIONS[event]
        path = _norm(args[index]) if len(args) > index else None
        if path is not None and not _beneath(path, _state["write_roots"]):
            _deny(event, _relative(path))
        return
    if event in _EXEC_EVENTS or event.startswith("os.exec") or event.startswith("os.spawn"):
        _deny(event, "process creation")
    if event in {"socket.connect", "socket.sendto", "socket.bind"}:
        address = args[1] if len(args) > 1 else None
        if isinstance(address, tuple) and len(address) >= 2:
            key = (str(address[0]), int(address[1]))
            if event != "socket.bind" and key in _state["allow_net"]:
                return
            # urllib3 probes IPv6 support at import by binding ::1 port 0: no traffic, no
            # listener. Only an ephemeral loopback bind is tolerated; connects never are.
            if event == "socket.bind" and key[1] == 0 and key[0] in {"::1", "127.0.0.1"}:
                return
            _deny(event, f"{key[0]}:{key[1]}")
        if isinstance(address, (str, bytes)):  # AF_UNIX
            _deny(event, "unix-socket")
        return
    if event == "socket.getaddrinfo":
        host = args[0]
        host = host.decode() if isinstance(host, bytes) else host
        if host not in _state["allow_hosts"]:
            _deny(event, str(host))


def install_from_environment() -> dict[str, Any]:
    """Install once. Returns the effective (non-secret) policy for the hello frame."""
    if _state["installed"]:
        return policy()
    raw = json.loads(os.environ.get("BUILDLENS_LAB_GUARD", "{}"))

    def roots(key: str) -> tuple[str, ...]:
        return tuple(os.path.realpath(p) for p in raw.get(key, []) if isinstance(p, str))

    _state["write_roots"] = roots("write_roots")
    _state["read_roots"] = roots("read_roots") + tuple(
        os.path.realpath(p) for p in sys.path if p and os.path.isabs(p)
    )
    _state["protected_roots"] = roots("protected_roots")
    allow = [(str(h), int(p)) for h, p in raw.get("allow_net", [])]
    _state["allow_net"] = set(allow)
    _state["allow_hosts"] = {h for h, _ in allow}
    _state["trace"] = bool(raw.get("trace"))
    sys.addaudithook(_hook)
    _state["installed"] = True
    return policy()


def policy() -> dict[str, Any]:
    return {
        "installed": _state["installed"],
        "write_roots": len(_state["write_roots"]),
        "read_roots": len(_state["read_roots"]),
        "protected_roots": len(_state["protected_roots"]),
        "allow_net": sorted(f"{h}:{p}" for h, p in _state["allow_net"]),
    }


def denials() -> list[dict[str, str]]:
    return list(_state["denials"])
