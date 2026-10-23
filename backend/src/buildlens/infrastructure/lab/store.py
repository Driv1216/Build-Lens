"""Local laboratory state under the existing excluded `.buildlens-eval/` boundary.

Layout (only `experiments/` is version-controlled):
  experiments/<name>.json   validated experiment configurations
  indexes/<index_id>/       framework stores, documents.json, index.json (sensitive: 0700)
  runs/<run_id>/            benchmark manifests, per-case results, scores, reports
  sandboxes/<id>/           generated synthetic repositories for loader experiments
  workers/<framework>/      private HOME/TMPDIR of each worker process
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from buildlens.domain.lab import ExperimentConfig, LabError, parse_config
from buildlens.infrastructure.evaluation.local import atomic_write, read_json

AREAS = ("experiments", "indexes", "runs", "sandboxes", "workers", "authorizations")


def default_lab_root() -> Path:
    backend = Path(__file__).resolve().parents[4]
    return backend.parent / ".buildlens-eval" / "lab"


class LabStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        if self.root.name != "lab" or self.root.parent.name != ".buildlens-eval":
            raise LabError("LAB_ROOT_INVALID", "Lab state must live in .buildlens-eval/lab")
        for area in AREAS:
            path = self.root / area
            path.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise LabError("LAB_PATH_SYMLINK")
            if area != "experiments":
                os.chmod(path, 0o700)

    @property
    def sandbox_base(self) -> Path:
        """Private per-user base for generated sandboxes.

        Sandbox trees cannot live under `.buildlens-eval/` because the BuildLens inspector
        (correctly) refuses roots inside excluded directories. The base is owner-only and
        rejected if it is a symlink or owned by someone else.
        """
        base = Path(tempfile.gettempdir()) / f"buildlens-lab-sandboxes-{os.getuid()}"
        try:
            base.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = os.lstat(base)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise LabError("LAB_SANDBOX_BASE_UNSAFE")
        return base.resolve()

    def area(self, name: str) -> Path:
        if name not in AREAS:
            raise LabError("LAB_AREA_INVALID")
        return self.root / name

    def inside(self, path: Path, area: str) -> Path:
        boundary = self.area(area)
        resolved = path.expanduser().resolve()
        if not resolved.is_relative_to(boundary) or resolved == boundary:
            raise LabError("LAB_PATH_OUTSIDE_BOUNDARY")
        return resolved

    # ---------------------------------------------------------------- experiments

    def save_experiment(self, config: ExperimentConfig) -> Path:
        path = self.area("experiments") / f"{config.name}.json"
        payload = {**config.as_dict(), "digest": config.digest}
        if path.exists():
            existing = read_json(path)
            if existing.get("digest") == config.digest:
                return path
        atomic_write(path, payload, replace=True)
        return path

    def experiments(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.area("experiments").glob("*.json")):
            raw = read_json(path)
            raw.pop("digest", None)
            try:
                config = parse_config(raw)
                result.append({**config.as_dict(), "digest": config.digest, "valid": True})
            except LabError as error:
                result.append({"name": path.stem, "valid": False, "error": error.code})
        return result

    def experiment(self, name: str) -> ExperimentConfig:
        path = self.inside(self.area("experiments") / f"{name}.json", "experiments")
        if not path.is_file():
            raise LabError("LAB_EXPERIMENT_NOT_FOUND")
        raw = read_json(path)
        raw.pop("digest", None)
        return parse_config(raw)

    # ---------------------------------------------------------------- indexes

    def index_dir(self, index_id: str, create: bool = False) -> Path:
        if not index_id.isalnum() or len(index_id) != 64:
            raise LabError("LAB_INDEX_ID_INVALID")
        path = self.area("indexes") / index_id
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        return path

    def write_index(self, index_id: str, name: str, value: Any) -> None:
        atomic_write(self.index_dir(index_id) / name, value, replace=True)

    def read_index(self, index_id: str, name: str) -> Any:
        path = self.index_dir(index_id) / name
        if not path.is_file():
            raise LabError("LAB_INDEX_NOT_FOUND")
        return read_json(path)

    def index_exists(self, index_id: str) -> bool:
        return (self.index_dir(index_id) / "index.json").is_file()

    def indexes(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.area("indexes").iterdir()):
            manifest = path / "index.json"
            if manifest.is_file():
                value = read_json(manifest)
                result.append(
                    {
                        k: value.get(k)
                        for k in (
                            "index_id",
                            "config",
                            "built_at",
                            "nodes",
                            "embedding",
                            "reuse_verification",
                            "corpus_digest",
                        )
                    }
                )
        return result

    def remove_index(self, index_id: str) -> None:
        shutil.rmtree(self.index_dir(index_id), ignore_errors=True)

    # ---------------------------------------------------------------- runs

    def run_dir(self, run_id: str) -> Path:
        if not run_id.replace("-", "").isalnum() or len(run_id) > 80:
            raise LabError("LAB_RUN_ID_INVALID")
        return self.area("runs") / run_id

    def runs(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.area("runs").iterdir(), reverse=True):
            manifest = path / "manifest.json"
            if manifest.is_file():
                value = read_json(manifest)
                result.append(
                    {
                        k: value.get(k)
                        for k in (
                            "run_id",
                            "kind",
                            "status",
                            "started_at",
                            "finished_at",
                            "pipelines",
                            "counts",
                            "dataset_id",
                        )
                    }
                )
        return result

    def read_run(self, run_id: str, name: str = "manifest.json") -> Any:
        path = self.inside(self.run_dir(run_id) / name, "runs")
        if not path.is_file():
            raise LabError("LAB_RUN_NOT_FOUND")
        return read_json(path)


def write_text(path: Path, text: str) -> None:
    atomic_write(path, text, replace=True, text=True)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
