"""Stage 15 Milestone A: flow evidence contracts, records and analysis-safe value shapes.

A flow evidence run reads source once (through the secure reader, fingerprint-checked) and
records, per scope, a statement-level CFG (``flow-cfg.v1``), callsites, bindings, JSX attribute
bindings and summaries. Two kinds of data are kept apart:

* **analysis-safe facts**: typed shapes over exact identifiers and literals stored as
  ``(type, sha256, length)`` (small ints, booleans and ``None`` by value). Resolution, CFG,
  guards and feasibility read only these;
* **display text**: bounded, secret-screened, control/bidi-flagged source excerpts stored
  separately and withheld when screening fails. No analysis reads display text.
"""

from __future__ import annotations

import hashlib
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal
from uuid import UUID

from buildlens.domain.flow_cfg import CFG_VERSION
from buildlens.domain.source import (
    READER_VERSION,
    SCREENING_VERSION,
    ContentRejectedError,
    digest,
    screen_content,
)
from buildlens.domain.structural import ParseDiagnostic, SourceSpan

# v2 (Milestone B): module-resolution facts from tsconfig and package.json (D9, D13).
FLOW_EVIDENCE_VERSION = "flow-evidence.v2"
MODULE_FACTS = "module-facts.v1"
PYTHON_FLOW = "python-flow.v2"  # v2: return annotations and star imports (Milestone B)
TS_FLOW = "ts-flow.v2"  # v2: class extends clause and instance field count (Milestone B)
VERSION_FACTS = "framework-version-facts.v1"
SHAPES_VERSION = "flow-shapes.v1"
DISPLAY_VERSION = "flow-display.v1"
FLOW_EXTRACTORS = (PYTHON_FLOW, TS_FLOW)

# Placeholder caps (D7; A0 report §5). Exceeding a scope cap truncates that scope; exceeding
# the artifact cap truncates the remaining scopes; exceeding the run cap fails the run closed.
CALLSITES_PER_SCOPE = 1_024
CFG_NODES_PER_SCOPE = 2_048
BINDINGS_PER_SCOPE = 2_048
JSX_PER_SCOPE = 1_024
CALLSITES_PER_ARTIFACT = 8_192
SCOPES_PER_ARTIFACT = 4_096
CALLSITES_PER_RUN = 500_000
SHAPE_DEPTH = 8
SHAPE_ITEMS = 16
DISPLAY_BYTES = 256
SUMMARY_SHAPES = 32
VERSION_FILE_BYTES = 8 * 1_048_576

# Version files read as data (D17), and the packages whose versions are extracted.
VERSION_FILES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "pdm.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "package.json",
    }
)
VERSION_PACKAGES = {
    "python": ("fastapi", "starlette", "sqlalchemy", "flask", "pydantic"),
    "npm": (
        "react",
        "react-dom",
        "react-router",
        "react-router-dom",
        "@tanstack/react-query",
        "react-hook-form",
        "openapi-fetch",
        "@hey-api/client-axios",
        "@hey-api/client-fetch",
        "axios",
        "next",
        "typescript",
    ),
}

ScopeKind = Literal[
    "module",
    "function",
    "method",
    "class_body",
    "lambda",
    "comprehension",
    "generator_expression",
    "arrow",
    "function_expression",
]
CallableKind = Literal[
    "module_code",
    "class_code",
    "sync_function",
    "coroutine_function",
    "generator_function",
    "async_generator_function",
    "js_async_function",
    "js_generator_function",
    "js_async_generator_function",
]
CoverageState = Literal[
    "extracted", "partial", "failed", "not_current", "not_extracted", "not_applicable"
]
RunOutcome = Literal["complete", "partial", "failed"]


