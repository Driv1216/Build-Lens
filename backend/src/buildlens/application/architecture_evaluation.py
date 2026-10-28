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
API_LABELS_VERSION = "architecture-api-labels.v1"
SCORE_VERSION = "architecture-evaluation-score.v1"
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
    """Counts and metrics over scored units (each with ``outcome`` and ``oracle``)."""
    counts = Counter(unit["outcome"] for unit in units)
    eligible = [unit for unit in units if unit["outcome"] != "ineligible"]
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
        "units": len(units),
        "eligible": len(eligible),
        "oracle_positive": len(positive),
        "outcomes": {name: counts[name] for name in OUTCOMES},
        "precision": metric(correct, emitted),
        "recall": metric(correct, len(positive)),
        "abstention_rate": metric(len(abstained), len(positive)),
        "coverage": metric(len(eligible), len(units)),
        "ambiguous_set_recall": metric(len(in_set), len(with_sets)),
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
    extra = [
        {"unit": unit["unit"], "locator": unit["locator"], "system": unit["system"]}
        for unit in by_unit.values()
    ]
    disagreements = [
        {**unit, "explanation": explanations.get(unit["unit"])}
        for unit in scored
        if unit["outcome"] in {"emitted_incorrect", "abstained"}
    ] + [
        {**unit, "outcome": "system_only", "explanation": explanations.get(unit["unit"])}
        for unit in extra
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
    negatives = sum(
        1
        for unit in scored
        if unit["oracle"]["label"] not in {"module", "external"}
        and unit["system"]["label"] in {"module", "external"}
    ) + sum(1 for unit in by_unit.values() if unit["system"]["label"] in {"module", "external"})
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
            endpoint["unit"]: {"methods": [], "paths": [], "note": ""}
            for endpoint in packet["endpoints"]
        },
        "sink_sample": {locator: [] for locator in packet["sink_sample"]},
        "post_reveal_disagreements": {},
    }


UNBOUND = {"private_network_host_unbound", "public_host_unbound"}


def score_api(
    preregistration: Mapping[str, Any],
    packet: Mapping[str, Any],
    sealed: Mapping[str, Any],
    labels: Mapping[str, Any],
) -> Row:
    """Mechanical scoring of the human labels against the sealed system answers."""
    prereg_digest = require_approved(preregistration)
    body = {key: value for key, value in packet.items() if key != "packet_digest"}
    if (
        digest(body) != packet.get("packet_digest")
        or packet.get("preregistration_digest") != prereg_digest
    ):
        raise EvaluationError("PACKET_CHANGED")
    sealed_body = {key: value for key, value in sealed.items() if key != "sealed_digest"}
    if (
        digest(sealed_body) != sealed.get("sealed_digest")
        or sealed.get("packet_digest") != packet["packet_digest"]
    ):
        raise EvaluationError("SEALED_CHANGED")
    if (
        labels.get("version") != API_LABELS_VERSION
        or labels.get("packet_digest") != packet["packet_digest"]
    ):
        raise EvaluationError("LABELS_FOR_ANOTHER_PACKET")
    if not labels.get("reviewer"):
        raise EvaluationError("LABELS_INCOMPLETE")
    site_labels = labels.get("sites") or {}
    if set(site_labels) != {site["unit"] for site in packet["sites"]} or any(
        not isinstance(value.get("statically_determinable"), bool) for value in site_labels.values()
    ):
        raise EvaluationError("LABELS_INCOMPLETE")
    units: list[Row] = []
    unbound_local = 0
    for unit, label in sorted(site_labels.items()):
        answer = sealed["sites"][unit]
        candidates = sorted(
            {
                candidate
                for branch in answer["branches"]
                if branch["outcome"] == "conditional_candidate"
                for candidate in branch["candidates"]
            }
        )
        ambiguous = sorted(
            {
                candidate
                for branch in answer["branches"]
                if branch["outcome"] == "ambiguous"
                for candidate in branch["candidates"]
            }
        )
        unbound_local += sum(
            1
            for branch in answer["branches"]
            if branch.get("destination") in UNBOUND and branch["outcome"] == "conditional_candidate"
        )
        targets = [target for target in label.get("targets") or [] if str(target).startswith("E")]
        oracle = (
            {"label": "module", "targets": targets}
            if label["statically_determinable"] and targets
            else {"label": "none", "targets": []}
        )
        system = (
            {"label": "module", "targets": candidates}
            if len(candidates) == 1
            else {"label": "unknown", "targets": [], "candidates": candidates or ambiguous}
        )
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
    for unit, label in sorted((labels.get("endpoints") or {}).items()):
        answer = sealed["endpoints"].get(unit, {})
        endpoint_rows.append(
            {
                "unit": unit,
                "paths_equal": sorted(label.get("paths") or [])
                == sorted(p for p in answer.get("paths", []) if p),
                "methods_equal": sorted(label.get("methods") or [])
                == sorted(answer.get("methods") or []),
            }
        )
    marked = labels.get("sink_sample") or {}
    found = sum(
        len(set(marked.get(locator) or []) & set(lines))
        for locator, lines in sealed["sample_sites"].items()
    )
    human = sum(len(set(lines)) for lines in marked.values())
    extra = {
        locator: sorted(set(lines) - set(marked.get(locator) or []))
        for locator, lines in sealed["sample_sites"].items()
        if set(lines) - set(marked.get(locator) or [])
    }
    rows = criteria(
        preregistration["suites"]["self_api"],
        summary,
        {"unbound_host_local_connections": unbound_local, "external_service_labels": 0},
    )
    return {
        "version": SCORE_VERSION,
        "suite": "self_api",
        "preregistration_digest": prereg_digest,
        "packet_digest": packet["packet_digest"],
        "labels_digest": digest(dict(labels)),
        "reviewer": labels["reviewer"],
        "summary": summary,
        "criteria": rows,
        "overall": verdict(rows, {"sites": summary}),
        "units": units,
        "endpoints": {
            "units": len(endpoint_rows),
            "paths_equal": metric(
                sum(row["paths_equal"] for row in endpoint_rows), len(endpoint_rows)
            ),
            "methods_equal": metric(
                sum(row["methods_equal"] for row in endpoint_rows), len(endpoint_rows)
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
