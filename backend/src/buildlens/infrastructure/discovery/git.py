"""Bounded Git metadata commands with executable configuration disabled."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time


def _git_output(
    root: str,
    arguments: list[str],
    timeout: float,
    input_text: str | None = None,
    max_output: int = 8 * 1024 * 1024,
    configuration: tuple[str, ...] = (),
) -> str | None:
    command = [
        "git",
        "-c",
        f"core.excludesFile={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "submodule.recurse=false",
        *configuration,
        "-C",
        root,
        *arguments,
    ]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    process = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        assert process.stdin and process.stdout
        payload = (input_text or "").encode("utf-8")
        sent = 0
        chunks: list[bytes] = []
        size = 0
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            if payload:
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                for key, _ in selector.select(min(remaining, 0.1)):
                    if key.fileobj is process.stdin:
                        sent += os.write(process.stdin.fileno(), payload[sent : sent + 65536])
                        if sent == len(payload):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            selector.unregister(process.stdout)
                        else:
                            size += len(chunk)
                            if size > max_output:
                                return None
                            chunks.append(chunk)
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode == 0 or (
            arguments[0] in {"check-ignore", "config"} and process.returncode == 1
        ):
            return b"".join(chunks).decode("utf-8")
        return None
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return None
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()


def git_output(
    root: str,
    arguments: list[str],
    timeout: float,
    input_text: str | None = None,
    max_output: int = 8 * 1024 * 1024,
) -> str | None:
    """Disable configured clean/process filters before status refresh can hash files."""
    deadline = time.monotonic() + timeout
    configuration: list[str] = []
    if arguments and arguments[0] == "status":
        names = _git_output(
            root, ["config", "--includes", "--name-only", "--get-regexp", r"^filter\."], timeout
        )
        if names is None:
            return None
        filters = {
            name.rpartition(".")[0] for name in names.splitlines() if name.startswith("filter.")
        }
        for name in sorted(filters):
            for suffix, value in (
                ("clean", ""),
                ("smudge", ""),
                ("process", ""),
                ("required", "false"),
            ):
                configuration.extend(("-c", f"{name}.{suffix}={value}"))
    return _git_output(
        root,
        arguments,
        max(0.001, deadline - time.monotonic()),
        input_text,
        max_output,
        tuple(configuration),
    )