def _package(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def flow_pass_contract(extractor: str) -> dict[str, Any] | None:
    """The current contract of a flow pass, or ``None`` when its runtime is not installed."""
    shared = {
        "extractor": extractor,
        "reader": READER_VERSION,
        "screening": SCREENING_VERSION,
        "fingerprint": "sha256:artifact-bytes.v1",
        "cfg": CFG_VERSION,
        "shapes": SHAPES_VERSION,
        "display": DISPLAY_VERSION,
        "limits": {
            "callsites_per_scope": CALLSITES_PER_SCOPE,
            "cfg_nodes_per_scope": CFG_NODES_PER_SCOPE,
            "bindings_per_scope": BINDINGS_PER_SCOPE,
            "jsx_per_scope": JSX_PER_SCOPE,
            "callsites_per_artifact": CALLSITES_PER_ARTIFACT,
            "scopes_per_artifact": SCOPES_PER_ARTIFACT,
            "shape_depth": SHAPE_DEPTH,
            "display_bytes": DISPLAY_BYTES,
        },
    }
    if extractor == PYTHON_FLOW:
        return {
            **shared,
            "runtime_version": sys.version.split()[0],
            "decoding": "python-tokenize.v2",
            "parser": "python-ast",
            "coordinates": "ast-lines-utf8-byte-columns.v1",
        }
    if extractor == TS_FLOW:
        binding = _package("tree-sitter")
        typescript = _package("tree-sitter-typescript")
        javascript = _package("tree-sitter-javascript")
        if binding is None or typescript is None or javascript is None:
            return None
        from buildlens.domain.extractors import TREE_SITTER_CANARY_DIGEST

        return {
            **shared,
            "binding": f"tree-sitter=={binding}",
            "grammar": f"tree-sitter-typescript=={typescript};tree-sitter-javascript=={javascript}",
            "decoding": "utf-8-sig.v1",
            "coordinates": "utf8-bomless-bytes-lines-and-decoded-offsets.v1",
            "partial_parse": "skip-erroneous-top-level-statements.v1",
            "canary": TREE_SITTER_CANARY_DIGEST,
        }
    raise ValueError(f"Unknown flow extractor {extractor!r}")


def flow_pass_digest(extractor: str) -> str | None:
    contract = flow_pass_contract(extractor)
    return digest(contract) if contract is not None else None


def version_facts_contract() -> dict[str, Any]:
    return {
        "version_facts": VERSION_FACTS,
        "reader": READER_VERSION,
        "files": sorted(VERSION_FILES) + ["requirements*.txt", "tsconfig*.json"],
        "module_facts": MODULE_FACTS,
        "packages": {key: list(values) for key, values in VERSION_PACKAGES.items()},
        "max_bytes": VERSION_FILE_BYTES,
        "parsers": "tomllib+json+yaml.safe_load",
    }


def flow_evidence_contract() -> dict[str, Any]:
    return {
        "flow_evidence": FLOW_EVIDENCE_VERSION,
        "passes": {name: flow_pass_digest(name) for name in FLOW_EXTRACTORS},
        "version_facts": digest(version_facts_contract()),
        "callsites_per_run": CALLSITES_PER_RUN,
    }


# ----------------------------------------------------------------------------- shapes


def literal(value: object) -> dict[str, Any]:
    """Analysis-safe literal: identity without storing arbitrary text."""
    if value is None:
        return {"k": "lit", "t": "none"}
    if isinstance(value, bool):
        return {"k": "lit", "t": "bool", "v": value}
    if isinstance(value, int) and abs(value) < 2**53:
        return {"k": "lit", "t": "int", "v": value}
    if isinstance(value, str):
        data = value.encode("utf-8", errors="surrogatepass")
        return {
            "k": "lit",
            "t": "str",
            "sha256": hashlib.sha256(data).hexdigest(),
            "len": len(value),
        }
    if isinstance(value, bytes):
        return {
            "k": "lit",
            "t": "bytes",
            "sha256": hashlib.sha256(value).hexdigest(),
            "len": len(value),
        }
    if isinstance(value, float):
        return {
            "k": "lit",
            "t": "float",
            "sha256": hashlib.sha256(repr(value).encode()).hexdigest(),
        }
    return {"k": "lit", "t": type(value).__name__}


def string_literal(value: str) -> dict[str, Any]:
    return literal(value)


# ----------------------------------------------------------------------------- display

_BIDI = {
    "؜",
    "‎",
    "‏",
    *(chr(c) for c in range(0x202A, 0x202F)),
    *(chr(c) for c in range(0x2066, 0x206A)),
}


@dataclass(frozen=True, slots=True)
class DisplayText:
    key: str
    text: str | None
    flags: tuple[str, ...] = ()


def display(key: str, text: str) -> DisplayText:
    """Bounded, screened display text. Analysis never reads it."""
    data = text.encode("utf-8", errors="replace")
    flags: list[str] = []
    if len(data) > DISPLAY_BYTES:
        text = data[:DISPLAY_BYTES].decode("utf-8", errors="ignore")
        flags.append("TRUNCATED")
    try:
        screen_content(text)
    except ContentRejectedError:
        return DisplayText(key, None, ("WITHHELD_SCREENING",))
    if any(ch in _BIDI for ch in text):
        flags.append("BIDI_CONTROL")
    if any(unicodedata.category(ch) == "Cc" and ch not in "\t\n\r" for ch in text):
        flags.append("CONTROL_CHARACTER")
    return DisplayText(key, text, tuple(flags))


# ----------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class FlowCallsite:
    ordinal: int
    cfg_node: int
    callee: dict[str, Any]
    arguments: tuple[dict[str, Any], ...]
    awaited: bool
    span: SourceSpan
    start_offset: int | None = None
    end_offset: int | None = None
    text_key: str | None = None
    # Further CFG nodes evaluating the same source call (a ``finally`` body is lowered once per
    # completion kind, so a call inside it appears in each copy).
    copies: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowJsxBinding:
    ordinal: int
    cfg_node: int
    element: str
    intrinsic: bool
    attribute: str
    value: dict[str, Any]
    span: SourceSpan
    start_offset: int | None = None
    end_offset: int | None = None


@dataclass(frozen=True, slots=True)
class FlowScope:
    key: str
    parent_key: str | None
    kind: str
    callable_kind: str
    name: str | None
    span: SourceSpan
    start_offset: int | None
    end_offset: int | None
    truncated: str | None
    cfg: dict[str, Any]
    summary: dict[str, Any]
    callsites: tuple[FlowCallsite, ...] = ()
    jsx: tuple[FlowJsxBinding, ...] = ()
    owner_symbol_id: UUID | None = None

    def portable(self) -> dict[str, Any]:
        """Everything but database identities (owner ids differ per run; positions do not)."""
        return {
            "key": self.key,
            "parent": self.parent_key,
            "kind": self.kind,
            "callable_kind": self.callable_kind,
            "name": self.name,
            "span": _span(self.span),
            "offsets": [self.start_offset, self.end_offset],
            "truncated": self.truncated,
            "cfg": self.cfg,
            "summary": self.summary,
            "callsites": [_callsite(c) for c in self.callsites],
            "jsx": [_jsx(j) for j in self.jsx],
        }

    @property
    def digest(self) -> str:
        return str(digest(self.portable()))


def _span(span: SourceSpan) -> list[int | None]:
    return [span.start_line, span.start_column, span.end_line, span.end_column]


def _callsite(c: FlowCallsite) -> dict[str, Any]:
    return {
        "ordinal": c.ordinal,
        "node": c.cfg_node,
        "callee": c.callee,
        "arguments": list(c.arguments),
        "awaited": c.awaited,
        "span": _span(c.span),
        "offsets": [c.start_offset, c.end_offset],
        "text": c.text_key,
        "copies": list(c.copies),
    }


def callsite_digest(c: FlowCallsite) -> str:
    """Row digest of a stored callsite (cited by flow model evidence)."""
    return str(digest(_callsite(c)))


def jsx_digest(j: FlowJsxBinding) -> str:
    return str(digest(_jsx(j)))


def _jsx(j: FlowJsxBinding) -> dict[str, Any]:
    return {
        "ordinal": j.ordinal,
        "node": j.cfg_node,
        "element": j.element,
        "intrinsic": j.intrinsic,
        "attribute": j.attribute,
        "value": j.value,
        "span": _span(j.span),
        "offsets": [j.start_offset, j.end_offset],
    }


@dataclass(frozen=True, slots=True)
class FlowExtraction:
    """One artifact's flow evidence, as produced in the worker."""

    extractor: str
    contract: dict[str, Any]
    contract_digest: str
    outcome: Literal["extracted", "partial", "failed"]
    input_fingerprint: str
    diagnostics: tuple[ParseDiagnostic, ...] = ()
    scopes: tuple[FlowScope, ...] = ()
    display_texts: tuple[DisplayText, ...] = ()
    unmatched_declarations: int = 0

    @property
    def callsite_count(self) -> int:
        return sum(len(scope.callsites) for scope in self.scopes)

    @property
    def evidence_digest(self) -> str:
        return str(
            digest(
                {
                    "contract": self.contract_digest,
                    "outcome": self.outcome,
                    "input": self.input_fingerprint,
                    "diagnostics": [[d.code, d.severity] for d in self.diagnostics],
                    # Order-independent: rows read back from PostgreSQL must reproduce it exactly.
                    "scopes": sorted([scope.key, scope.digest] for scope in self.scopes),
                    "display": sorted(
                        {t.key: [t.key, t.text, list(t.flags)] for t in self.display_texts}.values()
                    ),
                }
            )
        )


@dataclass(frozen=True, slots=True)
class FrameworkFact:
    ecosystem: Literal["python", "npm"]
    package: str
    version: str | None
    constraint: str | None
    source_kind: Literal["lockfile", "requirements_pin", "declared"]
    locator: str
    input_fingerprint: str
    # Workspace importer within the file ("" = the file's own directory), e.g. "apps/web".
    scope: str = ""

    @property
    def directory(self) -> str:
        return self.locator.rsplit("/", 1)[0] if "/" in self.locator else ""


@dataclass(frozen=True, slots=True)
class ModuleFact:
    """Module-resolution facts read as data from a tsconfig or package.json (D9, D13)."""

    kind: Literal["tsconfig", "package"]
    locator: str
    input_fingerprint: str
    facts: dict[str, Any]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactFlowCoverage:
    artifact_analysis_id: UUID
    artifact_id: UUID
    locator: str
    language: str | None
    state: CoverageState
    extractor: str | None
    contract_digest: str | None
    input_fingerprint: str | None
    execution_mode: Literal["extracted", "reused", "none"]
    reuse_source_run_id: UUID | None
    evidence_digest: str | None
    scope_count: int
    callsite_count: int
    diagnostics: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class FlowEvidenceRun:
    id: UUID
    project_id: UUID
    project_version_id: UUID
    revision: int
    started_at: datetime
    completed_at: datetime
    outcome: RunOutcome
    structural_analysis_id: UUID
    structural_revision: int
    contract: dict[str, Any]
    contract_digest: str
    input_authority_digest: str
    evidence_digest: str | None
    coverage: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    superseded: bool = False
    captured_active_run_id: UUID | None = None
    captured_latest_revision: int = 0
    failure_code: str | None = None


def coverage_summary(rows: list[ArtifactFlowCoverage]) -> dict[str, Any]:
    states: dict[str, int] = {}
    for row in rows:
        states[row.state] = states.get(row.state, 0) + 1
    return {
        "artifacts": len(rows),
        "states": dict(sorted(states.items())),
        "scopes": sum(r.scope_count for r in rows),
        "callsites": sum(r.callsite_count for r in rows),
        "reused": sum(1 for r in rows if r.execution_mode == "reused"),
    }
