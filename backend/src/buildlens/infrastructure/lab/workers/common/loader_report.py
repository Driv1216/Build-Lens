"""Content-free per-file reporting shared by the framework loader experiments (LD-1)."""

from __future__ import annotations

import os
from typing import Any

from buildlens.domain.lab import sha256_hex


def relative(path: str, root: str) -> str:
    real_root = os.path.realpath(root)
    absolute = os.path.abspath(path)
    if absolute.startswith(real_root + os.sep) or absolute.startswith(root.rstrip(os.sep) + os.sep):
        base = real_root if absolute.startswith(real_root + os.sep) else root.rstrip(os.sep)
        return os.path.relpath(absolute, base)
    return "<outside-root>"


def describe(
    path: str, root: str, text: str, metadata: dict[str, Any], markers: dict[str, str]
) -> dict[str, Any]:
    """Never returns content: only sizes, hashes, marker hits and exposed metadata keys."""
    exposed = sorted(metadata)
    absolute = any(isinstance(v, str) and v.startswith(os.sep) for v in metadata.values())
    return {
        "path": relative(path, root),
        "chars": len(text),
        "sha256": sha256_hex(text),
        "contains_canary": markers["canary"] in text,
        "contains_bait": markers["bait"] in text,
        "contains_private_key_header": "PRIVATE KEY-----" in text,
        "metadata_keys": exposed,
        "absolute_path_in_metadata": absolute,
    }
