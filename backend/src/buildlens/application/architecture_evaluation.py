"""Stage 14 Milestone D: the pre-registered evaluation (``architecture-evaluation.v1``).

Order is enforced as in Stage 13: an approved pre-registration (units, outcomes, metrics,
thresholds, oracle, packet design and the frozen corpus) is locked first, and every score
refuses a pre-registration that is not approved, not complete or whose fixed texts differ
from this code. Nothing here judges anything: module edges are scored mechanically against a
lab-only differential oracle (``backend/scripts/architecture_differential.py``), API
connections against human labels collected on a blind packet. Undefined metrics fail their
criterion; a stratum with no eligible units makes the suite ``inconclusive``.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from buildlens.application.architecture import ArchitectureService, Assertion, LoadedGraph
from buildlens.domain.architecture import (
    MODULE_EDGE_KINDS,
    ArchitectureEdge,
    ArchitectureEvidence,
    ArchitectureRun,
)
from buildlens.domain.source import digest

EVALUATION_VERSION = "architecture-evaluation.v1"
MODULE_UNITS_VERSION = "architecture-module-units.v1"
API_PACKET_VERSION = "architecture-api-packet.v1"
# v2: endpoints carry ``determinable`` and sink-sample files ``reviewed``, so an untouched
# template can no longer pass as "no request sites" (v1 label files are refused).
API_LABELS_VERSION = "architecture-api-labels.v2"
# v2: system-only emissions count as false positives (v1 left them out of precision), and API
# labels are validated for completeness, vocabulary and duplicates before scoring.
SCORE_VERSION = "architecture-evaluation-score.v2"
ALIAS_PREFIXES = ("@/", "~/", "#", "/")
PYTHON_SUFFIXES = (".py",)
TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

Row = dict[str, Any]

UNITS: dict[str, str] = {
    "module_python": "One unit per imported name of a Python import statement anywhere in a "
    "file (module, class or function scope): key (form, level, module, name) plus its "
    "occurrence index among identical keys in source order. `import a.b` targets a/b; "
    "`from m import n` targets m.n when m.n is a module, otherwise m.",
    "module_typescript": "One unit per TS/JS statement with a module specifier: import "
    "declarations (incl. `import type`), `import x = require()`, `export ... from`, and calls "
    "`import(<string>)` / `require(<string>)`; key (kind import|reexport, specifier) plus its "
    "occurrence index in source order.",
    "eligible": "A unit is eligible when its file is in the snapshot and the architecture "
    "run's module node has coverage state `analysed`; other units are `ineligible` and count "
    "only against coverage.",
    "alias_stratum": "TS/JS units whose specifier starts with @/, ~/, # or / form the alias "
    "stratum. The oracle labels them `unknown` (no tsconfig is read by either side).",
    "api_site": "One unit per request site (client_call node) of the evaluated run, labelled "
    "by the reviewer with the endpoint unit(s) its statically determinable destination "
    "targets, or `external`, `opaque` or `none`.",
    "api_endpoint": "One unit per backend endpoint (api_endpoint node), labelled with its "
    "full path(s) and method(s) as reconstructed from the excerpts.",
    "sink_sample": "A seeded, stratified sample of frontend code files shown as full verified "
    "stored source; the reviewer marks the start line of every HTTP request site.",
}
OUTCOMES: dict[str, str] = {
    "emitted_correct": "The system emitted a target (module or external) equal to the oracle's.",
    "emitted_incorrect": "The system emitted a target that differs from the oracle's, or "
    "emitted one where the oracle has none or unknown.",
    "abstained": "The oracle has a target but the system emitted none (unknown, ambiguous, "
    "opaque, no candidate or unmapped).",
    "correct_abstention": "Neither side has a target (oracle none/unknown, system unknown).",
    "ineligible": "The unit's file is not eligible.",
}
METRICS: dict[str, str] = {
    "precision": "emitted_correct / (emitted_correct + emitted_incorrect)",
    "recall": "emitted_correct / oracle-positive eligible units",
    "abstention_rate": "abstained / oracle-positive eligible units",
    "coverage": "eligible units / all units",
    "ambiguous_set_recall": "abstained units whose oracle target is in the system's ambiguous "
    "candidate set / abstained units with candidate sets",
    "interval": "Every metric reports numerator, denominator and a Wilson 95% interval.",
}
ZERO_DENOMINATOR = (
    "A metric with a zero denominator is undefined and its criterion fails. A stratum with zero "
    "eligible units makes its suite `inconclusive`. A suite passes only if every criterion and "
    "every hard gate passes."
)
DISAGREEMENT_CODES: dict[str, str] = {
    "ORACLE_LIMITATION": "The oracle cannot model the construct (e.g. bundler-only assets).",
    "DOCUMENTED_LIMITATION": "A limitation listed in docs/ARCHITECTURE_INTELLIGENCE.md.",
    "EXTRACTOR_DIFFERENCE": "The two parsers enumerate the statement differently.",
    "SYSTEM_DEFECT": "The system is wrong; counted, never excused.",
}
PACKET_DESIGN: dict[str, str] = {
    "blind": "The blind section lists unit ids, locations and verified stored source only; "
    "the system's folded URLs, methods, outcomes and candidates are absent and are revealed "
    "in a separate file only after labels are complete.",
    "site_excerpts": "For each file with request sites: its full stored source, plus the full "
    "stored source of every internal module it imports (one hop), which contains every "
    "constant, helper and wrapper definition the system might fold.",
    "endpoint_excerpts": "For each endpoint: the stored chunk containing its decorator, plus "
    "every chunk in the snapshot's Python files that contains `APIRouter(`, `FastAPI(`, "
    "`Blueprint(`, `Flask(`, `include_router(`, `register_blueprint(` or `.mount(`, selected "
    "by text, not by the system's literal rows.",
    "verification": "Every excerpt is a stored chunk whose sha256(content) equals its stored "
    "hash; a mismatch refuses the packet.",
    "tamper": "Packets and labels carry digests; scoring refuses changed packets, labels for "
    "another packet, missing labels and a different pre-registration.",
}
ADVERSARIAL: tuple[str, ...] = (
    "external host with a path equal to a local route",
    "relative URL whose base path differs from the mount",
    "route-like strings in comments, docstrings and test data",
    "method mismatch",
    "fetch method not normalised by the Fetch standard",
    "trailing slash difference",
    "{p:path} versus single-segment parameter",
    "typed converter mismatch",
    "environment-only base without fallback",
    "opaque ${a}${b} path",
    "route defined in an excluded directory",
    "duplicate route definitions",
    "same name stem with an unrelated feature",
    "production module importing a test helper",
    "homoglyph module names",
    "bidi control characters in literals",
    "shadowed fetch declaration",
    "axios imported from a local mock",
    "wrapper imported through a barrel",
    "private network host",
    "URL-shaped arguments to non-sink calls",
    "alias import of an API wrapper",
)
SUITES: dict[str, dict[str, float]] = {
    "fixtures": {
        "precision_min": 1.0,
        "recall_min": 1.0,
        "abstention_max": 0.0,
        "coverage_min": 1.0,
        "designed_negative_emissions_max": 0,
    },
    "self_module": {
        "precision_min": 0.99,
        "recall_min": 0.99,
        "coverage_min": 0.95,
        "unexplained_disagreements_max": 0,
        "alias_resolved_edges_max": 0,
    },
    "self_api": {
        "precision_min": 0.95,
        "recall_min": 0.80,
        "abstention_max": 0.20,
        "unbound_host_local_connections_max": 0,
        "external_service_labels_max": 0,
    },
    "adversarial": {
        "false_connections_max": 0,
        "source_reads_during_build_max": 0,
        "outbound_connections_max": 0,
        "tamper_detection_min": 1.0,
    },
}


class EvaluationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def preregistration_template() -> dict[str, Any]:
    """A draft; thresholds may change only before the owner approves and freezes it."""
    return {
        "version": EVALUATION_VERSION,
        "status": "draft",
        "approved_by": None,
        "corpus": {
            "repository": "BuildLens",
            "commit": None,
            "tree_sha256": None,
            "archive": "git archive of the commit (tracked files only), extracted read-only "
            "outside the repository; built in a dedicated scratch database, never in "
            "buildlens-stage13-eval",
            "second_corpus": "only an owner-supplied local repository (D8); exploratory",
        },
        "units": UNITS,
        "outcomes": OUTCOMES,
        "metrics": METRICS,
        "zero_denominator": ZERO_DENOMINATOR,
        "suites": SUITES,
        "oracle": {
            "python": "importlib.machinery.PathFinder.find_spec over the listed roots only "
            "(filesystem search; nothing imported). A top-level name found only as a "
            "namespace portion is labelled external (PEP 420: a regular package elsewhere "
            "on sys.path wins).",
            "python_roots": None,
            "typescript": "ts.resolveModuleName with the listed options and a host confined to "
            "the archive (no Program, no emit, no plugins). Bare specifiers that do not "
            "resolve inside the archive are external; relative specifiers with a non-code "
            "extension resolve by exact file existence.",
            "typescript_options": None,
            "typescript_version": None,
        },
        "disagreement_codes": DISAGREEMENT_CODES,
        "api_packet": PACKET_DESIGN,
        "sink_sample": {
            "seed": 14,
            "size": 8,
            "population": "TS/TSX/JS files of the evaluated run that are analysed and are not "
            "tests (no `.test.` or `__tests__` in the locator)",
            "strata": "round-robin over parent directories in sorted order, a seeded shuffle "
            "within each directory",
            "reported": "sink-detection recall and the unmatched system sites; not gated",
        },
        "adversarial": list(ADVERSARIAL),
        "performance_placeholders": {"build_seconds_max": 30, "query_p95_seconds_max": 1.0},
        "judging": "Human labelling only; no LLM judge. The reviewer is not shown the "
        "system's output before labels are complete.",
    }


def require_approved(preregistration: Mapping[str, Any]) -> str:
    """Approved, complete and identical to this code's fixed texts; returns its digest."""
    if preregistration.get("version") != EVALUATION_VERSION:
        raise EvaluationError("PREREGISTRATION_VERSION")
    if preregistration.get("status") != "approved" or not preregistration.get("approved_by"):
        raise EvaluationError("PREREGISTRATION_NOT_APPROVED")
    fixed = {
        "units": UNITS,
        "outcomes": OUTCOMES,
        "metrics": METRICS,
        "zero_denominator": ZERO_DENOMINATOR,
        "disagreement_codes": DISAGREEMENT_CODES,
        "api_packet": PACKET_DESIGN,
        "adversarial": list(ADVERSARIAL),
    }
    if any(preregistration.get(key) != value for key, value in fixed.items()):
        raise EvaluationError("PREREGISTRATION_TEXT_MISMATCH")
    suites = preregistration.get("suites") or {}
    if set(suites) != set(SUITES) or any(
        set(suites[name]) != set(SUITES[name])
        or not all(isinstance(value, (int, float)) for value in suites[name].values())
        for name in SUITES
    ):
        raise EvaluationError("PREREGISTRATION_INCOMPLETE")
    corpus = preregistration.get("corpus") or {}
    oracle = preregistration.get("oracle") or {}
    if not all(
        isinstance(corpus.get(key), str) and corpus[key] for key in ("commit", "tree_sha256")
    ):
        raise EvaluationError("PREREGISTRATION_INCOMPLETE")
    if not oracle.get("python_roots") or not oracle.get("typescript_options"):
        raise EvaluationError("PREREGISTRATION_INCOMPLETE")
    value: str = digest(dict(preregistration))
    return value


