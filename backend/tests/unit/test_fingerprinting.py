from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from buildlens.application.fingerprinting import SnapshotFingerprinter
from buildlens.domain.entities import Artifact
from buildlens.domain.repository_snapshot import ArtifactMetadata
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader


def _artifact(locator: str, size: int) -> Artifact:
    return Artifact(
        uuid4(),
        uuid4(),
        "file",
        locator,
        datetime.now(UTC),
        ArtifactMetadata("included", size, "Python", ("source",), "text", "not_ignored", (), False),
    )


def test_exact_byte_fingerprints_distinguish_newlines_and_publish_empty_manifest(
    tmp_path: Path,
) -> None:
    (tmp_path / "lf.py").write_bytes(b"x = 1\n")
    (tmp_path / "crlf.py").write_bytes(b"x = 1\r\n")
    (tmp_path / "empty.py").write_bytes(b"")
    result = SnapshotFingerprinter(
        reader=SupervisedSourceReader(), max_file_bytes=1024, max_total_bytes=1024
    ).fingerprint(
        str(tmp_path), [_artifact("lf.py", 6), _artifact("crlf.py", 7), _artifact("empty.py", 0)]
    )
    fingerprints = {item.locator: item.content_fingerprint for item in result.artifacts}
    assert result.coverage.complete
    assert result.manifest is not None
    assert fingerprints["lf.py"] != fingerprints["crlf.py"]
    assert fingerprints["empty.py"] is not None


def test_hash_limits_are_independent_of_parser_limits(tmp_path: Path) -> None:
    payload = b"x" * 2048
    (tmp_path / "large.py").write_bytes(payload)
    result = SnapshotFingerprinter(
        reader=SupervisedSourceReader(), max_file_bytes=4096, max_total_bytes=4096
    ).fingerprint(str(tmp_path), [_artifact("large.py", len(payload))])
    assert result.artifacts[0].fingerprint_status == "fingerprinted"
