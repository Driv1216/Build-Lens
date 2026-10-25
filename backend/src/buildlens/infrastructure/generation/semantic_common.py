"""Helpers shared by the semantic generation adapters only (Ask adapters are untouched)."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version

UNTRUSTED_PREFIX = "Untrusted project data (JSON):\n"
SCHEMA_NAME = "buildlens_semantic_claims"


def package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def retry_after(headers: Mapping[str, str] | None) -> float | None:
    """Seconds from a ``Retry-After`` delta header; HTTP dates are ignored (no retry)."""
    if not headers:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and 0 <= seconds <= 3600 else None


def generation_policy(max_output_tokens: int, **extra: object) -> dict[str, object]:
    return {
        "temperature": 0,
        "max_output_tokens": max_output_tokens,
        "structured_output": "json_schema",
        "streaming": False,
        "tools": False,
        "automatic_retries": 0,
        **extra,
    }
