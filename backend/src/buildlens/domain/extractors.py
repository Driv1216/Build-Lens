"""Stage 13 additive extractor contracts and per-language producer authority.

The frozen Python declaration producer keeps its run-level ``structural_contract()``.
Every additive pass (Python facts, TS/JS declarations) carries its own contract per artifact,
so a change to one never alters the currency of another (invariants M3/M4).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from buildlens.domain.source import READER_VERSION, SCREENING_VERSION, digest

PYTHON_FACTS = "python-facts.v1"
TYPESCRIPT_DECLARATIONS = "typescript-declarations.v1"
JAVASCRIPT_DECLARATIONS = "javascript-declarations.v1"
TS_IDENTITY_SCHEME = "ts-declaration-slot.v1"
PYTHON_IDENTITY_SCHEME = "python-declaration-slot.v1"
# Stage 14 additive literal passes: sanitised syntactic evidence for architecture views only.
# They are never part of the catalog contract or of processing dependency fingerprints.
PYTHON_LITERALS = "python-architecture-literals.v1"
TS_LITERALS = "ts-architecture-literals.v1"
LITERAL_EXTRACTORS = frozenset({PYTHON_LITERALS, TS_LITERALS})

LITERAL_ROWS_CONTRACT = "architecture-literal-rows.v2"
LITERAL_SANITISATION_CONTRACT = "url-origin-path-only.v1"
# Syntactic binding scope of TS/JS callee roots; part of the TS pass contract, so rows made
# before it existed are no longer current and a run that used them becomes stale.
TS_CALLEE_SCOPE_CONTRACT = "ts-callee-scope.v1"

# Digest of the declarations extracted from the built-in canary fixture by the pinned
# grammars. The worker handshake recomputes it; any difference fails the language closed.
TREE_SITTER_CANARY_DIGEST = "2b1326b3187ebec512f3d824e462311343ba431fafe29850c3f2a213224bbf51"


@dataclass(frozen=True, slots=True)
class LanguageSupport:
    """Which passes produce a language's evidence. ``None`` declarations = frozen Python."""

    language: str
    declaration_extractor: str | None
    additional_extractors: tuple[str, ...]


LANGUAGES: dict[str, LanguageSupport] = {
    "Python": LanguageSupport("Python", None, (PYTHON_FACTS, PYTHON_LITERALS)),
    "TypeScript": LanguageSupport("TypeScript", TYPESCRIPT_DECLARATIONS, (TS_LITERALS,)),
    "JavaScript": LanguageSupport("JavaScript", JAVASCRIPT_DECLARATIONS, (TS_LITERALS,)),
}


def _package(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def extractor_contract(extractor: str) -> dict[str, str] | None:
    """The current contract for an extractor, or ``None`` when its runtime is not installed."""
    shared = {
        "extractor": extractor,
        "reader": READER_VERSION,
        "screening": SCREENING_VERSION,
        "fingerprint": "sha256:artifact-bytes.v1",
    }
    if extractor == PYTHON_FACTS:
        return {
            **shared,
            "runtime_version": sys.version.split()[0],
            "decoding": "python-tokenize.v2",
            "parser": "python-ast+tokenize",
            "limits": "references-256-per-symbol-4096-per-artifact.v1",
        }
    if extractor == PYTHON_LITERALS:
        return {
            **shared,
            "runtime_version": sys.version.split()[0],
            "decoding": "python-tokenize.v2",
            "parser": "python-ast",
            "rows": LITERAL_ROWS_CONTRACT,
            "sanitisation": LITERAL_SANITISATION_CONTRACT,
        }
    if extractor == TS_LITERALS:
        binding = _package("tree-sitter")
        typescript = _package("tree-sitter-typescript")
        javascript = _package("tree-sitter-javascript")
        if binding is None or typescript is None or javascript is None:
            return None
        return {
            **shared,
            "binding": f"tree-sitter=={binding}",
            "grammar": f"tree-sitter-typescript=={typescript};tree-sitter-javascript=={javascript}",
            "decoding": "utf-8-sig.v1",
            "partial_parse": "skip-erroneous-top-level-statements.v1",
            "callee_scope": TS_CALLEE_SCOPE_CONTRACT,
            "rows": LITERAL_ROWS_CONTRACT,
            "sanitisation": LITERAL_SANITISATION_CONTRACT,
            "canary": TREE_SITTER_CANARY_DIGEST,
        }
    if extractor in {TYPESCRIPT_DECLARATIONS, JAVASCRIPT_DECLARATIONS}:
        binding = _package("tree-sitter")
        grammar_package = (
            "tree-sitter-typescript"
            if extractor == TYPESCRIPT_DECLARATIONS
            else "tree-sitter-javascript"
        )
        grammar = _package(grammar_package)
        if binding is None or grammar is None:
            return None
        return {
            **shared,
            "binding": f"tree-sitter=={binding}",
            "grammar": f"{grammar_package}=={grammar}",
            "decoding": "utf-8-sig.v1",
            "coordinates": "utf8-bomless-bytes-to-decoded-offsets.v1",
            "identity": TS_IDENTITY_SCHEME,
            "partial_parse": "drop-erroneous-top-level-statements.v1",
            "limits": "references-256-per-symbol-4096-per-artifact.v1",
            "canary": TREE_SITTER_CANARY_DIGEST,
        }
    raise ValueError(f"Unknown extractor {extractor!r}")


def extractor_contract_digest(extractor: str) -> str | None:
    contract = extractor_contract(extractor)
    return digest(contract) if contract is not None else None


def current_declaration_digests() -> dict[str, str]:
    """Current digests of every available non-Python declaration extractor."""
    values: dict[str, str] = {}
    for support in LANGUAGES.values():
        if support.declaration_extractor:
            value = extractor_contract_digest(support.declaration_extractor)
            if value is not None:
                values[support.declaration_extractor] = value
    return values


def language_for(language: str | None, locator: str) -> LanguageSupport | None:
    if language not in LANGUAGES:
        return None
    if language == "JavaScript" and locator.lower().endswith(".min.js"):
        return None
    return LANGUAGES[language]
