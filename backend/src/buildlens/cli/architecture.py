"""Architecture model commands (same services and verification as the HTTP API)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import typer

from buildlens.application.architecture import ArchitectureService, Assertion
from buildlens.application.architecture_queries import ArchitectureQueries
from buildlens.application.architecture_rules import ArchitectureRulesService
from buildlens.core.config import get_settings
from buildlens.domain.architecture import CLASS_SETS, DEFAULT_CLASS_SET, MODULE_EDGE_KINDS
from buildlens.infrastructure.runtime import (
    LocalRuntime,
    architecture_rules_service,
    architecture_service,
)

app = typer.Typer(help="Deterministic, evidence-backed architecture model (no AI text).")


def _emit(value: Any) -> None:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


@contextmanager
def _runtime() -> Iterator[LocalRuntime]:
    runtime = LocalRuntime(get_settings())
    try:
        yield runtime
    finally:
        asyncio.run(runtime.close())


def _service(call: Callable[[ArchitectureService], Any]) -> None:
    with _runtime() as runtime:
        _emit(call(architecture_service(runtime)))


def _queries(call: Callable[[ArchitectureQueries], Any]) -> None:
    with _runtime() as runtime:
        _emit(call(ArchitectureQueries(architecture_service(runtime))))


@app.command("build")
def build(project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)) -> None:
    """Build (or reuse) the architecture model from the active semantic catalog."""
    _service(lambda service: service.build(project, snapshot))


@app.command("status")
def status(project: UUID = typer.Option(...), snapshot: UUID = typer.Option(...)) -> None:
    """Show the latest attempt, the active run and its freshness."""
    _service(lambda service: service.status(project, snapshot))


@app.command("overview")
def overview(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Workspaces, element counts, cycles, diagnostics and coverage (verified)."""
    _queries(lambda queries: queries.overview(project, snapshot, run))