# --------------------------------------------------------------------------- metrics


def wilson(numerator: int, denominator: int, z: float = 1.959963984540054) -> list[float] | None:
    if denominator <= 0:
        return None
    p = numerator / denominator
    centre = p + z * z / (2 * denominator)
    margin = z * math.sqrt(p * (1 - p) / denominator + z * z / (4 * denominator * denominator))
    scale = 1 + z * z / denominator
    return [
        round(max(0.0, (centre - margin) / scale), 6),
        round(min(1.0, (centre + margin) / scale), 6),
    ]


def metric(numerator: int, denominator: int) -> Row:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 6) if denominator else None,
        "wilson95": wilson(numerator, denominator),
    }


def classify(oracle: Row, system: Row, eligible: bool) -> str:
    """One unit's outcome. Labels: module (with targets), external, none, unknown."""
    if not eligible:
        return "ineligible"
    positive = oracle["label"] in {"module", "external"}
    emitted = system["label"] in {"module", "external"}
    if positive and emitted:
        same = system["label"] == oracle["label"] and (
            oracle["label"] == "external"
            or bool(set(system.get("targets") or ()) & set(oracle.get("targets") or ()))
        )
        return "emitted_correct" if same else "emitted_incorrect"
    if emitted:
        return "emitted_incorrect"
    return "abstained" if positive else "correct_abstention"


