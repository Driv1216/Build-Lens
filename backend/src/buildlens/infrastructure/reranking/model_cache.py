"""Explicit acquisition and verification of the pinned Stage 9 reranker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

MODEL_ID = "cross-encoder/ms-marco-MiniLM-L6-v2"
MODEL_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
MODEL_SAFETENSORS_SHA256 = "821d1aa69520101d6e0737f78a042ae25b19e5cb9160701909d10434f4aeb0ae"
MANIFEST_NAME = "buildlens-reranker-manifest.json"
REQUIRED_FILES = (
    "README.md",
    "config.json",
    "model.safetensors",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
)


def default_cache_root() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    base = Path(configured) if configured else Path.home() / ".cache"
    return base / "buildlens" / "reranking"


def snapshot_path(cache_root: Path, revision: str = MODEL_REVISION) -> Path:
    return cache_root / "ms-marco-MiniLM-L6-v2" / revision


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_snapshot(path: Path, revision: str = MODEL_REVISION) -> tuple[bool, str | None]:
    if path.is_symlink():
        return False, "RERANKER_CACHE_SYMLINK"
    manifest_path = path / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return False, "RERANKER_MANIFEST_MISSING"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("model_id") != MODEL_ID or manifest.get("revision") != revision:
            return False, "RERANKER_REVISION_MISMATCH"
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != set(REQUIRED_FILES):
            return False, "RERANKER_MANIFEST_INVALID"
        for name in REQUIRED_FILES:
            candidate = path / name
            expected = files.get(name)
            if (
                not isinstance(expected, str)
                or not candidate.is_file()
                or candidate.is_symlink()
                or _checksum(candidate) != expected
            ):
                return False, "RERANKER_CHECKSUM_MISMATCH"
        if files["model.safetensors"] != MODEL_SAFETENSORS_SHA256:
            return False, "RERANKER_WEIGHT_CHECKSUM_MISMATCH"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, "RERANKER_MANIFEST_INVALID"
    return True, None


def download_snapshot(cache_root: Path, revision: str = MODEL_REVISION) -> Path:
    if len(revision) != 40 or any(value not in "0123456789abcdef" for value in revision):
        raise ValueError("Reranker revision must be a full lowercase commit SHA.")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise RuntimeError("Install BuildLens with the reranking extra first.") from error
    target = snapshot_path(cache_root, revision)
    if target.is_symlink():
        raise RuntimeError("Pinned reranker cache path must not be a symlink.")
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=MODEL_ID,
        revision=revision,
        local_dir=target,
        allow_patterns=list(REQUIRED_FILES),
        ignore_patterns=[
            "*.bin",
            "*.h5",
            "*.ot",
            "*.py",
            "onnx/*",
            "openvino/*",
        ],
        token=False,
    )
    checksums: dict[str, str] = {}
    for name in REQUIRED_FILES:
        candidate = target / name
        if not candidate.is_file() or candidate.is_symlink():
            raise RuntimeError("Pinned reranker download is incomplete.")
        checksums[name] = _checksum(candidate)
    if checksums["model.safetensors"] != MODEL_SAFETENSORS_SHA256:
        raise RuntimeError("Pinned reranker weights failed published checksum verification.")
    manifest: dict[str, Any] = {
        "schema": "buildlens.reranker-model-manifest.v1",
        "model_id": MODEL_ID,
        "revision": revision,
        "files": checksums,
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    valid, reason = verify_snapshot(target, revision)
    if not valid:
        raise RuntimeError(f"Pinned reranker verification failed: {reason}")
    return target
