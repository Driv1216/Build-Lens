"""LD-1 loader safety diff: framework file loaders vs the BuildLens source boundary.

BuildLens's view is computed with the real inventory inspector, eligibility policy,
processing suffix rule, supervised secure reader, decoder and secret screen, without any
database writes. Framework loaders run in their worker, confined to the generated sandbox.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any

from buildlens.application.processing import _TEXT_EXT
from buildlens.application.source_ports import SourceReadError
from buildlens.domain.eligibility import POLICY_VERSION, exclusion_reason
from buildlens.domain.lab import LabError
from buildlens.domain.source import (
    READER_VERSION,
    ContentRejectedError,
    decode_source,
    screen_content,
)
from buildlens.infrastructure.discovery.filesystem import LocalRepositoryInspector
from buildlens.infrastructure.lab.synthetic import Sandbox
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader

SUPPORTED = {"py", "md", "json", "toml", "yaml", "yml", *_TEXT_EXT}
LOADERS = {
    "langchain": ("directory_default", "directory_hidden", "generic_language"),
    "llamaindex": ("reader_default", "reader_hidden"),
}


def buildlens_view(sandbox: Sandbox) -> dict[str, dict[str, Any]]:
    inspection = LocalRepositoryInspector(timeout_seconds=30).inspect(str(sandbox.root))
    expected = None
    reader = SupervisedSourceReader(timeout=3)
    result: dict[str, dict[str, Any]] = {}
    try:
        for observation in inspection.artifacts:
            meta = observation.metadata
            locator = observation.locator
            row: dict[str, Any] = {"kind": observation.kind, "inventory": meta.inventory_status}
            reasons = list(meta.exclusion_reasons)
            if observation.kind != "file":
                row.update({"outcome": "not_file", "reasons": reasons})
                result[locator] = row
                continue
            if (
                meta.inventory_status != "included"
                or meta.pruned
                or meta.ignore_status != "not_ignored"
                or reasons
                or exclusion_reason(locator, False)
            ):
                reason = reasons or (
                    [meta.ignore_status] if meta.ignore_status != "not_ignored" else []
                )
                reason = reason or [str(exclusion_reason(locator, False) or meta.inventory_status)]
                row.update({"outcome": "excluded", "reasons": reason})
                result[locator] = row
                continue
            suffix = PurePosixPath(locator).suffix.lstrip(".").lower()
            if suffix not in SUPPORTED:
                row.update({"outcome": "unsupported", "reasons": ["UNSUPPORTED_ARTIFACT"]})
                result[locator] = row
                continue
            try:
                raw = reader.read(inspection.canonical_root, locator, 1024 * 1024, expected).data
                text, _ = decode_source(raw, suffix == "py")
                screen_content(text)
                row.update(
                    {
                        "outcome": "processed",
                        "reasons": [],
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
            except SourceReadError as error:
                row.update({"outcome": "read_rejected", "reasons": [error.code]})
            except ContentRejectedError as error:
                row.update({"outcome": "secret_rejected", "reasons": [error.code]})
            except (UnicodeDecodeError, SyntaxError, LookupError):
                row.update({"outcome": "decode_rejected", "reasons": ["DECODE_FAILED"]})
            result[locator] = row
    finally:
        reader.close()
    return result


def loader_diff(pool: Any, sandbox: Sandbox, area: str) -> dict[str, Any]:
    native = buildlens_view(sandbox)
    variants: dict[str, Any] = {}
    for framework, names in LOADERS.items():
        worker = pool.get(framework)
        for variant in names:
            try:
                call = worker.request(
                    "load_directory",
                    {
                        "root": str(sandbox.root),
                        "token": sandbox.token,
                        "area": area,
                        "variant": variant,
                        "canary": sandbox.canary,
                        "bait": sandbox.bait_marker,
                    },
                    timeout_seconds=120,
                )
                variants[f"{framework}.{variant}"] = {
                    **call.value,
                    "guard_denials": call.guard_denials,
                }
            except LabError as error:
                variants[f"{framework}.{variant}"] = {
                    "errors": [error.code],
                    "files": [],
                    "documents": 0,
                }
    paths = sorted(set(native) | {f["path"] for v in variants.values() for f in v["files"]})
    rows = []
    for path in paths:
        row: dict[str, Any] = {
            "path": path,
            "buildlens": native.get(
                path, {"outcome": "pruned_directory", "reasons": ["parent pruned"]}
            ),
        }
        for name, variant in variants.items():
            hits = [f for f in variant["files"] if f["path"] == path]
            row[name] = (
                {
                    "loaded": True,
                    "documents": len(hits),
                    "chars": sum(h["chars"] for h in hits),
                    "contains_canary": any(h["contains_canary"] for h in hits),
                    "contains_bait": any(h["contains_bait"] for h in hits),
                    "contains_private_key_header": any(
                        h["contains_private_key_header"] for h in hits
                    ),
                    "absolute_path_in_metadata": any(h["absolute_path_in_metadata"] for h in hits),
                }
                if hits
                else {"loaded": False}
            )
        rows.append(row)
    summary = {}
    for name, variant in variants.items():
        loaded = [r for r in rows if r[name].get("loaded")]
        summary[name] = {
            "documents": variant.get("documents", 0),
            "files_loaded": len(loaded),
            "loaded_but_buildlens_excluded_or_rejected": sorted(
                r["path"]
                for r in loaded
                if r["buildlens"]["outcome"] not in {"processed", "unsupported"}
            ),
            "loaded_unsupported_by_buildlens": sorted(
                r["path"] for r in loaded if r["buildlens"]["outcome"] == "unsupported"
            ),
            "symlink_escape_followed": any(r[name].get("contains_canary") for r in loaded),
            "secret_bait_files_loaded": sorted(
                r["path"]
                for r in loaded
                if r[name].get("contains_bait") or r[name].get("contains_private_key_header")
            ),
            "absolute_paths_exposed": any(r[name].get("absolute_path_in_metadata") for r in loaded),
            "errors": variant.get("errors", []),
            "warnings": variant.get("warnings", []),
            "guard_denials": variant.get("guard_denials", []),
        }
    return {
        "version": "stage12.loader-diff.v1",
        "sandbox": {"id": sandbox.id, "manifest": sandbox.manifest},
        "buildlens_policy": {"inventory": POLICY_VERSION, "reader": READER_VERSION},
        "buildlens_outcomes": {
            k: sum(1 for r in native.values() if r["outcome"] == k)
            for k in sorted({r["outcome"] for r in native.values()})
        },
        "summary": summary,
        "rows": rows,
    }