def summarise(units: Sequence[Row]) -> Row:
    """Counts and metrics over scored units (each with ``outcome`` and ``oracle``).

    System-only units (predictions for statements the oracle does not enumerate) count in
    every outcome and in precision, but not in coverage, which is a property of the oracle's
    enumeration."""
    counts = Counter(unit["outcome"] for unit in units)
    enumerated = [unit for unit in units if not unit.get("system_only")]
    eligible = [unit for unit in enumerated if unit["outcome"] != "ineligible"]
    positive = [unit for unit in eligible if unit["oracle"]["label"] in {"module", "external"}]
    correct = counts["emitted_correct"]
    emitted = correct + counts["emitted_incorrect"]
    abstained = [unit for unit in positive if unit["outcome"] == "abstained"]
    with_sets = [unit for unit in abstained if unit["system"].get("candidates")]
    in_set = [
        unit
        for unit in with_sets
        if set(unit["system"]["candidates"]) & set(unit["oracle"].get("targets") or ())
    ]
    return {
        "units": len(enumerated),
        "eligible": len(eligible),
        "oracle_positive": len(positive),
        "outcomes": {name: counts[name] for name in OUTCOMES},
        "precision": metric(correct, emitted),
        "recall": metric(correct, len(positive)),
        "abstention_rate": metric(len(abstained), len(positive)),
        "coverage": metric(len(eligible), len(enumerated)),
        "ambiguous_set_recall": metric(len(in_set), len(with_sets)),
        "system_only_emitted": sum(1 for unit in units if unit.get("system_only")),
    }


def _criterion(name: str, threshold: float, value: float | None, maximum: bool) -> Row:
    passed = value is not None and (value <= threshold if maximum else value >= threshold)
    return {"name": name, "threshold": threshold, "value": value, "passed": passed}


def criteria(
    thresholds: Mapping[str, float], summary: Row, gates: Mapping[str, float]
) -> list[Row]:
    values: list[Row] = []
    for name, threshold in thresholds.items():
        base, _, bound = name.rpartition("_")
        if base in {"precision", "recall", "coverage"}:
            value = summary[base]["value"]
        elif base == "abstention":
            value = summary["abstention_rate"]["value"]
        else:
            value = gates.get(base)
        values.append(_criterion(name, threshold, value, bound == "max"))
    return values


def verdict(rows: Sequence[Row], strata: Mapping[str, Row]) -> str:
    if any(stratum["eligible"] == 0 for stratum in strata.values()):
        return "inconclusive"
    return "passed" if rows and all(row["passed"] for row in rows) else "failed"


# --------------------------------------------------------------------------- module units


def is_alias(specifier: str) -> bool:
    return specifier.startswith(ALIAS_PREFIXES)


def _occurrences(items: Iterable[tuple[tuple[Any, ...], Row]]) -> list[Row]:
    seen: Counter[tuple[Any, ...]] = Counter()
    values = []
    for key, value in items:
        seen[key] += 1
        values.append({**value, "unit": "|".join(str(part) for part in (*key, seen[key]))})
    return values


def statement_refs(
    loaded: LoadedGraph, evidence: Mapping[str, ArchitectureEvidence]
) -> dict[str, ArchitectureEdge]:
    """Import/export row id -> the module edge whose ``import_statements`` evidence cites it."""
    edge_of: dict[str, ArchitectureEdge] = {}
    for edge in loaded.edges.values():
        if edge.kind not in MODULE_EDGE_KINDS:
            continue
        for link in edge.evidence:
            item = evidence.get(link.digest) if link.source == "architecture" else None
            if item is None or item.kind != "import_statements":
                continue
            for ref in item.source_refs:
                if ref.get("table") in {"import_references", "export_references"}:
                    edge_of[str(ref["id"])] = edge
    return edge_of


def system_module_units(
    loaded: LoadedGraph, edge_of: Mapping[str, ArchitectureEdge], statements: Mapping[str, Row]
) -> tuple[list[Row], dict[str, str]]:
    """The run's per-unit output, keyed like the oracle, plus each module's coverage state.

    ``statements`` maps import/export rows of the run's structural analysis to their fields;
    a row cited by no edge is reported as ``unmapped`` (an abstention)."""
    coverage: dict[str, str] = {
        node.locator: node.coverage_state
        for node in loaded.nodes.values()
        if node.kind == "module" and node.locator
    }
    python: list[tuple[tuple[Any, ...], Row]] = []
    typescript: dict[tuple[str, str, int], Row] = {}
    for row_id, row in statements.items():
        locator = str(row["locator"])
        edge = edge_of.get(row_id)
        if edge is None:
            output: Row = {"label": "unknown", "targets": [], "status": "unmapped"}
        elif edge.target_key and edge.target_key.startswith("module:"):
            output = {
                "label": "module",
                "targets": [edge.target_key.removeprefix("module:")],
                "status": edge.resolution_status,
                "class": edge.import_class,
            }
        elif edge.resolution_status == "external":
            output = {"label": "external", "targets": [], "status": "external"}
        else:
            output = {"label": "unknown", "targets": [], "status": edge.resolution_status}
        if locator.endswith(PYTHON_SUFFIXES):
            key = (
                locator,
                row["form"],
                int(row.get("level") or 0),
                row.get("from_module") or "",
                row["name"],
            )
            order = (
                int(row["start_line"]),
                int(row["statement_ordinal"]),
                int(row["item_ordinal"]),
            )
            python.append((key, {"order": order, "locator": locator, "system": output}))
        else:
            kind = "reexport" if row["table"] == "export_references" else "import"
            specifier = str(row.get("source_module") if kind == "reexport" else row["from_module"])
            statement = int(row["start_line"] if kind == "reexport" else row["statement_ordinal"])
            typescript.setdefault(
                (locator, kind, statement),
                {
                    "order": (int(row["start_line"]), statement),
                    "locator": locator,
                    "key": (locator, kind, specifier),
                    "system": output,
                },
            )
    python.sort(key=lambda item: (item[0][0], item[1]["order"]))
    units = _occurrences(python)
    ordered = sorted(typescript.values(), key=lambda value: (value["locator"], value["order"]))
    units += _occurrences((value["key"], value) for value in ordered)
    for unit in units:
        unit.pop("order", None)
        unit.pop("key", None)
    return units, coverage


