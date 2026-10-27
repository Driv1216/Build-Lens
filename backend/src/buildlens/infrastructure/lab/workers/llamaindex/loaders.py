"""LD-1: LlamaIndex SimpleDirectoryReader, run ONLY against generated lab sandboxes."""

from __future__ import annotations

import time
import warnings
from pathlib import Path
from typing import Any

from llama_index.core import SimpleDirectoryReader

from buildlens.domain.lab import LabError
from buildlens.infrastructure.lab.synthetic import require_sandbox
from buildlens.infrastructure.lab.workers.common.loader_report import describe
from buildlens.infrastructure.lab.workers.common.runtime import WorkerSession

VARIANTS = ("reader_default", "reader_hidden")


def load_directory(session: WorkerSession, params: dict[str, Any]) -> dict[str, Any]:
    root = require_sandbox(Path(params["root"]), str(params["token"]), Path(params["area"]))
    variant = str(params["variant"])
    if variant not in VARIANTS:
        raise LabError("LAB_LOADER_VARIANT_UNKNOWN", variant)
    markers = {"canary": str(params["canary"]), "bait": str(params["bait"])}
    errors: list[str] = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        started = time.perf_counter()
        try:
            reader = SimpleDirectoryReader(
                input_dir=str(root),
                recursive=True,
                exclude_hidden=variant == "reader_default",
                raise_on_error=False,
            )
            documents = reader.load_data(show_progress=False)
        except Exception as error:
            documents = []
            errors.append(type(error).__name__)
        elapsed = (time.perf_counter() - started) * 1000
    files = [
        describe(
            str(d.metadata.get("file_path", "")),
            str(root),
            d.get_content(),
            dict(d.metadata),
            markers,
        )
        for d in documents
    ]
    return {
        "framework": "llamaindex",
        "variant": variant,
        "loader": "SimpleDirectoryReader",
        "documents": len(documents),
        "files": files,
        "errors": errors,
        "warnings": sorted(
            {str(w.category.__name__) + ": " + str(w.message)[:160] for w in caught}
        ),
        "elapsed_ms": round(elapsed, 2),
    }
