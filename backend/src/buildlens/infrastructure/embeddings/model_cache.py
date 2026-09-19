"""Pinned model acquisition and offline cache verification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
MANIFEST_NAME = "buildlens-model-manifest.json"
REQUIRED_FILES = (
    "1_Pooling/config.json",
    "config.json",
    "config_sentence_transformers.json",
    "model.safetensors",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
)


def default_cache_root() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    base = Path(configured) if configured else Path.home() / ".cache"
    return base / "buildlens" / "embeddings"


def snapshot_path(cache_root: Path, revision: str = MODEL_REVISION) -> Path:
    return cache_root / "all-MiniLM-L6-v2" / revision


def _checksum(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def verify_snapshot(path: Path, revision: str = MODEL_REVISION) -> tuple[bool, str | None]:
    manifest_path = path / MANIFEST_NAME
    if not manifest_path.is_file():
        return False, "MODEL_MANIFEST_MISSING"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if value.get("model_id") != MODEL_ID or value.get("revision") != revision:
            return False, "MODEL_REVISION_MISMATCH"
        files = value.get("files")
        if not isinstance(files, dict):
            return False, "MODEL_MANIFEST_INVALID"
        for name in REQUIRED_FILES:
            expected = files.get(name)
            candidate = path / name
            if (
                not isinstance(expected, str)
                or not candidate.is_file()
                or candidate.is_symlink()
                or _checksum(candidate) != expected
            ):
                return False, "MODEL_CHECKSUM_MISMATCH"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, "MODEL_MANIFEST_INVALID"
    return True, None


def download_snapshot(cache_root: Path, revision: str = MODEL_REVISION) -> Path:
    if len(revision) != 40 or any(value not in "0123456789abcdef" for value in revision):
        raise ValueError("Embedding model revision must be a full lowercase commit SHA.")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise RuntimeError("Install BuildLens with the embeddings extra first.") from error

    target = snapshot_path(cache_root, revision)
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=MODEL_ID,
        revision=revision,
        local_dir=target,
        allow_patterns=list(REQUIRED_FILES),
        ignore_patterns=["*.bin", "*.h5", "*.ot", "onnx/*", "openvino/*"],
        token=False,
    )
    checksums: dict[str, str] = {}
    for name in REQUIRED_FILES:
        candidate = target / name
        if not candidate.is_file():
            raise RuntimeError("Pinned model download is incomplete.")
        checksums[name] = _checksum(candidate)
    manifest: dict[str, Any] = {
        "schema": "buildlens.embedding-model-manifest.v1",
        "model_id": MODEL_ID,
        "revision": revision,
        "files": checksums,
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    valid, reason = verify_snapshot(target, revision)
    if not valid:
        raise RuntimeError(f"Pinned model verification failed: {reason}")
    return target