def system_only_units(
    extra: Iterable[Row], coverage: Mapping[str, str]
) -> tuple[list[Row], list[Row]]:
    """Score system units the oracle does not enumerate; returns (scored, out of scope).

    By the pre-registered outcome definitions, a target emitted where the oracle has none is
    ``emitted_incorrect``: an emission in an eligible file is a false positive and counts
    against precision whatever its explanation. Only two cases are out of scope and listed
    without being scored: the file is not eligible (its coverage state is not ``analysed``),
    or the system emitted nothing (an ``unknown`` unit is not a prediction)."""
    scored: list[Row] = []
    out_of_scope: list[Row] = []
    for unit in extra:
        locator = str(unit["locator"])
        output = unit["system"]
        if coverage.get(locator) != "analysed":
            out_of_scope.append({**unit, "reason": "FILE_NOT_ELIGIBLE"})
            continue
        if output.get("label") not in {"module", "external"}:
            out_of_scope.append({**unit, "reason": "NO_PREDICTION"})
            continue
        python = locator.endswith(PYTHON_SUFFIXES)
        # Unit keys are locator|kind|specifier|occurrence for TS/JS (see ``UNITS``).
        parts = str(unit["unit"]).rsplit("|", 3)
        specifier = parts[2] if not python and len(parts) == 4 else ""
        oracle = {"label": "none", "targets": []}
        scored.append(
            {
                "unit": unit["unit"],
                "locator": locator,
                "language": "python" if python else "typescript",
                "alias": bool(specifier) and is_alias(specifier),
                "system_only": True,
                "oracle": oracle,
                "system": output,
                "outcome": classify(oracle, output, True),
            }
        )
    return scored, out_of_scope


def score_module_units(
    preregistration: Mapping[str, Any],
    oracle: Mapping[str, Any],
    system: Mapping[str, Any],
    explanations: Mapping[str, Mapping[str, str]] | None = None,
) -> Row:
    """Score the run's module units against the differential oracle (self-hosted suite)."""
    prereg_digest = require_approved(preregistration)
    if system.get("version") != MODULE_UNITS_VERSION:
        raise EvaluationError("SYSTEM_UNITS_VERSION")
    if oracle.get("corpus_tree_sha256") != preregistration["corpus"]["tree_sha256"]:
        raise EvaluationError("ORACLE_CORPUS_MISMATCH")
    if system.get("corpus_tree_sha256") != preregistration["corpus"]["tree_sha256"]:
        raise EvaluationError("SYSTEM_CORPUS_MISMATCH")
    explanations = explanations or {}
    by_unit = {unit["unit"]: unit for unit in system["units"]}
    coverage = system["coverage"]
    scored: list[Row] = []
    for unit in oracle["units"]:
        locator = unit["locator"]
        theirs = by_unit.pop(unit["unit"], None)
        output = (
            theirs["system"] if theirs else {"label": "unknown", "targets": [], "status": "missing"}
        )
        eligible = coverage.get(locator) == "analysed"
        scored.append(
            {
                "unit": unit["unit"],
                "locator": locator,
                "language": unit["language"],
                "alias": unit.get("alias", False),
                "oracle": {"label": unit["label"], "targets": unit.get("targets", [])},
                "system": output,
                "outcome": classify(unit, output, eligible),
            }
        )
    extra, out_of_scope = system_only_units(by_unit.values(), coverage)
    scored += extra
    disagreements = [
        {**unit, "explanation": explanations.get(unit["unit"])}
        for unit in scored
        if unit["outcome"] in {"emitted_incorrect", "abstained"}
    ]
    unexplained = [
        item
        for item in disagreements
        if not item["explanation"] or item["explanation"].get("code") not in DISAGREEMENT_CODES
    ]
    defects = [
        item for item in disagreements if (item["explanation"] or {}).get("code") == "SYSTEM_DEFECT"
    ]
    alias_resolved = [
        unit for unit in scored if unit["alias"] and unit["system"]["label"] == "module"
    ]
    strata = {
        language: summarise([unit for unit in scored if unit["language"] == language])
        for language in ("python", "typescript")
    }
    non_alias = summarise([unit for unit in scored if not unit["alias"]])
    overall = summarise(scored)
    rows = criteria(
        preregistration["suites"]["self_module"],
        non_alias,
        {
            "unexplained_disagreements": len(unexplained),
            "alias_resolved_edges": len(alias_resolved),
        },
    )
    return {
        "version": SCORE_VERSION,
        "suite": "self_module",
        "preregistration_digest": prereg_digest,
        "oracle_digest": digest(dict(oracle)),
        "system_digest": digest(dict(system)),
        "run_id": system.get("run_id"),
        "acceptance_scope": "non-alias stratum (C-6); overall reported, not gated",
        "summary": non_alias,
        "overall_all_strata": overall,
        "strata": strata,
        "alias_stratum": summarise([unit for unit in scored if unit["alias"]]),
        "criteria": rows,
        "overall": verdict(rows, strata),
        "disagreements": disagreements,
        "disagreement_counts": dict(
            sorted(Counter(str(item["outcome"]) for item in disagreements).items())
        ),
        "false_positives": sum(1 for unit in scored if unit["outcome"] == "emitted_incorrect"),
        "system_only_emitted": len(extra),
        "out_of_scope_system_units": out_of_scope,
        "unexplained": len(unexplained),
        "system_defects": len(defects),
    }


def score_fixture_units(
    preregistration: Mapping[str, Any], designed: Mapping[str, Any], system: Mapping[str, Any]
) -> Row:
    """Fixture suite: the designed oracle must be met exactly (precision, recall, coverage 1)."""
    prereg_digest = require_approved(preregistration)
    by_unit = {unit["unit"]: unit for unit in system["units"]}
    scored = []
    for unit in designed["units"]:
        theirs = by_unit.pop(unit["unit"], None)
        output = theirs["system"] if theirs else {"label": "unknown", "targets": []}
        eligible = system["coverage"].get(unit["locator"]) == "analysed"
        scored.append(
            {**unit, "oracle": unit, "system": output, "outcome": classify(unit, output, eligible)}
        )
    extra, out_of_scope = system_only_units(
        (
            {"unit": unit["unit"], "locator": unit["locator"], "system": unit["system"]}
            for unit in by_unit.values()
        ),
        system["coverage"],
    )
    scored += extra
    negatives = sum(
        1
        for unit in scored
        if unit["oracle"]["label"] not in {"module", "external"}
        and unit["system"]["label"] in {"module", "external"}
    ) + sum(1 for unit in out_of_scope if unit["system"]["label"] in {"module", "external"})
    summary = summarise(scored)
    rows = criteria(
        preregistration["suites"]["fixtures"],
        summary,
        {"designed_negative_emissions": negatives},
    )
    return {
        "version": SCORE_VERSION,
        "suite": "fixtures",
        "fixture": designed.get("fixture"),
        "preregistration_digest": prereg_digest,
        "summary": summary,
        "criteria": rows,
        "overall": verdict(rows, {"fixture": summary}),
        "undesigned_system_units": sorted(by_unit),
        "units": scored,
    }


