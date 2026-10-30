"""Stage 14 Milestone D2: fixture scoring against designed oracles, and the blind API packet."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, update

from buildlens.application.architecture import ArchitectureService
from buildlens.application.architecture_evaluation import (
    ArchitectureEvaluationService,
    EvaluationError,
    score_fixture_units,
)
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from buildlens.infrastructure.persistence.models import ArchitectureEvidenceRecord, ChunkRecord
from tests.integration.test_architecture_api_matching import FILES as API_FILES
from tests.semantic_support import REPOSITORY, CatalogStack, write
from tests.unit.test_architecture_evaluation import TREE, approved

pytestmark = pytest.mark.integration

DESIGNED = Path(__file__).resolve().parents[1] / "architecture_fixtures"
ALIAS_FILES = {
    "pyproject.toml": '[project]\nname = "falias"\n',
    "app/__init__.py": "",
    "app/main.py": (
        "from fastapi import FastAPI\n\nfrom app.routes import router\n\n"
        "app = FastAPI()\napp.include_router(router, prefix='/api/v1')\n"
    ),
    "app/routes.py": (
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        "@router.get('/users')\ndef list_users():\n    return []\n"
    ),
    "web/package.json": json.dumps({"name": "web", "dependencies": {"react": "^18", "zod": "^3"}}),
    # JSONC with comments, trailing commas, extends and paths: never read by Stage 14.
    "web/tsconfig.base.json": '{\n  // shared\n  "compilerOptions": {"strict": true,},\n}\n',
    "web/tsconfig.json": (
        '{\n  "extends": "./tsconfig.base.json", // base\n  "compilerOptions": {\n'
        '    "baseUrl": ".",\n    "paths": {"@/*": ["src/*"],},\n  },\n}\n'
    ),
    "web/src/lib/client.ts": (
        "const BASE = 'http://127.0.0.1:8000/api/v1'\n"
        "export function request(path: string, init: RequestInit = {}) {\n"
        "  return fetch(`${BASE}${path}`, { ...init })\n}\n"
    ),
    "web/src/lib/format.ts": "export const format = (value: string) => value.trim()\n",
    "web/src/api/users.ts": (
        "import { request } from '@/lib/client'\nexport const listUsers = () => request('/users')\n"
    ),
    "web/src/pages/Users.tsx": (
        "import { listUsers } from '@/api/users'\nimport { format } from '@/lib/format'\n"
        "import { useState } from 'react'\n"
        "export function Users() { const [v] = useState(format('x')); listUsers(); "
        "return <div>{v}</div> }\n"
    ),
    "web/src/main.tsx": (
        "import { Users } from '@/pages/Users'\nimport { format } from './lib/format'\n"
        "export const root = () => <Users />\nexport const label = format(' x ')\n"
    ),
}


def _built(engine: Engine, root: Path, files: dict[str, str], name: str) -> Any:
    write(root, files)
    stack = CatalogStack(engine, root, name)
    snapshot = stack.prepared()
    stack.catalog.build(stack.project.id, snapshot.id)
    store = SqlAlchemyArchitectureStore(stack.factory)
    service = ArchitectureService(store, stack.catalog)
    service.build(stack.project.id, snapshot.id)
    return stack, snapshot, service, ArchitectureEvaluationService(service, store)


def _designed(name: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((DESIGNED / name).read_text())
    return value


def test_f_mod_meets_its_designed_oracle(pg_engine: Engine, tmp_path: Path) -> None:
    stack, snapshot, _, evaluation = _built(pg_engine, tmp_path, REPOSITORY, "F-mod eval")
    system = evaluation.module_units(stack.project.id, snapshot.id, None, TREE)
    result = score_fixture_units(approved(), _designed("f_mod.units.json"), system)
    assert result["overall"] == "passed", result["criteria"]
    assert result["summary"]["outcomes"]["emitted_correct"] == 10
    assert result["summary"]["outcomes"]["correct_abstention"] == 1  # the alias import
    assert result["undesigned_system_units"] == []


def test_f_alias_abstains_on_every_alias_and_gates_absence(
    pg_engine: Engine, tmp_path: Path
) -> None:
    stack, snapshot, service, evaluation = _built(pg_engine, tmp_path, ALIAS_FILES, "F-alias")
    system = evaluation.module_units(stack.project.id, snapshot.id, None, TREE)
    result = score_fixture_units(approved(), _designed("f_alias.units.json"), system)
    assert result["overall"] == "passed", result["criteria"]
    aliases = [unit for unit in result["units"] if unit.get("alias")]
    assert len(aliases) == 4 and {unit["outcome"] for unit in aliases} == {"correct_abstention"}
    run, _, loaded = service.graph(stack.project.id, snapshot.id, None)
    # No API candidate through the alias-bound wrapper; its binding is stated as unknown.
    assert not [edge for edge in loaded.edges.values() if edge.kind == "calls_endpoint"]
    codes = {diagnostic.code for diagnostic in loaded.stored.diagnostics}
    assert "WRAPPER_BINDING_UNKNOWN" in codes
    # `zod` is declared and not imported, but alias imports block the absence claim.
    assert "DECLARED_NOT_OBSERVED_IMPORTED" not in codes
    assert "ABSENCE_NOT_DETERMINABLE" in codes
    assert run.coverage["alias"]["statements"] == 4
    # Stage 14 never cites a tsconfig chunk (C-6): no evidence row refers to one.
    with stack.factory() as session:
        refs = session.scalars(
            select(ArchitectureEvidenceRecord.source_refs).where(
                ArchitectureEvidenceRecord.architecture_run_id == run.id
            )
        ).all()
    cited = {str(ref.get("locator")) for item in refs for ref in item}
    assert "web/src/api/users.ts" in cited
    assert not {locator for locator in cited if "tsconfig" in locator}


def test_api_packet_is_blind_verified_and_tamper_evident(pg_engine: Engine, tmp_path: Path) -> None:
    stack, snapshot, _, evaluation = _built(pg_engine, tmp_path, API_FILES, "F-api packet")
    packet, sealed = evaluation.api_packet(stack.project.id, snapshot.id, None, approved(), TREE)
    assert packet["sites"] and packet["endpoints"]
    blind = json.dumps({k: v for k, v in packet.items() if k != "instructions"})
    for word in ("conditional_candidate", "candidates", "destination", "branches", "outcome"):
        assert word not in blind
    assert set(sealed["sites"]) == {site["unit"] for site in packet["sites"]}
    # Router construction and mounts are selected by text from stored chunks.
    assert any("include_router(" in chunk["content"] for chunk in packet["router_excerpts"])
    assert all(endpoint["excerpt"] for endpoint in packet["endpoints"])
    with pytest.raises(EvaluationError, match="SYSTEM_CORPUS_MISMATCH"):
        evaluation.api_packet(stack.project.id, snapshot.id, None, approved(), "b" * 64)
    with stack.factory.begin() as session:
        session.execute(update(ChunkRecord).values(content=ChunkRecord.content + " "))
    with pytest.raises(EvaluationError, match="CHUNK_HASH_MISMATCH"):
        evaluation.api_packet(stack.project.id, snapshot.id, None, approved(), TREE)
