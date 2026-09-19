from uuid import uuid4

from buildlens.domain.structural import ArtifactAnalysis
from buildlens.infrastructure.structural.parser import PythonAstParser


def parse(source: str) -> ArtifactAnalysis:
    return PythonAstParser().parse(
        analysis_id=uuid4(),
        version_id=uuid4(),
        artifact_id=uuid4(),
        locator="package/example.py",
        data=source.encode(),
    )


def test_parser_preserves_lexical_declarations_and_unresolved_imports() -> None:
    result = parse(
        "from ..services import auth as service\n"
        "@decorator( # comment\n"
        "    value='a#b')\n"
        "class Service:\n"
        "    async def run(self, /, value: list[str] = ['x'], *items, "
        "enabled: bool = True, **options) -> str:\n"
        "        import os as operating_system\n"
        "        def nested():\n"
        "            return None\n"
        "        return 'ok'\n"
    )
    assert result.outcome == "parsed"
    assert [(item.kind, item.qualified_name, item.is_async) for item in result.symbols] == [
        ("class", "Service", False),
        ("method", "Service.run", True),
        ("function", "Service.run.nested", False),
    ]
    method = result.symbols[1]
    assert method.signature is not None
    assert [item.kind for item in method.signature.parameters] == [
        "positional_only",
        "positional_or_keyword",
        "variadic_positional",
        "keyword_only",
        "variadic_keyword",
    ]
    assert method.decorators == ()
    assert result.symbols[0].decorators[0].expression.text == "decorator(value='a#b')"
    assert [
        (item.form, item.from_module, item.name, item.alias, item.level) for item in result.imports
    ] == [
        ("from", "services", "auth", "service", 2),
        ("import", None, "os", "operating_system", 0),
    ]


def test_parser_omits_lambda_fragment_and_never_returns_function_body() -> None:
    result = parse('def fn(value=lambda: secret()):\n    "docstring"\n    return secret()\n')
    assert result.outcome == "parsed"
    signature = result.symbols[0].signature
    assert signature is not None
    default = signature.parameters[0].default
    assert default is not None and default.text is None and default.omitted
    assert all(
        "secret" not in (item.expression.text or "") for item in result.symbols[0].decorators
    )


def test_parser_reports_malformed_source_without_echoing_it() -> None:
    result = parse("def broken(:\n    secret = 'do not echo'\n")
    assert result.outcome == "failed"
    assert result.diagnostics[0].code == "SYNTAX_ERROR"
    assert "secret" not in result.diagnostics[0].message