# --------------------------------------------------------------------------- API packet


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def verified_source(chunks: Sequence[Row]) -> list[Row]:
    """Stored chunks with their hash re-checked; a mismatch refuses the packet."""
    values = []
    for chunk in sorted(chunks, key=lambda item: (item["start_line"], item["start_offset"])):
        if _sha(chunk["content"]) != chunk["content_hash"]:
            raise EvaluationError("CHUNK_HASH_MISMATCH")
        values.append(
            {
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "content": chunk["content"],
                "content_hash": chunk["content_hash"],
            }
        )
    return values


ROUTER_MARKERS = (
    "APIRouter(",
    "FastAPI(",
    "Blueprint(",
    "Flask(",
    "include_router(",
    "register_blueprint(",
    ".mount(",
)


def sink_sample(locators: Iterable[str], seed: int, size: int) -> list[str]:
    population = sorted(
        locator
        for locator in locators
        if locator.endswith(TS_SUFFIXES) and ".test." not in locator and "__tests__" not in locator
    )
    groups: dict[str, list[str]] = defaultdict(list)
    for locator in population:
        groups[locator.rpartition("/")[0]].append(locator)
    generator = random.Random(seed)
    for values in groups.values():
        generator.shuffle(values)
    picked: list[str] = []
    while len(picked) < min(size, len(population)):
        for directory in sorted(groups):
            if groups[directory] and len(picked) < size:
                picked.append(groups[directory].pop(0))
    return sorted(picked)


def build_api_packet(
    preregistration: Mapping[str, Any],
    run: ArchitectureRun,
    loaded: LoadedGraph,
    sources: Mapping[str, Sequence[Row]],
    corpus_tree_sha256: str,
) -> tuple[Row, Row]:
    """(blind packet, sealed system answers). ``sources`` maps locators to stored chunks."""
    prereg_digest = require_approved(preregistration)
    if corpus_tree_sha256 != preregistration["corpus"]["tree_sha256"]:
        raise EvaluationError("SYSTEM_CORPUS_MISMATCH")
    nodes = loaded.nodes
    sites = sorted(
        (node for node in nodes.values() if node.kind == "client_call"),
        key=lambda node: (node.locator or "", int(node.attributes.get("line") or 0), node.key),
    )
    endpoints = sorted(
        (node for node in nodes.values() if node.kind == "api_endpoint"),
        key=lambda node: (node.locator or "", int(node.attributes.get("line") or 0), node.key),
    )
    imports: dict[str, set[str]] = defaultdict(set)
    for edge in loaded.edges.values():
        if (
            edge.kind in MODULE_EDGE_KINDS
            and edge.target_key
            and edge.target_key.startswith("module:")
        ):
            imports[edge.source_key.removeprefix("module:")].add(
                edge.target_key.removeprefix("module:")
            )
    site_ids = {node.key: f"S{index + 1:03d}" for index, node in enumerate(sites)}
    endpoint_ids = {node.key: f"E{index + 1:03d}" for index, node in enumerate(endpoints)}
    site_files = sorted({node.locator for node in sites if node.locator})
    definition_files = sorted(
        {target for locator in site_files for target in imports.get(locator, ())} - set(site_files)
    )
    sample = sink_sample(
        (
            node.locator
            for node in nodes.values()
            if node.kind == "module" and node.locator and node.coverage_state == "analysed"
        ),
        int(preregistration["sink_sample"]["seed"]),
        int(preregistration["sink_sample"]["size"]),
    )

    def source(locator: str) -> list[Row]:
        if locator not in sources:
            raise EvaluationError("SOURCE_NOT_STORED")
        return verified_source(sources[locator])

    def definition(locator: str, function: str) -> list[Row]:
        marker = re.compile(rf"\bdef {re.escape(function.rsplit('.', 1)[-1])}\(")
        return [chunk for chunk in source(locator) if marker.search(chunk["content"])]

    router_chunks = [
        {"locator": locator, **chunk}
        for locator in sorted(sources)
        if locator.endswith(PYTHON_SUFFIXES)
        for chunk in source(locator)
        if any(marker in chunk["content"] for marker in ROUTER_MARKERS)
    ]
    packet: Row = {
        "version": API_PACKET_VERSION,
        "preregistration_digest": prereg_digest,
        "run_id": str(run.id),
        "graph_digest": run.graph_digest,
        "corpus_tree_sha256": corpus_tree_sha256,
        "instructions": (
            "For each site, reconstruct from the source alone the request's method, URL and "
            "destination class for every statically determinable base, and name the endpoint "
            "unit(s) it would reach (or external, opaque, none). For each endpoint, write its "
            "full path(s) and method(s). For each sampled file, list the start line of every "
            "HTTP request site. Do not open the sealed answers until every label is written."
        ),
        "sites": [
            {
                "unit": site_ids[node.key],
                "locator": node.locator,
                "line": node.attributes.get("line"),
            }
            for node in sites
        ],
        "endpoints": [
            {
                "unit": endpoint_ids[node.key],
                "locator": node.locator,
                "function": node.attributes.get("function"),
                "excerpt": definition(node.locator or "", str(node.attributes.get("function"))),
            }
            for node in endpoints
        ],
        "site_sources": {locator: source(locator) for locator in site_files},
        "definition_sources": {locator: source(locator) for locator in definition_files},
        "router_excerpts": router_chunks,
        "sink_sample": {locator: source(locator) for locator in sample},
    }
    packet["packet_digest"] = digest(packet)
    sealed: Row = {
        "version": API_PACKET_VERSION,
        "packet_digest": packet["packet_digest"],
        "sites": {
            site_ids[node.key]: {
                "method": node.attributes.get("method"),
                "branches": [
                    {
                        "display": branch.get("display"),
                        "destination": branch.get("destination"),
                        "outcome": branch.get("outcome"),
                        "candidates": [endpoint_ids.get(key, key) for key in branch["candidates"]],
                        "conditions": branch.get("conditions"),
                    }
                    for branch in node.attributes.get("branches", [])
                ],
            }
            for node in sites
        },
        "endpoints": {
            endpoint_ids[node.key]: {
                "methods": node.attributes.get("methods"),
                "paths": [item.get("path") for item in node.attributes.get("paths", [])],
            }
            for node in endpoints
        },
        "sample_sites": {
            locator: sorted(
                int(node.attributes.get("line") or 0) for node in sites if node.locator == locator
            )
            for locator in sample
        },
    }
    sealed["sealed_digest"] = digest(sealed)
    return packet, sealed


