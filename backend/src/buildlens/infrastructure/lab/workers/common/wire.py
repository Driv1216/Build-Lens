"""Stage 12 worker wire protocol: bounded, length-prefixed JSON frames.

Stdlib-only. The isolated langchain-postgres environment vendors this exact file; a test
compares the bytes so both sides always speak the same protocol.

Frame: 4-byte big-endian payload length, then UTF-8 JSON (an object, no NaN/Infinity,
no duplicate keys). Kinds:

  worker -> parent  hello     {protocol, framework, versions, pid}
  parent -> worker  request   {id, op, params}
  worker -> parent  call      {id, method, params}        (callback during a request)
  parent -> worker  reply     {id, ok, result | error}
  worker -> parent  response  {id, ok, result | error}
"""

from __future__ import annotations

import json
import os
import selectors
import struct
import time
from typing import Any, BinaryIO

PROTOCOL = "stage12.worker.v1"
MAX_FRAME_BYTES = 8 * 1024 * 1024
KINDS = frozenset({"hello", "request", "call", "reply", "response"})
_HEADER = struct.Struct(">I")


class WireError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise WireError("WIRE_DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> Any:
    raise WireError("WIRE_NONFINITE_NUMBER")


def encode(message: dict[str, Any]) -> bytes:
    if message.get("kind") not in KINDS:
        raise WireError("WIRE_KIND_INVALID")
    try:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise WireError("WIRE_UNSERIALIZABLE") from error
    data = payload.encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise WireError("WIRE_FRAME_TOO_LARGE")
    return _HEADER.pack(len(data)) + data


def decode(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WireError("WIRE_JSON_INVALID") from error
    if not isinstance(value, dict) or value.get("kind") not in KINDS:
        raise WireError("WIRE_KIND_INVALID")
    return value


def write_frame(stream: BinaryIO, message: dict[str, Any]) -> None:
    stream.write(encode(message))
    stream.flush()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise WireError("WIRE_EOF")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO) -> dict[str, Any]:
    """Blocking read used by workers (the parent enforces deadlines by killing them)."""
    (size,) = _HEADER.unpack(_read_exact(stream, _HEADER.size))
    if size > MAX_FRAME_BYTES:
        raise WireError("WIRE_FRAME_TOO_LARGE")
    return decode(_read_exact(stream, size))


class DeadlineReader:
    """Non-blocking frame reader for the parent; never waits beyond the given deadline."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._buffer = bytearray()
        self._selector = selectors.DefaultSelector()
        self._selector.register(fd, selectors.EVENT_READ)

    def close(self) -> None:
        self._selector.close()

    def read(self, deadline: float, poll: float = 0.2) -> dict[str, Any] | None:
        """Return one frame, or None when `poll` elapsed without a full frame."""
        poll_until = min(deadline, time.monotonic() + poll)
        while True:
            frame = self._take()
            if frame is not None:
                return frame
            remaining = poll_until - time.monotonic()
            if remaining <= 0:
                if time.monotonic() >= deadline:
                    raise WireError("WIRE_DEADLINE")
                return None
            if not self._selector.select(remaining):
                continue
            chunk = os.read(self._fd, 1024 * 1024)
            if not chunk:
                raise WireError("WIRE_EOF")
            self._buffer.extend(chunk)
            if len(self._buffer) > MAX_FRAME_BYTES + _HEADER.size:
                (size,) = _HEADER.unpack(bytes(self._buffer[: _HEADER.size]))
                if size > MAX_FRAME_BYTES:
                    raise WireError("WIRE_FRAME_TOO_LARGE")

    def _take(self) -> dict[str, Any] | None:
        if len(self._buffer) < _HEADER.size:
            return None
        (size,) = _HEADER.unpack(bytes(self._buffer[: _HEADER.size]))
        if size > MAX_FRAME_BYTES:
            raise WireError("WIRE_FRAME_TOO_LARGE")
        if len(self._buffer) < _HEADER.size + size:
            return None
        data = bytes(self._buffer[_HEADER.size : _HEADER.size + size])
        del self._buffer[: _HEADER.size + size]
        return decode(data)