@app.command("deps")
def deps(
    node: str = typer.Argument(..., help="A module, container or workspace key."),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    direction: str = typer.Option("out", help="out (dependencies) or in (dependants)."),
    class_set: str = typer.Option(DEFAULT_CLASS_SET, "--class-set"),
    depth: int = typer.Option(32),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Transitive dependencies with witness paths and the gaps that bound the answer."""
    _queries(
        lambda queries: queries.dependencies(
            project,
            snapshot,
            run,
            node,
            direction="in" if direction == "in" else "out",
            class_set=class_set,
            depth=depth,
        )
    )


@app.command("path")
def path(
    source: str = typer.Argument(...),
    target: str = typer.Argument(...),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    class_set: str = typer.Option(DEFAULT_CLASS_SET, "--class-set"),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Shortest witness path between two modules, or why none is claimed."""
    _queries(
        lambda queries: queries.path(project, snapshot, run, source, target, class_set=class_set)
    )


@app.command("cycles")
def cycles(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    level: str | None = typer.Option(None, help="module or container"),
    class_set: str | None = typer.Option(None, "--class-set"),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Import cycles and aggregate dependency cycles with witness cycles."""
    _queries(
        lambda queries: queries.cycles(
            project, snapshot, run, level=level, class_set=class_set, limit=500
        )
    )


@app.command("diagnostics")
def diagnostics(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    code: str | None = typer.Option(None),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Evidence-linked diagnostics, optionally filtered by code."""
    _queries(lambda queries: queries.diagnostics(project, snapshot, run, code=code, limit=500))


@app.command("api")
def api(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    outcome: str | None = typer.Option(None, help="Only request sites with this branch outcome."),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Endpoints, request sites and conditional API connections (never confirmed matches)."""
    _queries(lambda queries: queries.api(project, snapshot, run, outcome=outcome, limit=500))


@app.command("view")
def view(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    name: str = typer.Option(..., help="integrations | database | frontend | subsystems"),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """One Milestone C view with its coverage and diagnostics."""
    _queries(lambda queries: queries.view(project, snapshot, run, name))


@app.command("coverage")
def coverage(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """What the model covers, including alias and guard-context gaps."""
    _queries(lambda queries: queries.coverage(project, snapshot, run)[2])


@app.command("verify")
def verify(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Full non-authoritative integrity inspection: structure plus every evidence item."""
    _service(lambda service: service.integrity(project, snapshot, run))


@app.command("export")
def export(
    out: Path = typer.Option(..., help="Destination file (written atomically)."),
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    format: Literal["json", "dot"] = typer.Option("json"),
    class_set: str = typer.Option(DEFAULT_CLASS_SET, "--class-set"),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Write the verified module graph (resolved internal edges) as JSON or Graphviz DOT."""
    if class_set not in CLASS_SETS:
        raise typer.BadParameter(f"Unknown class set {class_set!r}.")
    with _runtime() as runtime:
        service = architecture_service(runtime)
        value, reasons, loaded = service.graph(project, snapshot, run)
        edges = sorted(
            (
                edge
                for edge in loaded.edges.values()
                if edge.kind in MODULE_EDGE_KINDS
                and edge.target_key is not None
                and edge.target_key.startswith("module:")
                and edge.import_class in CLASS_SETS[class_set]
            ),
            key=lambda edge: edge.key,
        )
        modules = sorted(key for key, node in loaded.nodes.items() if node.kind == "module")
        service.verify_assertion(
            value, loaded, Assertion(frozenset(modules), frozenset(edge.key for edge in edges))
        )
        if format == "dot":
            lines = [f"// BuildLens architecture run {value.id} class set {class_set}", "digraph {"]
            lines += [f"  {json.dumps(key)};" for key in modules]
            lines += [
                f"  {json.dumps(e.source_key)} -> {json.dumps(e.target_key)}"
                f" [label={json.dumps(str(e.import_class))}];"
                for e in edges
            ]
            content = "\n".join([*lines, "}"]) + "\n"
        else:
            content = json.dumps(
                {
                    "run_id": str(value.id),
                    "portable_digest": value.portable_digest,
                    "freshness_reasons": list(reasons),
                    "class_set": class_set,
                    "modules": modules,
                    "edges": [
                        {
                            "key": e.key,
                            "source": e.source_key,
                            "target": e.target_key,
                            "class": e.import_class,
                            "count": e.count,
                        }
                        for e in edges
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=out.parent, prefix=".architecture-")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, out)
        _emit({"written": str(out), "modules": len(modules), "edges": len(edges)})


rules_app = typer.Typer(help="Owner-declared architecture rules (intent) and conformance checks.")
app.add_typer(rules_app, name="rules")


def _rules(call: Callable[[ArchitectureRulesService], Any]) -> None:
    with _runtime() as runtime:
        _emit(call(architecture_rules_service(runtime)))


@rules_app.command("set")
def rules_set(
    project: UUID = typer.Option(...),
    file: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Declare a new immutable rule-set version from a local JSON file you wrote."""
    payload = json.loads(file.read_text(encoding="utf-8"))
    rules = payload.get("rules") if isinstance(payload, dict) else payload
    _rules(lambda service: service.declare(project, rules))


@rules_app.command("show")
def rules_show(project: UUID = typer.Option(...), version: int | None = typer.Option(None)) -> None:
    """The latest (or a given) declared rule-set version."""
    _rules(lambda service: service.rule_set(project, version))


@rules_app.command("check")
def rules_check(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    version: int | None = typer.Option(None),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Conformance of one rule-set version against the verified architecture (no enforcement)."""
    _rules(lambda service: service.evaluate(project, snapshot, run, version))


eval_app = typer.Typer(
    help="Pre-registered evaluation (architecture-evaluation.v1): exports, packets, scores."
)
app.add_typer(eval_app, name="eval")


def _write_once(path: Path, value: Any, *, text: bool = False) -> None:
    from buildlens.infrastructure.evaluation.local import atomic_write

    atomic_write(path, value, text=text)
    typer.echo(f"wrote {path}")


def _read_json(path: Path) -> Any:
    from buildlens.infrastructure.evaluation.local import read_json

    return read_json(path)


@eval_app.command("preregister")
def eval_preregister(out: Path = typer.Option(..., "--out")) -> None:
    """Write a draft pre-registration; the owner approves it before any scored run."""
    from buildlens.application.architecture_evaluation import preregistration_template

    _write_once(out, preregistration_template())


@eval_app.command("module-units")
def eval_module_units(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    corpus_tree_sha256: str = typer.Option(..., "--corpus-tree-sha256"),
    out: Path = typer.Option(..., "--out"),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Export the verified run's per-unit module-edge output (write-once)."""
    from buildlens.infrastructure.runtime import architecture_evaluation_service

    with _runtime() as runtime:
        value = architecture_evaluation_service(runtime).module_units(
            project, snapshot, run, corpus_tree_sha256
        )
    _write_once(out, value)
    _emit({"run_id": value["run_id"], "units": len(value["units"])})


@eval_app.command("module-score")
def eval_module_score(
    preregistration: Path = typer.Option(..., "--preregistration"),
    oracle: Path = typer.Option(..., "--oracle"),
    system: Path = typer.Option(..., "--system"),
    out: Path = typer.Option(..., "--out"),
    explanations: Path | None = typer.Option(None, "--explanations"),
) -> None:
    """Score module units against the differential oracle (mechanical; no judging)."""
    from buildlens.application.architecture_evaluation import score_module_units

    notes = _read_json(explanations) if explanations else None
    result = score_module_units(
        _read_json(preregistration),
        _read_json(oracle),
        _read_json(system),
        notes.get("units", notes) if isinstance(notes, dict) else None,
    )
    _write_once(out, result)
    _emit({key: result[key] for key in ("overall", "criteria", "unexplained", "system_defects")})


@eval_app.command("api-packet")
def eval_api_packet(
    project: UUID = typer.Option(...),
    snapshot: UUID = typer.Option(...),
    preregistration: Path = typer.Option(..., "--preregistration"),
    corpus_tree_sha256: str = typer.Option(..., "--corpus-tree-sha256"),
    out: Path = typer.Option(..., "--out", help="Directory for the blind packet."),
    sealed: Path = typer.Option(..., "--sealed", help="File for the sealed system answers."),
    run: UUID | None = typer.Option(None, "--run"),
) -> None:
    """Blind API packet (verified stored source only) and the sealed system answers."""
    from buildlens.application.architecture_evaluation import (
        labels_template,
        render_packet_markdown,
    )
    from buildlens.infrastructure.runtime import architecture_evaluation_service

    with _runtime() as runtime:
        packet, answers = architecture_evaluation_service(runtime).api_packet(
            project, snapshot, run, _read_json(preregistration), corpus_tree_sha256
        )
    _write_once(out / "packet.json", packet)
    _write_once(out / "REVIEW.md", render_packet_markdown(packet), text=True)
    _write_once(out / "labels.template.json", labels_template(packet))
    _write_once(sealed, answers)
    _emit({"sites": len(packet["sites"]), "endpoints": len(packet["endpoints"])})


@eval_app.command("labels-template")
def eval_labels_template(
    packet: Path = typer.Option(..., "--packet"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    """Write an empty label file for an existing packet (reads the blind packet only)."""
    from buildlens.application.architecture_evaluation import labels_template

    _write_once(out, labels_template(_read_json(packet)))


@eval_app.command("api-score")
def eval_api_score(
    preregistration: Path = typer.Option(..., "--preregistration"),
    packet: Path = typer.Option(..., "--packet"),
    sealed: Path = typer.Option(..., "--sealed"),
    labels: Path = typer.Option(..., "--labels"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    """Score human labels against the sealed answers (refuses changed packets or labels)."""
    from buildlens.application.architecture_evaluation import score_api

    result = score_api(
        _read_json(preregistration), _read_json(packet), _read_json(sealed), _read_json(labels)
    )
    _write_once(out, result)
    _emit({key: result[key] for key in ("overall", "criteria", "sink_recall")})