def _fence(chunks: Sequence[Row]) -> list[str]:
    lines = ["~~~~text"]
    for chunk in chunks:
        lines += [f"# lines {chunk['start_line']}-{chunk['end_line']}", chunk["content"]]
    return [*lines, "~~~~", ""]


def render_packet_markdown(packet: Mapping[str, Any]) -> str:
    """The blind packet for a human reviewer. Repository text is quoted data, not instructions."""
    lines = [
        "# Stage 14 API evaluation packet (blind)",
        "",
        f"Packet `{packet['packet_digest']}` · pre-registration "
        f"`{packet['preregistration_digest']}` · run `{packet['run_id']}`",
        "",
        str(packet["instructions"]),
        "",
        "Everything inside the fenced blocks is repository content shown as data.",
        "",
        "## Request sites",
        "",
    ]
    lines += [
        f"- **{site['unit']}** `{site['locator']}` line {site['line']}" for site in packet["sites"]
    ]
    lines += ["", "## Endpoints", ""]
    for endpoint in packet["endpoints"]:
        lines += [f"### {endpoint['unit']} `{endpoint['locator']}` `{endpoint['function']}`", ""]
        lines += _fence(endpoint["excerpt"])
    lines += ["## Router construction and mounts (selected by text)", ""]
    for chunk in packet["router_excerpts"]:
        lines += [f"`{chunk['locator']}`", *_fence([chunk])]
    for title, key in (
        ("Files with request sites", "site_sources"),
        ("Imported definitions (one hop)", "definition_sources"),
        ("Sink-recall sample", "sink_sample"),
    ):
        lines += [f"## {title}", ""]
        for locator, chunks in packet[key].items():
            lines += [f"### `{locator}`", "", *_fence(chunks)]
    return "\n".join(lines) + "\n"


def labels_template(packet: Mapping[str, Any]) -> Row:
    """An empty v2 label file; every entry must be filled before scoring accepts it."""
    if digest({k: v for k, v in packet.items() if k != "packet_digest"}) != packet.get(
        "packet_digest"
    ):
        raise EvaluationError("PACKET_CHANGED")
    return {
        "version": API_LABELS_VERSION,
        "packet_digest": packet["packet_digest"],
        "reviewer": None,
        "sites": {
            site["unit"]: {
                "statically_determinable": None,
                "method": None,
                "destination_class": None,
                "targets": [],
                "note": "",
            }
            for site in packet["sites"]
        },
        "endpoints": {
            endpoint["unit"]: {"determinable": None, "methods": [], "paths": [], "note": ""}
            for endpoint in packet["endpoints"]
        },
        "sink_sample": {
            locator: {"reviewed": False, "request_lines": [], "note": ""}
            for locator in packet["sink_sample"]
        },
        "post_reveal_disagreements": {},
    }


# What decides ``overall`` for the self-hosted API suite, as frozen in the pre-registration
# (``suites.self_api``, ``sink_sample.reported``, ``zero_denominator``); reported with every
# score so the diagnostic metrics are never read as acceptance evidence.
API_VERDICT_BASIS: dict[str, Any] = {
    "gated": [
        "precision_min over request-site units",
        "recall_min over request-site units",
        "abstention_max over request-site units",
        "unbound_host_local_connections_max",
        "external_service_labels_max",
    ],
    "inconclusive_when": "the packet has no request-site units",
    "diagnostic_only": [
        "endpoints.paths_equal",
        "endpoints.methods_equal",
        "site_methods_equal",
        "sink_recall",
        "summary.coverage",
        "summary.ambiguous_set_recall",
    ],
    "precondition": "complete, valid labels for every site, endpoint and sampled file; "
    "otherwise nothing is scored",
}
UNBOUND = {"private_network_host_unbound", "public_host_unbound"}
# The destination classes ``api-matching.v3`` produces; none of them is an "external service".
DESTINATION_CLASSES = frozenset(
    {"same_origin_relative", "local_loopback", "opaque_origin", *UNBOUND}
)
BRANCH_OUTCOMES = frozenset(
    {
        "conditional_candidate", "ambiguous", "method_mismatch", "method_unknown",
        "no_candidate", "external_destination", "opaque_origin", "opaque_path",
    }
)  # fmt: skip
SITE_TARGET_WORDS = frozenset({"external", "opaque", "none"})
HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_LABEL_FIELDS = {
    "": {"version", "packet_digest", "reviewer", "sites", "endpoints", "sink_sample",
         "post_reveal_disagreements"},
    "sites": {"statically_determinable", "method", "destination_class", "targets", "note"},
    "endpoints": {"determinable", "methods", "paths", "note"},
    "sink_sample": {"reviewed", "request_lines", "note"},
}  # fmt: skip


def _unique_strings(values: Any, code: str = "LABELS_INVALID") -> list[str]:
    if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
        raise EvaluationError(code)
    if len(set(values)) != len(values):
        raise EvaluationError("LABELS_DUPLICATE_VALUE")
    return list(values)


def _entries(labels: Mapping[str, Any], section: str, expected: set[str]) -> dict[str, Row]:
    """One label section: exactly the packet's units, each an object of known fields."""
    values = labels.get(section)
    if not isinstance(values, dict):
        raise EvaluationError("LABELS_INCOMPLETE")
    if set(values) - expected:
        raise EvaluationError("LABELS_UNEXPECTED_UNIT")
    if set(values) != expected:
        raise EvaluationError("LABELS_INCOMPLETE")
    for value in values.values():
        if not isinstance(value, dict) or set(value) - _LABEL_FIELDS[section]:
            raise EvaluationError("LABELS_UNEXPECTED_FIELD")
    return values


