"""LD-2: unsupported-language retrieval on a generated synthetic sandbox (Tier D, constructed).

Creates a DISPOSABLE migrated database on the given admin server, onboards the sandbox
through the real BuildLens API (real local models), runs native and framework pipelines,
scores spans known by construction, then drops the database. Never touches other DBs.

    PYTHONPATH=src python scripts/lab_ld2.py --admin-url URL --output path/ld2.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from alembic import command
from buildlens.api.main import create_app
from buildlens.application.lab_evaluation import constructed_coverage
from buildlens.core.config import Settings, get_settings
from buildlens.domain.lab import parse_config
from buildlens.infrastructure.evaluation.local import atomic_write
from buildlens.infrastructure.lab.service import build_lab_service
from buildlens.infrastructure.lab.store import LabStore, default_lab_root
from buildlens.infrastructure.lab.synthetic import constructed_dataset, create_sandbox

BACKEND = Path(__file__).resolve().parents[1]
PIPELINES = {
    "native-hybrid": {"pipeline": "native.context", "params": {"mode": "hybrid"}},
    "lc-ingest-language": {
        "pipeline": "langchain.ingest",
        "corpus": {"mode": "artifact_sources", "include_unsupported": True},
        "params": {"chunk_size": 400, "synthesis": False},
    },
    "li-ingest-sentence": {
        "pipeline": "llamaindex.ingest",
        "corpus": {"mode": "artifact_sources", "include_unsupported": True},
        "params": {"chunk_size": 128, "response_mode": "no_text"},
    },
    "li-ingest-code": {
        "pipeline": "llamaindex.ingest",
        "corpus": {"mode": "artifact_sources", "include_unsupported": True},
        "params": {"chunk_size": 128, "parser": "code", "response_mode": "no_text"},
    },
    "li-sentence-window": {
        "pipeline": "llamaindex.sentence_window",
        "corpus": {"mode": "artifact_sources", "include_unsupported": True},
        "params": {"window_size": 1},
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=12)
    args = parser.parse_args()
    admin_url = make_url(args.admin_url)
    name = "buildlens_ld2_" + uuid4().hex[:12]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    url = admin_url.set(database=name).render_as_string(hide_password=False)
    try:
        import os

        os.environ["BUILDLENS_DATABASE_URL"] = url
        get_settings.cache_clear()
        config = Config(str(BACKEND / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND / "alembic"))
        command.upgrade(config, "head")
        store = LabStore(default_lab_root())
        sandbox = create_sandbox(store.sandbox_base, args.seed)
        dataset = constructed_dataset(sandbox.root)
        app = create_app(Settings(database_url=url))
        results: dict[str, object] = {}
        with TestClient(app) as client:
            project = UUID(
                client.post(
                    "/api/v1/projects",
                    json={"name": "LD-2 sandbox", "root_path": str(sandbox.root)},
                ).json()["id"]
            )
            snapshot = UUID(
                client.post(f"/api/v1/projects/{project}/snapshots", json={}).json()["id"]
            )
            scope = f"/api/v1/projects/{project}/snapshots/{snapshot}"
            for operation in ("structure", "processing", "embeddings"):
                response = client.post(scope + "/" + operation, json={})
                if response.status_code != 200:
                    print(operation, response.text, file=sys.stderr)
                    return 1
            service = build_lab_service(app.state)
            try:
                for label, raw in PIPELINES.items():
                    cfg = parse_config({"name": f"ld2-{label}", **raw})
                    by_case = {}
                    build = (
                        service.build(cfg, project, snapshot)
                        if cfg.pipeline != "native.context"
                        else {}
                    )
                    for case in dataset["cases"]:
                        by_case[case["id"]] = service.query(
                            cfg, project, snapshot, case["question"]
                        )
                    scored = constructed_coverage(dataset, by_case)
                    results[label] = {
                        "config": cfg.as_dict(),
                        "gateway": build.get("gateway"),
                        "nodes": build.get("nodes"),
                        "embedding": build.get("embedding"),
                        "anchor_methods": (build.get("worker_manifest") or {}).get(
                            "anchor_methods"
                        ),
                        "coverage": scored,
                    }
                    print(
                        label,
                        {k: v["covered@5"] for k, v in scored["cases"].items()},
                        file=sys.stderr,
                    )
            finally:
                app.state.lab_workers.close()
        summary = {}
        for label, value in results.items():
            cases = value["coverage"]["cases"]  # type: ignore[index]
            groups: dict[str, list[bool]] = {}
            for case in dataset["cases"]:
                group = (
                    "processed-by-buildlens"
                    if case["language"] in {"py", "md", "yaml", "json"}
                    else "unsupported-by-buildlens"
                )
                groups.setdefault(group, []).append(bool(cases[case["id"]]["covered@5"]))
            summary[label] = {g: f"{sum(v)}/{len(v)}" for g, v in groups.items()}
        atomic_write(
            args.output,
            {
                "version": "stage12.ld2.v1",
                "tier": "constructed (not human-approved)",
                "sandbox": sandbox.manifest,
                "dataset": dataset,
                "summary_covered@5": summary,
                "results": results,
            },
            replace=True,
        )
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
