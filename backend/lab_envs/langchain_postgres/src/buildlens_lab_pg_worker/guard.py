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
* writes, deletes, chmod, mkdir: only beneath write_roots;
* rename/replace/move: the source AND the destination must be beneath write_roots;
* hard links: the source AND the destination must be beneath write_roots (a hard link
  aliases the source inode, so linking a protected or out-of-root file is denied);
* paths relative to a directory descriptor (dir_fd) are resolved through that descriptor,
  and descriptor-only mutations (fchmod etc.) through the descriptor's path; anything that
  cannot be resolved is denied;
* chdir is denied, so relative paths always resolve against the fixed working directory;
* reads beneath protected_roots (e.g. the user's home and registered repositories) are
  denied unless they are also beneath read_roots (venv, package source, index, sandbox).

Known limitation: the `open` audit event carries no dir_fd, so a relative open through a
directory descriptor is checked as if relative to the working directory.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from typing import Any

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
# event -> (path argument index, dir_fd argument index or None) for every path it mutates.
_FS_MUTATIONS: dict[str, tuple[tuple[int, int | None], ...]] = {
    "os.remove": ((0, 1),),
    "os.rmdir": ((0, 1),),
    "os.mkdir": ((0, 2),),
    "os.chmod": ((0, 2),),
    "os.chown": ((0, 3),),
    "os.utime": ((0, 3),),
    "os.truncate": ((0, None),),
    "os.chflags": ((0, None),),
    "os.setxattr": ((0, None),),
    "os.removexattr": ((0, None),),
    "os.rename": ((0, 2), (1, 3)),  # also raised by os.replace
    "os.link": ((0, 2), (1, 3)),
    "os.symlink": ((1, 2),),  # the target is checked when opened (realpath)
    "shutil.rmtree": ((0, 1),),
    "shutil.chown": ((0, None),),
    "shutil.copyfile": ((1, None),),
    "shutil.copymode": ((1, None),),
    "shutil.copystat": ((1, None),),
    "shutil.copytree": ((1, None),),
    "shutil.move": ((0, None), (1, None)),
    "shutil.unpack_archive": ((1, None),),
}
_UNRESOLVED = "<unresolved-path>"

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


def _fd_path(fd: int) -> str | None:
    try:
        if hasattr(fcntl, "F_GETPATH"):  # macOS
            raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return os.fsdecode(raw.split(b"\0", 1)[0]) or None
        return os.readlink(f"/proc/self/fd/{fd}")  # Linux
    except (OSError, ValueError):
        return None


def _resolve(path: Any, dir_fd: Any) -> str | None:
    """Mutation target: descriptors and dir_fd-relative paths resolve through the descriptor."""
    if isinstance(path, int):
        base = _fd_path(path)
        return _norm(base) if base is not None else _UNRESOLVED
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, (str, os.PathLike)):
        return None
    raw = os.fsdecode(os.fspath(path))
    if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(raw):
        base = _fd_path(dir_fd)
        if base is None:
            return _UNRESOLVED
        raw = os.path.join(base, raw)
    return _norm(raw)


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
        for index, fd_index in _FS_MUTATIONS[event]:
            if len(args) <= index:
                continue
            dir_fd = args[fd_index] if fd_index is not None and len(args) > fd_index else None
            path = _resolve(args[index], dir_fd)
            if path is not None and not _beneath(path, _state["write_roots"]):
                _deny(event, _relative(path))
        return
    if event == "os.chdir":  # also raised by os.fchdir
        _deny(event, "working directory is fixed")
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
