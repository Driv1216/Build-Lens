"""Stage 14 pure API-matching rules (``api-matching.v3``)."""

from __future__ import annotations

from buildlens.application.architecture_api import (
    backend_pattern,
    client_pattern,
    fastapi_prefix,
    flask_join,
    host_class,
    intersects,
    normalise_fetch_method,
)


def _backend(path: str, framework: str = "fastapi") -> list[tuple[str, object]]:
    compiled = backend_pattern(path, framework)
    assert compiled is not None
    return compiled[0]


def test_segment_and_multi_segment_parameters() -> None:
    segment = _backend("/items/{item_id}")
    assert intersects(client_pattern([("s", "/items/"), ("e", "id")]), segment)
    assert not intersects(client_pattern([("s", "/items")]), segment)  # a segment is non-empty
    assert not intersects(client_pattern([("s", "/items/a/b")]), segment)
    # Starlette ``{p:path}`` is ``.*``: it may be empty and may span segments.
    starlette = _backend("/files/{path:path}")
    assert intersects(client_pattern([("s", "/files/")]), starlette)
    assert intersects(client_pattern([("s", "/files/a/b/c")]), starlette)
    # Flask ``<path:p>`` is one or more characters.
    flask = _backend("/files/<path:p>", "flask")
    assert not intersects(client_pattern([("s", "/files/")]), flask)
    assert intersects(client_pattern([("s", "/files/a/b")]), flask)


def test_client_placeholders_may_span_segments_unless_strict() -> None:
    evidence = _backend("/s/{s}/architecture/evidence/{digest}")
    loose = client_pattern([("s", "/s/"), ("e", "id"), ("s", "/embeddings")])
    strict = client_pattern([("s", "/s/"), ("e", "id"), ("s", "/embeddings")], strict=True)
    assert intersects(loose, evidence)  # ``id`` could be "x/architecture/evidence"
    assert not intersects(strict, evidence)
    # Unbounded placeholders (query suffixes) are never made strict.
    tree = client_pattern([("s", "/tree"), ("e", "query")], strict=True)
    assert intersects(tree, _backend("/tree"))


def test_typed_converters_and_unknown_syntax() -> None:
    compiled = backend_pattern("/items/{item_id:int}", "fastapi")
    assert compiled is not None and compiled[1] == {"typed"}
    assert backend_pattern("/items/{item_id:custom}", "fastapi") is None
    assert backend_pattern("/broken/{unclosed", "fastapi") is None


def test_prefix_rules_and_flask_join() -> None:
    assert fastapi_prefix("") and fastapi_prefix("/api")
    assert not fastapi_prefix("api") and not fastapi_prefix("/api/")
    assert flask_join("/admin/", "/stats") == "/admin/stats"
    assert flask_join("/admin", "") == "/admin"
    assert flask_join("", "/x") == "/x"


def test_destination_classes_never_bind_hosts() -> None:
    assert host_class("localhost") == "local_loopback"
    assert host_class("127.0.0.1") == "local_loopback"
    assert host_class("[::1]") == "local_loopback"
    assert host_class("10.0.0.5") == "private_network_host_unbound"
    assert host_class("169.254.1.1") == "private_network_host_unbound"
    assert host_class("api.example.com") == "public_host_unbound"
    assert host_class("8.8.8.8") == "public_host_unbound"


def test_fetch_method_normalisation_is_byte_exact_beyond_six_methods() -> None:
    assert normalise_fetch_method("post") == ("POST", ())
    assert normalise_fetch_method("delete") == ("DELETE", ())
    assert normalise_fetch_method("patch") == ("patch", ("METHOD_NOT_NORMALISED_BY_FETCH",))
    assert normalise_fetch_method("PATCH") == ("PATCH", ())


def test_typed_converters_accept_only_their_characters() -> None:
    integer = _backend("/items/{item_id:int}")
    assert intersects(client_pattern([("s", "/items/42")]), integer)
    assert not intersects(client_pattern([("s", "/items/abc")]), integer)  # v2 defect (D4)
    assert intersects(client_pattern([("s", "/items/"), ("e", "id")], strict=True), integer)
    uuid = _backend("/runs/{run_id:uuid}")
    assert intersects(client_pattern([("s", "/runs/0f-1a")]), uuid)
    assert not intersects(client_pattern([("s", "/runs/latest")]), uuid)
    flask = _backend("/items/<int:item_id>", "flask")
    assert not intersects(client_pattern([("s", "/items/x1")]), flask)
