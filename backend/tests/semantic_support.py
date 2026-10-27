"""Shared Stage 13 catalog fixtures: a synthetic Python + TSX repository and a real stack."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from buildlens.application.fingerprinting import SnapshotFingerprinter
from buildlens.application.processing import ProcessingService
from buildlens.application.semantic_catalog import SemanticCatalogService
from buildlens.application.snapshots import SnapshotService
from buildlens.application.structural import StructuralAnalysisService
from buildlens.domain.project_factory import new_project
from buildlens.infrastructure.discovery.filesystem import LocalRepositoryInspector
from buildlens.infrastructure.persistence.database import create_session_factory_from_engine
from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore
from buildlens.infrastructure.persistence.repositories import SqlAlchemyProjectRepository
from buildlens.infrastructure.persistence.semantic import SqlAlchemySemanticCatalogStore
from buildlens.infrastructure.persistence.snapshots import SqlAlchemySnapshotStore
from buildlens.infrastructure.persistence.structural import SqlAlchemyStructuralAnalysisStore
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
from buildlens.infrastructure.structural.worker import SupervisedArtifactAnalyzer

REPOSITORY = {
    "pyproject.toml": '[project]\nname = "demo"\n',
    "app/__init__.py": "",
    "app/db.py": (
        "from sqlalchemy.orm import DeclarativeBase, Mapped\n\n\n"
        "class Base(DeclarativeBase):\n    pass\n\n\n"
        "class User(Base):\n"
        '    __tablename__ = "users"\n'
        "    id: Mapped[int]\n\n\n"
        "def session():\n    return None\n"
    ),
    "app/api.py": (
        "from fastapi import APIRouter\n\n"
        "from .db import User, session\n\n"
        "router = APIRouter()\n\n\n"
        '@router.get("/users/{user_id}")\n'
        "async def get_user(user_id: int) -> dict:\n"
        "    record = session()\n"
        "    return load(record, user_id)\n\n\n"
        "def load(record, user_id):\n"
        '    """Load one user."""\n'
        "    if record is None:\n"
        "        raise LookupError(user_id)\n"
        '    return {"id": user_id}\n\n\n'
        "class Service(User):\n"
        "    def run(self):\n"
        "        return self.helper()\n\n"
        "    def helper(self):\n"
        "        return eval('1')\n"
    ),
    "migrations/versions/0001_users.py": (
        'from alembic import op\n\n\ndef upgrade():\n    op.create_table("users")\n'
    ),
    "web/hooks.ts": (
        'import { useState } from "react";\n\n'
        "export function useToggle(initial: boolean): [boolean, () => void] {\n"
        "  const [on, setOn] = useState(initial);\n"
        "  return [on, () => setOn(!on)];\n"
        "}\n"
    ),
    "web/Button.tsx": (
        'import { memo } from "react";\n'
        'import { useToggle } from "./hooks";\n\n'
        "export const Button = memo(({ label }: { label: string }) => {\n"
        "  const [on] = useToggle(false);\n"
        "  return <button>{label}{on}</button>;\n"
        "});\n\n"
        "export default function Page() {\n"
        '  return <div><Button label="x" /></div>;\n'
        "}\n"
    ),
    "web/broken.ts": "export function ok(): number { return 1; }\nconst = ;\n",
    "web/alias.ts": 'import { x } from "@/lib/x";\nimport lodash from "lodash";\n',
    "notes.md": "# Notes\n\nText.\n",
}


class CatalogStack:
    def __init__(self, engine: Engine, root: Path, name: str = "Stage 13 B") -> None:
        self.factory = create_session_factory_from_engine(engine)
        with self.factory() as session:
            self.project = SqlAlchemyProjectRepository(session).add(
                new_project(name=name, root_path=str(root))
            )
        self.snapshots = SnapshotService(
            SqlAlchemySnapshotStore(self.factory),
            LocalRepositoryInspector(),
            SnapshotFingerprinter(reader=SupervisedSourceReader()),
        )
        self.structure_store = SqlAlchemyStructuralAnalysisStore(self.factory)
        self.structural = StructuralAnalysisService(
            self.structure_store, SupervisedArtifactAnalyzer()
        )
        self.processing = ProcessingService(
            SqlAlchemyProcessingStore(self.factory), reader=SupervisedSourceReader()
        )
        self.store = SqlAlchemySemanticCatalogStore(self.factory)
        self.catalog = SemanticCatalogService(self.store, self.structure_store, self.processing)

    def prepared(self) -> Any:
        snapshot = self.snapshots.create(self.project.id)
        self.structural.analyse(self.project.id, snapshot.id)
        self.processing.process(self.project.id, snapshot.id)
        return snapshot


def write(root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
