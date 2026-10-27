"""Stage 13 Milestone C fixtures: a counting loopback model server and a generation stack."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from buildlens.application.semantic_generation import GenerationSettings, SemanticGenerationService
from buildlens.infrastructure.generation.semantic_common import UNTRUSTED_PREFIX
from buildlens.infrastructure.generation.semantic_local import LoopbackGenerator
from buildlens.infrastructure.persistence.semantic_generation import SqlAlchemyGenerationStore
from tests.semantic_support import CatalogStack

Reply = Callable[[dict[str, Any]], tuple[int, dict[str, Any] | None, dict[str, str]]]


def package_of(request: dict[str, Any]) -> dict[str, Any]:
    content = request["messages"][1]["content"]
    assert content.startswith(UNTRUSTED_PREFIX)
    value: dict[str, Any] = json.loads(content[len(UNTRUSTED_PREFIX) :])
    return value


def grounded_claims(package: dict[str, Any]) -> dict[str, Any]:
    """A well-behaved model: quotes that occur exactly once, identifiers that are grounded."""
    entity = package["entity"]
    facts = [item for item in package["facts"] if item["scope"] == "own"]
    claims: list[dict[str, Any]] = []
    signature = next((item for item in facts if ": " in item["text"]), None)
    if signature is not None:
        quote = signature["text"][:60]
        if signature["text"].count(quote) == 1:
            claims.append(
                {
                    "category": "purpose",
                    "text": f"`{entity['name']}` is declared in `{entity['locator']}`.",
                    "evidence": [{"id": signature["id"], "quote": quote}],
                    "assertion": None,
                }
            )
    return {"claims": claims, "unknowns": [{"text": "Callers are not shown in this package."}]}


def completion(content: dict[str, Any], model: str = "stub-model-1") -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


class StubModelServer:
    """OpenAI-compatible chat-completions stub on 127.0.0.1 that counts every request."""

    def __init__(self, reply: Reply | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.reply: Reply = reply or (
            lambda request: (200, completion(grounded_claims(package_of(request))), {})
        )
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                request = json.loads(body)
                owner.requests.append(request)
                owner.paths.append(self.path)
                status, payload, headers = owner.reply(request)
                data = json.dumps(payload).encode() if payload is not None else b"{}"
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                owner.paths.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def generation_service(
    stack: CatalogStack,
    server: StubModelServer,
    *,
    model: str = "stub-model",
    settings: GenerationSettings | None = None,
) -> SemanticGenerationService:
    values = settings or GenerationSettings(
        enabled=True, request_timeout_seconds=10, deadline_seconds=120
    )
    return SemanticGenerationService(
        stack.catalog,
        stack.store,
        SqlAlchemyGenerationStore(stack.factory),
        lambda: LoopbackGenerator(server.url, model, values.max_output_tokens),
        values,
    )


def standalone_service(
    database_url: str, server_url: str, model: str = "stub-model", **settings: Any
) -> SemanticGenerationService:
    """A service over an existing database (used by subprocess tests); creates no project."""
    from sqlalchemy import create_engine

    from buildlens.application.processing import ProcessingService
    from buildlens.application.semantic_catalog import SemanticCatalogService
    from buildlens.infrastructure.persistence.database import create_session_factory_from_engine
    from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore
    from buildlens.infrastructure.persistence.semantic import SqlAlchemySemanticCatalogStore
    from buildlens.infrastructure.persistence.structural import SqlAlchemyStructuralAnalysisStore
    from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader

    factory = create_session_factory_from_engine(create_engine(database_url))
    store = SqlAlchemySemanticCatalogStore(factory)
    catalog = SemanticCatalogService(
        store,
        SqlAlchemyStructuralAnalysisStore(factory),
        ProcessingService(SqlAlchemyProcessingStore(factory), reader=SupervisedSourceReader()),
    )
    values = GenerationSettings(
        **{"enabled": True, "request_timeout_seconds": 10, "deadline_seconds": 120, **settings}
    )
    return SemanticGenerationService(
        catalog,
        store,
        SqlAlchemyGenerationStore(factory),
        lambda: LoopbackGenerator(server_url, model, values.max_output_tokens),
        values,
    )


EXECUTE_SCRIPT = """
import json, sys
from uuid import UUID
from tests.generation_support import standalone_service

def main():
    url, server, project, snapshot, plan, digest = sys.argv[1:7]
    service = standalone_service(url, server)
    try:
        run = service.execute(UUID(project), UUID(snapshot), UUID(plan), digest, True)
        print(json.dumps({"state": run.state}))
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}))

if __name__ == "__main__":
    main()
"""
