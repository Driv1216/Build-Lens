"""Exact text overlays. This module never accesses or writes the filesystem."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_PATCH_BYTES = 256 * 1024
MAX_PATCH_FILES = 8
MAX_SOURCE_BYTES = 1_048_576
_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}
_HUNK = re.compile(r"@@ -(\d{1,8})(?:,(\d{1,8}))? \+(\d{1,8})(?:,(\d{1,8}))? @@(?: .*)?")
_LOCATOR = re.compile(r"[A-Za-z0-9_. /@()+-]+")


class PatchRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Hunk:
    old: int
    new: int
    preimage: tuple[str, ...]
    postimage: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PatchFile:
    locator: str
    hunks: tuple[Hunk, ...]


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lines(text: str) -> list[str]:
    if "\r" in text or "\x00" in text or (text and not text.endswith("\n")):
        raise PatchRejected("PATCH_UNSUPPORTED_LINE_FORMAT")
    return text.split("\n")[:-1]


def _locator(header: str) -> str:
    path = PurePosixPath(header)
    if (
        not _LOCATOR.fullmatch(header)
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != header
        or path.suffix not in _SUFFIXES
    ):
        raise PatchRejected("PATCH_UNSUPPORTED_PATH")
    return header


def parse_patch(text: str) -> tuple[PatchFile, ...]:
    if len(text.encode("utf-8")) > MAX_PATCH_BYTES:
        raise PatchRejected("PATCH_SIZE_LIMIT")
    files: list[PatchFile] = []
    lines = _lines(text)
    index = 0
    while index < len(lines):
        git_header = None
        if lines[index].startswith("diff --git "):
            git_header = lines[index]
            index += 1
            if index < len(lines) and re.fullmatch(
                r"index [a-f0-9.]+(?: 100[67][45][45])?", lines[index]
            ):
                index += 1
        if index + 1 >= len(lines) or not (
            lines[index].startswith("--- ") and lines[index + 1].startswith("+++ ")
        ):
            raise PatchRejected("PATCH_UNSUPPORTED_FILE_HEADER")
        old, new = lines[index][4:], lines[index + 1][4:]
        if old.startswith("a/") and new.startswith("b/"):
            old, new = old[2:], new[2:]
        locator = _locator(old)
        if old != new or any(file.locator == locator for file in files):
            raise PatchRejected("PATCH_UNSUPPORTED_FILE_OPERATION")
        if git_header is not None and git_header != f"diff --git a/{locator} b/{locator}":
            raise PatchRejected("PATCH_UNSUPPORTED_FILE_HEADER")
        index += 2
        hunks: list[Hunk] = []
        while index < len(lines) and lines[index].startswith("@@"):
            match = _HUNK.fullmatch(lines[index])
            if not match:
                raise PatchRejected("PATCH_INVALID_HUNK")
            old_at, old_count, new_at, new_count = match.groups()
            pre_count, post_count = int(old_count or "1"), int(new_count or "1")
            if pre_count == 0 or int(old_at) == 0:
                raise PatchRejected("PATCH_CONTEXT_REQUIRED")
            index += 1
            pre: list[str] = []
            post: list[str] = []
            has_context = False
            while len(pre) < pre_count or len(post) < post_count:
                if index >= len(lines) or not lines[index] or lines[index][0] not in " +-":
                    raise PatchRejected("PATCH_INVALID_HUNK")
                marker, value = lines[index][0], lines[index][1:]
                if marker in " -":
                    pre.append(value)
                if marker in " +":
                    post.append(value)
                has_context |= marker == " "
                index += 1
                if len(pre) > pre_count or len(post) > post_count:
                    raise PatchRejected("PATCH_INVALID_HUNK")
            if not has_context:
                raise PatchRejected("PATCH_CONTEXT_REQUIRED")
            post_at = int(new_at) - 1 if post_count else int(new_at)
            hunks.append(Hunk(int(old_at) - 1, post_at, tuple(pre), tuple(post)))
        if not hunks:
            raise PatchRejected("PATCH_INVALID_HUNK")
        files.append(PatchFile(locator, tuple(hunks)))
        if len(files) > MAX_PATCH_FILES:
            raise PatchRejected("PATCH_FILE_LIMIT")
    if not files:
        raise PatchRejected("PATCH_EMPTY")
    return tuple(files)


def apply_patch(file: PatchFile, data: bytes, expected: str) -> bytes:
    if len(data) > MAX_SOURCE_BYTES or fingerprint(data) != expected:
        raise PatchRejected("PATCH_BASELINE_MISMATCH")
    try:
        source = _lines(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise PatchRejected("PATCH_UNSUPPORTED_ENCODING") from None
    result: list[str] = []
    position = 0
    for hunk in file.hunks:
        end = hunk.old + len(hunk.preimage)
        if hunk.old < position or end > len(source):
            raise PatchRejected("PATCH_OVERLAPPING_OR_INVALID_HUNK")
        result.extend(source[position : hunk.old])
        if len(result) != hunk.new or tuple(source[hunk.old : end]) != hunk.preimage:
            raise PatchRejected("PATCH_CONTEXT_MISMATCH")
        result.extend(hunk.postimage)
        position = end
    result.extend(source[position:])
    value = ("\n".join(result) + ("\n" if result else "")).encode("utf-8")
    if len(value) > MAX_SOURCE_BYTES:
        raise PatchRejected("PATCH_POSTIMAGE_LIMIT")
    return value
