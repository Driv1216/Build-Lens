from uuid import uuid4

import pytest

from buildlens.application.processors import (
    ContentRejectedError,
    generic_chunks,
    json_chunks,
    markdown_chunks,
    screen_content,
)


def test_generic_chunks_are_verbatim_unicode_slices() -> None:
    source = "alpha\nβeta\n"
    chunks = generic_chunks(source, uuid4(), "artifact")
    assert "".join(item.content for item in chunks) == source
    assert chunks[0].span.start_offset == 0
    assert chunks[0].span.end_offset == len(source)


def test_markdown_parent_does_not_duplicate_child_section() -> None:
    chunks, diagnostics = markdown_chunks("# Parent\ntext\n## Child\nbody\n", uuid4(), "a")
    assert not diagnostics
    assert [chunk.content for chunk in chunks] == ["# Parent\ntext\n", "## Child\nbody\n"]


def test_json_preserves_duplicate_members_in_source_order() -> None:
    chunks, diagnostics = json_chunks('{"same": 1, "same": 2}', uuid4(), "a")
    assert not diagnostics
    assert [chunk.content for chunk in chunks] == ['"same": 1', '"same": 2']
    assert [chunk.json_path for chunk in chunks] == ['["same",0]', '["same",1]']


def test_secret_screen_keeps_placeholders_but_rejects_recognizable_token() -> None:
    screen_content('password="password"\napi_key="YOUR_API_KEY"\n')
    with pytest.raises(ContentRejectedError) as error:
        screen_content("ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    assert error.value.code == "RECOGNIZABLE_GITHUB_TOKEN"