def validate_api_labels(packet: Mapping[str, Any], labels: Mapping[str, Any]) -> None:
    """Every packet unit labelled once, in the v2 vocabulary; anything else is refused.

    Sites: ``statically_determinable`` is a boolean; a determinable site names one or more
    distinct endpoint units of this packet, or exactly one of ``external`` / ``none``; a site
    that is not determinable is labelled exactly ``["opaque"]``. Endpoints: ``determinable``
    is a boolean; a determinable endpoint has non-empty distinct methods and ``/`` paths, an
    undeterminable one has neither. Sink sample: every sampled file is ``reviewed`` with the
    distinct positive start lines of its request sites (possibly none)."""
    if labels.get("version") != API_LABELS_VERSION:
        raise EvaluationError("LABELS_VERSION")
    if labels.get("packet_digest") != packet["packet_digest"]:
        raise EvaluationError("LABELS_FOR_ANOTHER_PACKET")
    if set(labels) - _LABEL_FIELDS[""]:
        raise EvaluationError("LABELS_UNEXPECTED_FIELD")
    reviewer = labels.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise EvaluationError("LABELS_INCOMPLETE")
    endpoint_units = {endpoint["unit"] for endpoint in packet["endpoints"]}
    sites = _entries(labels, "sites", {site["unit"] for site in packet["sites"]})
    for label in sites.values():
        determinable = label.get("statically_determinable")
        if not isinstance(determinable, bool):
            raise EvaluationError("LABELS_INCOMPLETE")
        targets = _unique_strings(label.get("targets"))
        if not targets:
            raise EvaluationError("LABELS_INCOMPLETE")
        words = set(targets) & SITE_TARGET_WORDS
        if words and len(targets) != 1:
            raise EvaluationError("LABELS_INVALID")
        if not words and set(targets) - endpoint_units:
            raise EvaluationError("LABELS_UNKNOWN_ENDPOINT")
        if determinable == (targets == ["opaque"]):
            raise EvaluationError("LABELS_INVALID")
        method = label.get("method")
        if method is not None and (not isinstance(method, str) or not method):
            raise EvaluationError("LABELS_INVALID")
        destination = label.get("destination_class")
        if destination is not None and destination not in DESTINATION_CLASSES:
            raise EvaluationError("LABELS_INVALID")
    for label in _entries(labels, "endpoints", endpoint_units).values():
        determinable = label.get("determinable")
        if not isinstance(determinable, bool):
            raise EvaluationError("LABELS_INCOMPLETE")
        methods = _unique_strings(label.get("methods"))
        paths = _unique_strings(label.get("paths"))
        if determinable != bool(methods) or determinable != bool(paths):
            raise EvaluationError("LABELS_INCOMPLETE" if determinable else "LABELS_INVALID")
        if set(methods) - HTTP_METHODS or any(not path.startswith("/") for path in paths):
            raise EvaluationError("LABELS_INVALID")
    for label in _entries(labels, "sink_sample", set(packet["sink_sample"])).values():
        if label.get("reviewed") is not True:
            raise EvaluationError("LABELS_INCOMPLETE")
        lines = label.get("request_lines")
        if not isinstance(lines, list) or not all(
            isinstance(line, int) and not isinstance(line, bool) and line > 0 for line in lines
        ):
            raise EvaluationError("LABELS_INVALID")
        if len(set(lines)) != len(lines):
            raise EvaluationError("LABELS_DUPLICATE_VALUE")
    if not isinstance(labels.get("post_reveal_disagreements", {}), dict):
        raise EvaluationError("LABELS_INVALID")


def _verify_packet_and_sealed(
    preregistration: Mapping[str, Any], packet: Mapping[str, Any], sealed: Mapping[str, Any]
) -> str:
    prereg_digest = require_approved(preregistration)
    body = {key: value for key, value in packet.items() if key != "packet_digest"}
    if (
        packet.get("version") != API_PACKET_VERSION
        or digest(body) != packet.get("packet_digest")
        or packet.get("preregistration_digest") != prereg_digest
    ):
        raise EvaluationError("PACKET_CHANGED")
    if packet.get("corpus_tree_sha256") != preregistration["corpus"]["tree_sha256"]:
        raise EvaluationError("SYSTEM_CORPUS_MISMATCH")
    sealed_body = {key: value for key, value in sealed.items() if key != "sealed_digest"}
    if (
        digest(sealed_body) != sealed.get("sealed_digest")
        or sealed.get("packet_digest") != packet["packet_digest"]
    ):
        raise EvaluationError("SEALED_CHANGED")
    endpoint_units = {endpoint["unit"] for endpoint in packet["endpoints"]}
    if (
        set(sealed.get("sites") or {}) != {site["unit"] for site in packet["sites"]}
        or set(sealed.get("endpoints") or {}) != endpoint_units
        or set(sealed.get("sample_sites") or {}) != set(packet["sink_sample"])
    ):
        raise EvaluationError("SEALED_INVALID")
    for answer in sealed["sites"].values():
        for branch in answer.get("branches") or []:
            if branch.get("outcome") not in BRANCH_OUTCOMES or (
                set(branch.get("candidates") or []) - endpoint_units
            ):
                raise EvaluationError("SEALED_INVALID")
    return prereg_digest


def _system_site(answer: Mapping[str, Any]) -> Row:
    """The system's site-level claim: one candidate endpoint, external, or no claim."""
    branches = list(answer.get("branches") or [])
    candidates = sorted(
        {
            candidate
            for branch in branches
            if branch["outcome"] == "conditional_candidate"
            for candidate in branch["candidates"]
        }
    )
    if len(candidates) == 1:
        return {"label": "module", "targets": candidates}
    if not candidates and any(b["outcome"] == "external_destination" for b in branches):
        return {"label": "external", "targets": []}
    ambiguous = sorted(
        {
            candidate
            for branch in branches
            if branch["outcome"] in {"ambiguous", "method_mismatch", "method_unknown"}
            for candidate in branch["candidates"]
        }
    )
    return {"label": "unknown", "targets": [], "candidates": candidates or ambiguous}


def _oracle_site(label: Mapping[str, Any]) -> Row:
    targets = list(label["targets"])
    if targets == ["opaque"]:
        return {"label": "unknown", "targets": []}
    if targets == ["none"]:
        return {"label": "none", "targets": []}
    if targets == ["external"]:
        return {"label": "external", "targets": []}
    return {"label": "module", "targets": sorted(targets)}


