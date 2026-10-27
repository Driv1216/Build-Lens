"""LD-1: LangChain community file loaders, run ONLY against generated lab sandboxes."""

from __future__ import annotations

import time
import warnings
from pathlib import Path
from typing import Any

from buildlens.domain.lab import LabError
from buildlens.infrastructure.lab.synthetic import require_sandbox
from buildlens.infrastructure.lab.workers.common.loader_report import describe
from buildlens.infrastructure.lab.workers.common.runtime import WorkerSession

VARIANTS = ("directory_default", "directory_hidden", "generic_language")


def load_directory(session: WorkerSession, params: dict[str, Any]) -> dict[str, Any]:
    root = require_sandbox(Path(params["root"]), str(params["token"]), Path(params["area"]))
    variant = str(params["variant"])
    if variant not in VARIANTS:
        raise LabError("LAB_LOADER_VARIANT_UNKNOWN", variant)
    markers = {"canary": str(params["canary"]), "bait": str(params["bait"])}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from langchain_community.document_loaders import DirectoryLoader, TextLoader

        started = time.perf_counter()
        errors: list[str] = []
        try:
            if variant == "generic_language":
                from langchain_community.document_loaders.generic import GenericLoader
                from langchain_community.document_loaders.parsers import LanguageParser

                loader: Any = GenericLoader.from_filesystem(
                    str(root), glob="**/*", parser=LanguageParser(), show_progress=False
                )
            else:
                loader = DirectoryLoader(
                    str(root),
                    glob="**/*",
                    loader_cls=TextLoader,
                    silent_errors=True,
                    recursive=True,
                    load_hidden=variant == "directory_hidden",
                )
            documents = loader.load()
        except Exception as error:  # report, never hide, a loader failure
            documents = []
            errors.append(type(error).__name__)
        elapsed = (time.perf_counter() - started) * 1000
    files = [
        describe(
            str(d.metadata.get("source", "")), str(root), d.page_content, dict(d.metadata), markers
        )
        for d in documents
    ]
    return {
        "framework": "langchain",
        "variant": variant,
        "loader": "GenericLoader+LanguageParser"
        if variant == "generic_language"
        else "DirectoryLoader+TextLoader",
        "documents": len(documents),
        "files": files,
        "errors": errors,
        "warnings": sorted(
            {str(w.category.__name__) + ": " + str(w.message)[:160] for w in caught}
        ),
        "elapsed_ms": round(elapsed, 2),
    }
