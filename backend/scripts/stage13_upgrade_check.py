"""Stage 13 invariants M1/M2 against genuine pre-Stage-13 evidence.

1. Export the pre-Stage-13 source (default ``816b136``) with ``git archive``.
2. With that *old* code: migrate a disposable database to 0009 and populate it through the
   normal snapshot -> structure -> processing operations on a synthetic Python+TS repository.
3. Record row counts and md5 digests of every pre-existing table over its original columns.
4. With the *new* code: upgrade to head, recompute the digests (M1), check that the old runs
   are still current (M2), then analyse a new snapshot and check identities are preserved and
   the additive passes ran.

Usage (from backend/, disposable admin URL required; it never touches other databases):
    BUILDLENS_TEST_ADMIN_URL=postgresql+psycopg://postgres@127.0.0.1:55439/postgres \\
      .venv/bin/python scripts/stage13_upgrade_check.py --report /path/report.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

BACKEND = Path(__file__).resolve().parents[1]

POPULATE = r"""
import json, sys
from pathlib import Path
from sqlalchemy import create_engine
from buildlens.application.fingerprinting import SnapshotFingerprinter
from buildlens.application.processing import ProcessingService
from buildlens.application.snapshots import SnapshotService
from buildlens.application.structural import StructuralAnalysisService
from buildlens.domain.project_factory import new_project
from buildlens.infrastructure.discovery.filesystem import LocalRepositoryInspector
from buildlens.infrastructure.persistence.database import create_session_factory_from_engine
from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore
from buildlens.infrastructure.persistence.repositories import SqlAlchemyProjectRepository
from buildlens.infrastructure.persistence.snapshots import SqlAlchemySnapshotStore
from buildlens.infrastructure.persistence.structural import SqlAlchemyStructuralAnalysisStore
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
from buildlens.infrastructure.structural.worker import SupervisedPythonAnalyzer

url, root = sys.argv[1], sys.argv[2]
factory = create_session_factory_from_engine(create_engine(url))
with factory() as session:
    project = SqlAlchemyProjectRepository(session).add(new_project(name="Upgrade", root_path=root))