def score_api(
    preregistration: Mapping[str, Any],
    packet: Mapping[str, Any],
    sealed: Mapping[str, Any],
    labels: Mapping[str, Any],
) -> Row:
    """Mechanical scoring of complete, validated human labels against the sealed answers."""
    prereg_digest = _verify_packet_and_sealed(preregistration, packet, sealed)
    validate_api_labels(packet, labels)
    units: list[Row] = []
    unbound_local: set[str] = set()
    external_service = 0
    methods_compared = methods_equal = 0
    for unit, label in sorted(labels["sites"].items()):
        answer = sealed["sites"][unit]
        branches = list(answer.get("branches") or [])
        external_service += sum(
            1 for branch in branches if branch.get("destination") not in DESTINATION_CLASSES
        )
        oracle = _oracle_site(label)
        system = _system_site(answer)
        if any(
            branch.get("destination") in UNBOUND and branch["outcome"] == "conditional_candidate"
            for branch in branches
        ) or (oracle["label"] == "external" and system["label"] == "module"):
            unbound_local.add(unit)
        if label["statically_determinable"] and label.get("method"):
            methods_compared += 1
            methods_equal += int(label["method"] == answer.get("method"))
        units.append(
            {
                "unit": unit,
                "oracle": oracle,
                "system": system,
                "outcome": classify(oracle, system, True),
            }
        )
    summary = summarise(units)
    endpoint_rows = []
    for unit, label in sorted(labels["endpoints"].items()):
        answer = sealed["endpoints"][unit]
        row: Row = {"unit": unit, "determinable": label["determinable"]}
        if label["determinable"]:
            row["paths_equal"] = sorted(label["paths"]) == sorted(
                path for path in answer.get("paths") or [] if path
            )
            row["methods_equal"] = sorted(label["methods"]) == sorted(answer.get("methods") or [])
        endpoint_rows.append(row)
    determinable = [row for row in endpoint_rows if row["determinable"]]
    marked = {
        locator: set(value["request_lines"]) for locator, value in labels["sink_sample"].items()
    }
    found = sum(
        len(marked[locator] & set(lines)) for locator, lines in sealed["sample_sites"].items()
    )
    human = sum(len(lines) for lines in marked.values())
    extra = {
        locator: sorted(set(lines) - marked[locator])
        for locator, lines in sealed["sample_sites"].items()
        if set(lines) - marked[locator]
    }
    gates = {
        "unbound_host_local_connections": len(unbound_local),
        "external_service_labels": external_service,
    }
    rows = criteria(preregistration["suites"]["self_api"], summary, gates)
    return {
        "version": SCORE_VERSION,
        "suite": "self_api",
        "preregistration_digest": prereg_digest,
        "packet_digest": packet["packet_digest"],
        "sealed_digest": sealed["sealed_digest"],
        "labels_digest": digest(dict(labels)),
        "reviewer": labels["reviewer"],
        "summary": summary,
        "criteria": rows,
        "gates": {**gates, "unbound_host_local_connection_sites": sorted(unbound_local)},
        "overall": verdict(rows, {"sites": summary}),
        "verdict_basis": API_VERDICT_BASIS,
        "units": units,
        "site_methods_equal": metric(methods_equal, methods_compared),
        "endpoints": {
            "units": len(endpoint_rows),
            "determinable": len(determinable),
            "paths_equal": metric(
                sum(row["paths_equal"] for row in determinable), len(determinable)
            ),
            "methods_equal": metric(
                sum(row["methods_equal"] for row in determinable), len(determinable)
            ),
            "rows": endpoint_rows,
        },
        "sink_recall": {**metric(found, human), "system_sites_not_marked": extra},
    }


# --------------------------------------------------------------------------- service


class EvaluationStore(Protocol):
    def evidence(self, run_id: UUID, digests: Sequence[str]) -> list[ArchitectureEvidence]: ...
    def statement_rows(self, structural_analysis_id: UUID) -> dict[str, Row]: ...
    def source_chunks(
        self, catalog_run_id: UUID, locators: Sequence[str]
    ) -> dict[str, list[Row]]: ...


class ArchitectureEvaluationService:
    """Reads the verified run for the evaluation; never writes and never reads files."""

    def __init__(self, architecture: ArchitectureService, store: EvaluationStore) -> None:
        self._architecture = architecture
        self._store = store

    def module_units(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None, corpus_tree_sha256: str
    ) -> Row:
        run, reasons, loaded = self._architecture.graph(project_id, snapshot_id, run_id)
        edges = [edge for edge in loaded.edges.values() if edge.kind in MODULE_EDGE_KINDS]
        self._architecture.verify_assertion(
            run,
            loaded,
            Assertion(frozenset(), frozenset(edge.key for edge in edges)),
        )
        digests = sorted(
            {
                link.digest
                for edge in edges
                for link in edge.evidence
                if link.source == "architecture"
            }
        )
        evidence = {item.digest: item for item in self._store.evidence(run.id, digests)}
        units, coverage = system_module_units(
            loaded,
            statement_refs(loaded, evidence),
            self._store.statement_rows(run.structural_analysis_id),
        )
        return {
            "version": MODULE_UNITS_VERSION,
            "run_id": str(run.id),
            "graph_digest": run.graph_digest,
            "portable_digest": run.portable_digest,
            "freshness_reasons": list(reasons),
            "corpus_tree_sha256": corpus_tree_sha256,
            "coverage": coverage,
            "units": units,
        }

    def api_packet(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        preregistration: Mapping[str, Any],
        corpus_tree_sha256: str,
    ) -> tuple[Row, Row]:
        run, _, loaded = self._architecture.graph(project_id, snapshot_id, run_id)
        asserted = [
            node.key
            for node in loaded.nodes.values()
            if node.kind in {"client_call", "api_endpoint"}
        ]
        self._architecture.verify_assertion(
            run, loaded, Assertion(frozenset(asserted), frozenset())
        )
        locators = sorted(
            node.locator
            for node in loaded.nodes.values()
            if node.kind == "module" and node.locator and node.coverage_state == "analysed"
        )
        sources = self._store.source_chunks(run.catalog_run_id, locators)
        return build_api_packet(preregistration, run, loaded, sources, corpus_tree_sha256)
