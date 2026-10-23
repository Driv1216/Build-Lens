"""Descriptor-relative POSIX reads. All failures carry source-free diagnostics."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import time
from collections.abc import Callable

from buildlens.application.source_ports import SecureSourceReader as SecureSourceReader
from buildlens.application.source_ports import SourceFingerprint as SourceFingerprint
from buildlens.application.source_ports import SourceRead as SourceRead
from buildlens.application.source_ports import SourceReadError as SourceReadError
from buildlens.domain.eligibility import exclusion_reason


def directory_at(root_fd: int, locator: str) -> int:
    """Return an owned descriptor below a pinned root, never crossing a mount."""
    fd = os.dup(root_fd)
    device = os.fstat(root_fd).st_dev
    try:
        for part in locator.split("/") if locator else []:
            if part in {"", ".", ".."}:
                raise SourceReadError("UNSAFE_LOCATOR")
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            os.close(fd)
            fd = child
            if os.fstat(fd).st_dev != device:
                raise SourceReadError("UNSAFE_PATH")
        return fd
    except BaseException:
        os.close(fd)
        raise


class PosixSecureSourceReader(SecureSourceReader):
    def __init__(self, progress: Callable[[int], None] | None = None) -> None:
        self.progress = progress

    def read(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> SourceRead:
        chunks: list[bytes] = []
        size = self._consume(
            root, locator, max_bytes, time.monotonic() + 3, chunks.append, expected_root_identity
        )
        return SourceRead(b"".join(chunks), size)

    def fingerprint(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        timeout_seconds: float,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> SourceFingerprint:
        digest = hashlib.sha256()
        size = self._consume(
            root,
            locator,
            max_bytes,
            time.monotonic() + timeout_seconds,
            digest.update,
            expected_root_identity,
        )
        return SourceFingerprint("sha256", "artifact-bytes.v1", digest.hexdigest(), size)

    def read_ignore_control(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        expected_root_identity: tuple[int, int] | None = None,
    ) -> SourceRead:
        """Inventory-only control read; never a source-processing entry point.

        Git's exact ignore-control file lives under excluded VCS metadata. It
        still uses descriptor-relative path, mount, size and identity checks.
        Every other locator uses the normal shared source eligibility policy.
        """
        if locator != ".git/info/exclude":
            return self.read(root, locator, max_bytes, expected_root_identity)
        chunks: list[bytes] = []
        size = self._consume(
            root,
            locator,
            max_bytes,
            time.monotonic() + 3,
            chunks.append,
            expected_root_identity,
            ignore_control=True,
        )
        return SourceRead(b"".join(chunks), size)

    def _consume(
        self,
        root: str,
        locator: str,
        max_bytes: int,
        deadline: float,
        consume: Callable[[bytes], object],
        expected_root_identity: tuple[int, int] | None,
        *,
        ignore_control: bool = False,
    ) -> int:
        if not self._valid_locator(locator):
            raise SourceReadError("UNSAFE_LOCATOR")
        if exclusion_reason(locator, False) and not (
            ignore_control and locator == ".git/info/exclude"
        ):
            raise SourceReadError("SOURCE_EXCLUDED")
        count = 0
        root_fd = parent_fd = file_fd = None
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
            root_stat = os.fstat(root_fd)
            if (
                expected_root_identity
                and (root_stat.st_dev, root_stat.st_ino) != expected_root_identity
            ):
                raise SourceReadError("ROOT_CHANGED")
            parent, _, name = locator.rpartition("/")
            parent_fd = directory_at(root_fd, parent)
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_dev != root_stat.st_dev:
                raise SourceReadError("UNSAFE_FILE")
            if before.st_size > max_bytes:
                raise SourceReadError("SOURCE_TOO_LARGE")
            while count < before.st_size:
                if time.monotonic() >= deadline:
                    raise SourceReadError("ARTIFACT_TIMEOUT")
                chunk = os.read(file_fd, min(65536, before.st_size - count))
                if not chunk:
                    break
                count += len(chunk)
                if self.progress:
                    self.progress(count)
                consume(chunk)
            after = os.fstat(file_fd)
            if count != before.st_size or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise SourceReadError("FILE_CHANGED")
            return count
        except SourceReadError as error:
            error.bytes_consumed = count
            raise
        except OSError as error:
            code = {
                errno.ENOENT: "SOURCE_NOT_FOUND",
                errno.EACCES: "SOURCE_UNREADABLE",
                errno.EPERM: "SOURCE_UNREADABLE",
                errno.ELOOP: "UNSAFE_PATH",
                errno.ENOTDIR: "UNSAFE_PATH",
            }.get(error.errno or 0, "SOURCE_IO_ERROR")
            raise SourceReadError(code, bytes_consumed=count) from None
        finally:
            for fd in (file_fd, parent_fd, root_fd):
                if fd is not None:
                    os.close(fd)

    @staticmethod
    def _valid_locator(locator: str) -> bool:
        return (
            bool(locator)
            and not locator.startswith("/")
            and "\x00" not in locator
            and all(p not in {"", ".", ".."} for p in locator.split("/"))
        )
