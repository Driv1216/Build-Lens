"""Shared source policy, decoding and producer identities; no infrastructure dependencies."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import tokenize
from importlib.metadata import version
from io import BytesIO

READER_VERSION = "posix-secure-reader.v2"
SCREENING_VERSION = "secret-screen.v2"
PARSER_VERSION = "python-ast.v2"
PROCESSING_VERSION = "stage5.native.v2"

_TOKEN = re.compile(r"(?<![A-Za-z0-9_])(ghp_[A-Za-z0-9]{36})(?![A-Za-z0-9_])")
_PEM = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----\s*([A-Za-z0-9+/=\s]+?)\s*"
    r"-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
    re.S,
)
_OPENSSH = re.compile(
    r"-----BEGIN OPENSSH PRIVATE KEY-----\s*([A-Za-z0-9+/=\s]+?)\s*"
    r"-----END OPENSSH PRIVATE KEY-----",
    re.S,
)


class ContentRejectedError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def screen_content(source: str) -> None:
    """Reject only recognizable secret evidence; never include a match in errors."""
    for match in _TOKEN.finditer(source):
        payload = match.group(1)[4:]
        if len(set(payload)) >= 8:
            raise ContentRejectedError("RECOGNIZABLE_GITHUB_TOKEN")
    for matcher, magic in ((_OPENSSH, b"openssh-key-v1\x00"), (_PEM, None)):
        for match in matcher.finditer(source):
            try:
                data = base64.b64decode(re.sub(r"\s+", "", match.group(1)), validate=True)
            except Exception:
                continue
            if (magic is not None and data.startswith(magic)) or (
                magic is None and len(data) >= 32 and data[:1] == b"0"
            ):
                raise ContentRejectedError("PRIVATE_KEY_MATERIAL")


def decode_source(data: bytes, python: bool = False) -> tuple[str, str]:
    # Encoding detection uses universal newlines; decoded source retains its original bytes.
    header = re.sub(rb"\r\n|\r", b"\n", data)
    encoding = tokenize.detect_encoding(BytesIO(header).readline)[0] if python else "utf-8-sig"
    source = data.decode(encoding, errors="strict")
    if "\x00" in source or any(ord(c) < 32 and c not in "\n\r\t\f" for c in source):
        raise ContentRejectedError("BINARY_CONTENT")
    return source, encoding


def line_offsets(source: str) -> list[int]:
    return [0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", source))]


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def structural_contract() -> dict[str, str]:
    return {
        "parser_version": PARSER_VERSION,
        "runtime_version": sys.version.split()[0],
        "reader": READER_VERSION,
        "screening": SCREENING_VERSION,
        "fingerprint": "sha256:artifact-bytes.v1",
        "decoding": "python-tokenize.v2",
        "extraction": "python-declaration-slot.v2",
    }


def processing_contract() -> dict[str, str]:
    return {
        "processing": PROCESSING_VERSION,
        "runtime": sys.version.split()[0],
        "yaml": version("PyYAML"),
        "decoding": "python-tokenize.v2+utf8-sig.v1",
        "boundaries": "recursive-zero-overlap.v2",
        "reader": READER_VERSION,
        "screening": SCREENING_VERSION,
        "fingerprint": "sha256:artifact-bytes.v1",
    }
