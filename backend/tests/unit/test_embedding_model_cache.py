import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from buildlens.domain.embeddings import EmbeddingVector
from buildlens.infrastructure.embeddings.model_cache import (
    MANIFEST_NAME,
    MODEL_ID,
    MODEL_REVISION,
    REQUIRED_FILES,
    verify_snapshot,
)
from buildlens.infrastructure.embeddings.supervisor import SupervisedSentenceTransformer


def write_snapshot(root: Path) -> None:
    checksums: dict[str, str] = {}
    for name in REQUIRED_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        content = name.encode("utf-8")
        target.write_bytes(content)
        checksums[name] = hashlib.sha256(content).hexdigest()
    (root / MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema": "buildlens.embedding-model-manifest.v1",
                "model_id": MODEL_ID,
                "revision": MODEL_REVISION,
                "files": checksums,
            }
        ),
        encoding="utf-8",
    )


def test_model_snapshot_requires_manifest_revision_and_every_checksum(tmp_path: Path) -> None:
    write_snapshot(tmp_path)
    assert verify_snapshot(tmp_path) == (True, None)
    (tmp_path / REQUIRED_FILES[-1]).write_text("changed", encoding="utf-8")
    assert verify_snapshot(tmp_path) == (False, "MODEL_CHECKSUM_MISMATCH")


def test_runtime_rejects_floating_model_revision(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SupervisedSentenceTransformer(tmp_path, revision="main")


def test_query_timeout_is_forwarded_as_a_hard_request_budget(tmp_path: Path) -> None:
    provider = SupervisedSentenceTransformer(tmp_path)
    vector = EmbeddingVector((1.0,) + (0.0,) * 383, 1)
    with patch.object(provider, "_request", return_value=[vector]) as request:
        assert provider.embed_query_with_timeout("find user", 1.25) == vector
    request.assert_called_once_with("query", ["find user"], timeout_seconds=1.25)
