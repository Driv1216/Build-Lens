import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from buildlens.application.processors import (
    markdown_chunks,
    process_source,
    python_chunks,
    toml_chunks,
    yaml_chunks,
)
from buildlens.domain.source import ContentRejectedError, decode_source
from buildlens.infrastructure.discovery.filesystem import LocalRepositoryInspector
from buildlens.infrastructure.structural.parser import PythonAstParser
from buildlens.infrastructure.structural.reader import PosixSecureSourceReader, SourceReadError
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader


def test_git_executable_configuration_never_runs(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    hook = tmp_path / "monitor"
    marker = tmp_path / "executed"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.fsmonitor", str(hook)], check=True)
    LocalRepositoryInspector().inspect(str(tmp_path))
    assert not marker.exists()


def test_fifo_missing_and_substitutions_are_bounded(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    reader = SupervisedSourceReader(2)
    try:
        started = time.monotonic()
        with pytest.raises(SourceReadError) as failure:
            reader.read(str(tmp_path), "fifo", 1024)
        assert failure.value.code == "UNSAFE_FILE"
        assert time.monotonic() - started < 3
        with pytest.raises(SourceReadError) as failure:
            reader.read(str(tmp_path), "missing", 1024)
        assert failure.value.code == "SOURCE_NOT_FOUND"
        (tmp_path / "good").write_text("ok")
        (tmp_path / "link").symlink_to(tmp_path / "good")
        with pytest.raises(SourceReadError):
            reader.read(str(tmp_path), "link", 1024)
        with pytest.raises(SourceReadError) as failure:
            reader.read(str(tmp_path), "good", 1024, (0, 0))
        assert failure.value.code == "ROOT_CHANGED"
        assert reader.read(str(tmp_path), "good", 2).data == b"ok"
    finally:
        reader.close()


def test_read_failure_accounts_for_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "file").write_bytes(b"a" * 65537)
    original = os.read
    calls = 0

    def failing(fd: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("source-free injected failure")
        return original(fd, size)

    monkeypatch.setattr(os, "read", failing)
    with pytest.raises(SourceReadError) as failure:
        PosixSecureSourceReader().read(str(tmp_path), "file", 65537)
    assert failure.value.bytes_consumed == 65536


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("encoding", ["utf-8", "latin-1", "utf-8-sig"])
def test_python_encodings_newlines_exact_slices(newline: str, encoding: str) -> None:
    source = newline.join(
        [
            "# coding: " + ("latin-1" if encoding == "latin-1" else "utf-8"),
            "# café",
            "def f():",
            '    return "café"',
            "",
        ]
    )
    raw = source.encode(encoding)
    decoded, _ = decode_source(raw, True)
    parsed = PythonAstParser().parse(
        analysis_id=uuid4(), version_id=uuid4(), artifact_id=uuid4(), locator="code.py", data=raw
    )
    assert parsed.outcome == "parsed"
    chunks = python_chunks(decoded, uuid4(), "a", parsed.symbols)
    assert "".join(c.content for c in chunks) == source
    assert all(c.content == decoded[c.span.start_offset : c.span.end_offset] for c in chunks)
    assert any(c.span.start_line == 3 and c.symbol_id for c in chunks)


def test_repeated_module_and_nested_regions_have_unique_keys() -> None:
    source = (
        "x=1\ndef a():\n    def inner():\n        pass\n"
        "    return inner\nx=1\ndef b():\n    pass\nx=1\n"
    )
    parsed = PythonAstParser().parse(
        analysis_id=uuid4(),
        version_id=uuid4(),
        artifact_id=uuid4(),
        locator="a.py",
        data=source.encode(),
    )
    symbols = tuple(replace(s, stable_identity_id=uuid4()) for s in parsed.symbols)
    chunks = python_chunks(source, uuid4(), "a", symbols)
    assert len({c.stable_key for c in chunks}) == len(chunks)
    assert "".join(c.content for c in chunks) == source
    shifted = "\n" + source
    shifted_symbols = tuple(
        replace(
            s,
            full_start_line=s.full_start_line + 1 if s.full_start_line else None,
            declaration_span=replace(
                s.declaration_span,
                start_line=(s.declaration_span.start_line or 0) + 1,
                end_line=(s.declaration_span.end_line or 0) + 1,
            ),
        )
        for s in symbols
    )
    after = python_chunks(shifted, uuid4(), "a", shifted_symbols)
    assert {c.stable_key for c in chunks if c.symbol_id} == {
        c.stable_key for c in after if c.symbol_id
    }


def test_long_markdown_fence_duplicate_headings() -> None:
    source = "# a/b\n````python\n```\n# inside\n````\n# a\n## b\n# a/b\n"
    chunks, diags = markdown_chunks(source, uuid4(), "a")
    assert not diags
    assert "# inside" in chunks[0].content
    assert len({c.stable_key for c in chunks}) == len(chunks)
    assert "".join(c.content for c in chunks) == source


def test_toml_and_yaml_boundaries_are_exact_safe_units() -> None:
    toml = 'title = "x=y"\n[server]\nports = [\n  80, 443\n]\ntext = """a\n[b]\n"""\n'
    chunks, diag = toml_chunks(toml, uuid4(), "a")
    assert not diag and len(chunks) == 4
    assert "".join(c.content for c in chunks) == toml
    yaml = "first:\n  child: 1\nsecond: [2, 3]\n"
    chunks, diag = yaml_chunks(yaml, uuid4(), "a")
    assert not diag and len(chunks) == 2
    assert all(c.content == yaml[c.span.start_offset : c.span.end_offset] for c in chunks)


def test_exact_chunk_limit_discards_partial_artifact() -> None:
    chunks, _, _ = process_source("x" * 4096, "txt", uuid4(), "a", (), 1, 3)
    assert len(chunks) == 1 and len(chunks[0].content) == 4096
    with pytest.raises(ContentRejectedError) as failure:
        process_source("x" * 4097, "txt", uuid4(), "a", (), 1, 3)
    assert failure.value.code == "CHUNK_LIMIT"
    assert process_source("", "txt", uuid4(), "a", (), 1, 3)[0] == ()


def test_structural_screen_rejects_entire_artifact() -> None:
    result = PythonAstParser().parse(
        analysis_id=uuid4(),
        version_id=uuid4(),
        artifact_id=uuid4(),
        locator="a.py",
        data=b'def f(token="ghp_abcdefghijklmnopqrstuvwxyz0123456789"):\n    pass\n',
    )
    assert result.outcome == "failed" and not result.symbols
    assert result.diagnostics[0].code == "RECOGNIZABLE_GITHUB_TOKEN"
    assert "ghp_" not in repr(result)


def test_worker_timeout_and_crash_are_replaced(tmp_path: Path) -> None:
    import signal

    (tmp_path / "a").write_text("ok")
    reader = SupervisedSourceReader(2)
    try:
        assert reader.read(str(tmp_path), "a", 2).data == b"ok"
        os.kill(reader._process.pid, signal.SIGSTOP)
        reader.timeout = 0.05
        with pytest.raises(SourceReadError) as failure:
            reader.read(str(tmp_path), "a", 2)
        assert failure.value.code == "ARTIFACT_TIMEOUT"
        reader.timeout = 2
        assert reader.read(str(tmp_path), "a", 2).data == b"ok"
        os.kill(reader._process.pid, signal.SIGKILL)
        with pytest.raises(SourceReadError):
            reader.read(str(tmp_path), "a", 2)
        assert reader.read(str(tmp_path), "a", 2).data == b"ok"
    finally:
        reader.close()


def test_ignore_controls_reject_symlink_and_apply_nested_rules(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_text("*.txt\n!keep.txt\n")
    (tmp_path / "sub" / "drop.txt").write_text("x")
    (tmp_path / "sub" / "keep.txt").write_text("x")
    result = LocalRepositoryInspector().inspect(str(tmp_path))
    items = {x.locator: x for x in result.artifacts}
    assert items["sub/drop.txt"].metadata.inventory_status == "excluded"
    assert items["sub/keep.txt"].metadata.inventory_status == "included"
    (tmp_path / "sub" / ".gitignore").unlink()
    (tmp_path / "sub" / ".gitignore").symlink_to(tmp_path / "sub" / "keep.txt")
    result = LocalRepositoryInspector().inspect(str(tmp_path))
    assert any(w.code == "IGNORE_RULES_UNREADABLE" for w in result.warnings)
    assert all(x.metadata.inventory_status == "excluded" for x in result.artifacts)
