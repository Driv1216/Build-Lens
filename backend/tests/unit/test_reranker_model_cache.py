import hashlib
import json
from pathlib import Path

import pytest

from buildlens.infrastructure.reranking import model_cache
from buildlens.infrastructure.reranking.model_cache import (
    MANIFEST_NAME,
    MODEL_ID,
    MODEL_REVISION,
    REQUIRED_FILES,
    verify_snapshot,
)
from buildlens.infrastructure.reranking.supervisor import SupervisedCrossEncoder


def write_snapshot(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checksums: dict[str, str] = {}
    for name in REQUIRED_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        content = name.encode()
        target.write_bytes(content)
        checksums[name] = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(model_cache, "MODEL_SAFETENSORS_SHA256", checksums["model.safetensors"])
    (root / MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": "buildlens.reranker-model-manifest.v1",
                "model_id": MODEL_ID,
                "revision": MODEL_REVISION,
                "files": checksums,
            }
        ),
        encoding="utf-8",
    )


def test_reranker_snapshot_requires_exact_allowlist_and_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_snapshot(tmp_path, monkeypatch)
    assert verify_snapshot(tmp_path) == (True, None)
    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text())
    manifest["files"]["unexpected.bin"] = "0" * 64
    (tmp_path / MANIFEST_NAME).write_text(json.dumps(manifest))
    assert verify_snapshot(tmp_path) == (False, "RERANKER_MANIFEST_INVALID")


def test_reranker_snapshot_rejects_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_snapshot(tmp_path, monkeypatch)
    original = tmp_path / "vocab.txt"
    target = tmp_path / "target.txt"
    target.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(target)
    assert verify_snapshot(tmp_path) == (False, "RERANKER_CHECKSUM_MISMATCH")


def test_reranker_rejects_floating_revision(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SupervisedCrossEncoder(tmp_path, revision="main")