snapshot = SnapshotService(
    SqlAlchemySnapshotStore(factory), LocalRepositoryInspector(),
    SnapshotFingerprinter(reader=SupervisedSourceReader()),
).create(project.id)
analysis = StructuralAnalysisService(
    SqlAlchemyStructuralAnalysisStore(factory), SupervisedPythonAnalyzer()
).analyse(project.id, snapshot.id)
processing = ProcessingService(SqlAlchemyProcessingStore(factory), reader=SupervisedSourceReader())
run = processing.process(project.id, snapshot.id)
print(json.dumps({
    "project": str(project.id), "snapshot": str(snapshot.id), "analysis": str(analysis.id),
    "processing": str(run.id),
    "identities": {s.qualified_name: str(s.stable_identity_id)
                   for a in analysis.artifact_analyses for s in a.symbols},
}))
"""

REPOSITORY = {
    "pkg/service.py": "class Service:\n    def load(self, key):\n        return key\n\n"
    "def load():\n    pass\n\ndef load():\n    pass\n",
    "web/app.tsx": "export function App() { return <main/>; }\n",
    "README.md": "# Upgrade\n\nText.\n",
}


def _alembic(code_root: Path, url: str, target: str, python_path: Path) -> None:
    env = {**os.environ, "BUILDLENS_DATABASE_URL": url, "PYTHONPATH": str(python_path)}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=code_root,
        env=env,
        check=True,
        capture_output=True,
    )


def _digests(
    url: str, columns: dict[str, list[str]] | None = None
) -> tuple[dict[str, object], dict[str, list[str]]]:
    engine = create_engine(url)
    with engine.connect() as connection:
        if columns is None:
            columns = {}
            for table in connection.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1"
                )
            ):
                columns[table] = list(
                    connection.scalars(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name=:t "
                            "ORDER BY ordinal_position"
                        ),
                        {"t": table},
                    )
                )
        values: dict[str, object] = {}
        for table, names in columns.items():
            row = ", ".join(f'"{name}"' for name in names)
            values[table] = (
                connection.execute(
                    text(
                        "SELECT count(*), "
                        "md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), '')) "
                        f'FROM (SELECT ROW({row}) AS r FROM "{table}") s'
                    )
                )
                .one()
                ._asdict()
            )
    engine.dispose()
    return values, columns


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="816b136")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    admin_url = os.environ["BUILDLENS_TEST_ADMIN_URL"]
    name = "buildlens_stage13_upgrade_" + uuid4().hex
    database_url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    report: dict[str, object] = {"base_commit": args.base, "database": name}
    try:
        with tempfile.TemporaryDirectory() as scratch:
            old = Path(scratch) / "old"
            old.mkdir()
            archive = Path(scratch) / "old.tar"
            subprocess.run(
                ["git", "archive", "--format=tar", "-o", str(archive), args.base, "backend"],
                cwd=BACKEND.parent,
                check=True,
            )
            with tarfile.open(archive) as handle:
                handle.extractall(old, filter="data")
            old_backend = old / "backend"
            repository = Path(scratch) / "repository"
            for locator, body in REPOSITORY.items():
                target = repository / locator
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body)

            _alembic(old_backend, database_url, "head", old_backend / "src")
            populated = subprocess.run(
                [sys.executable, "-c", POPULATE, database_url, str(repository)],
                cwd=old_backend,
                env={
                    **os.environ,
                    "BUILDLENS_DATABASE_URL": database_url,
                    "PYTHONPATH": str(old_backend / "src"),
                },
                check=True,
                capture_output=True,
                text=True,
            )
            old_state = json.loads(populated.stdout.strip().splitlines()[-1])
            before, columns = _digests(database_url)

            _alembic(BACKEND, database_url, "head", BACKEND / "src")
            after, _ = _digests(database_url, columns)
            report["m1_unchanged_tables"] = sorted(t for t in columns if before[t] == after[t])
            report["m1_changed_tables"] = sorted(t for t in columns if before[t] != after[t])

            os.environ["BUILDLENS_DATABASE_URL"] = database_url
            sys.path.insert(0, str(BACKEND / "src"))
            from uuid import UUID

            from buildlens.application.fingerprinting import SnapshotFingerprinter
            from buildlens.application.processing import ProcessingService
            from buildlens.application.snapshots import SnapshotService
            from buildlens.application.structural import StructuralAnalysisService
            from buildlens.infrastructure.discovery.filesystem import LocalRepositoryInspector
            from buildlens.infrastructure.persistence.database import (
                create_session_factory_from_engine,
            )
            from buildlens.infrastructure.persistence.processing import SqlAlchemyProcessingStore
            from buildlens.infrastructure.persistence.snapshots import SqlAlchemySnapshotStore
            from buildlens.infrastructure.persistence.structural import (
                SqlAlchemyStructuralAnalysisStore,
            )
            from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
            from buildlens.infrastructure.structural.worker import SupervisedArtifactAnalyzer

            factory = create_session_factory_from_engine(create_engine(database_url))
            project, snapshot = UUID(old_state["project"]), UUID(old_state["snapshot"])
            structural = StructuralAnalysisService(
                SqlAlchemyStructuralAnalysisStore(factory), SupervisedArtifactAnalyzer()
            )
            processing = ProcessingService(
                SqlAlchemyProcessingStore(factory), reader=SupervisedSourceReader()
            )
            current = structural.latest_analysis(project, snapshot)
            status = processing.status(project, snapshot)
            report["m2_structure_current"] = bool(
                current and str(current.id) == old_state["analysis"]
            )
            report["m2_symbol_reads_current"] = structural.content_run(project, snapshot, None) == (
                current.id if current else None
            )
            report["m2_processing_fresh"] = (
                status.active_freshness == "fresh"
                and str(status.active_run.id if status.active_run else "")
                == old_state["processing"]
            )

            new_snapshot = SnapshotService(
                SqlAlchemySnapshotStore(factory),
                LocalRepositoryInspector(),
                SnapshotFingerprinter(reader=SupervisedSourceReader()),
            ).create(project)
            upgraded = structural.analyse(project, new_snapshot.id)
            identities = {
                s.qualified_name: str(s.stable_identity_id)
                for a in upgraded.artifact_analyses
                for s in a.symbols
                if a.language in {None, "Python"}
            }
            report["python_identities_preserved"] = identities == old_state["identities"]
            report["artifacts_after_upgrade"] = {
                a.locator: {
                    "outcome": a.outcome,
                    "execution_mode": a.execution_mode,
                    "language": a.language,
                    "extractions": [
                        [x.extractor, x.execution_mode, x.outcome] for x in a.extractions
                    ],
                }
                for a in upgraded.artifact_analyses
            }
            processed = processing.process(project, new_snapshot.id)
            report["processing_after_upgrade"] = {
                p.locator: [p.outcome, p.execution_mode, p.fingerprint_scheme]
                for p in processed.artifact_processings
            }
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
    output = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
