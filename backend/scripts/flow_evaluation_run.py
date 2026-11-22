"""Part F disposable-database execution. Never imports or executes benchmark source."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import shutil
import signal
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from alembic import command
from buildlens.application.architecture import ArchitectureService
from buildlens.application.flow import FlowService
from buildlens.application.flow_evaluation import (
    FACETS,
    SYSTEM_VERSION,
    bound,
    latency_summary,
    require_preregistration,
    validate_labels,
)
from buildlens.application.flow_evidence import FlowEvidenceService
from buildlens.core.config import Settings
from buildlens.domain.flow_trace import TraceQuery
from buildlens.infrastructure.persistence.architecture import SqlAlchemyArchitectureStore
from buildlens.infrastructure.persistence.flow import SqlAlchemyFlowStore
from buildlens.infrastructure.persistence.flow_evidence import SqlAlchemyFlowEvidenceStore
from buildlens.infrastructure.runtime import LocalRuntime, flow_service, flow_trace_service
from buildlens.infrastructure.structural.supervisor import SupervisedSourceReader
from tests.semantic_support import CatalogStack

if __package__:
    from .flow_evaluate import BACKEND, files, load, save, target_id
else:
    from flow_evaluate import BACKEND, files, load, save, target_id


def build(directory: Path, corpus_name: str, admin_url: str) -> None:
    prereg = load(directory / "preregistration.json")
    require_preregistration(prereg)
    spec = next(c for c in prereg["corpora"] if c["name"] == corpus_name)
    original_root = Path(spec["root"])
    if files(original_root) != spec["files"]:
        raise ValueError("Corpus changed after registration")
    # Evaluation directories are intentionally excluded by the production inventory policy.
    # Relocate identical bytes, rather than weakening that policy for benchmark ingestion.
    root = Path("/private/tmp/buildlens-f-corpora") / corpus_name
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(original_root, root, symlinks=True)
    if files(root) != spec["files"]:
        raise ValueError("Relocated corpus bytes differ")
    name = f"buildlens_f_{corpus_name.replace('-', '_')}_{uuid4().hex[:8]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        from sqlalchemy import text

        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    # Persist ownership immediately; failures never orphan unidentifiable databases.
    save(
        directory / f"database.{corpus_name}.json",
        {"name": name, "url": url, "owned_by": "stage15-f"},
    )
    os.environ["BUILDLENS_DATABASE_URL"] = url
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(config, "head")
    engine = create_engine(url)
    measurements: dict[str, Any] = {
        "corpus": corpus_name,
        "tree_digest": spec["tree_digest"],
        "preregistration_digest": prereg["digest"],
        "limits": "Production defaults; pipeline wall deadlines 600 s. "
        "Artifact/byte/chunk caps unchanged; partial coverage retained.",
    }

    def timed(phase: str, fn: Any) -> Any:
        start = time.perf_counter()
        value = fn()
        measurements[phase] = {
            "seconds": time.perf_counter() - start,
            "outcome": getattr(value, "outcome", None),
        }
        print(
            json.dumps({"corpus": corpus_name, "phase": phase, **measurements[phase]}), flush=True
        )
        return value

    try:
        stack = CatalogStack(engine, root, f"Stage 15 F {corpus_name}")
        # Wall-clock allowance for a supervised lab build; no semantic/size caps changed.
        stack.structural._overall_timeout_seconds = 600
        stack.processing._overall_timeout_seconds = 600
        project = stack.project.id
        snapshot = timed("snapshot", lambda: stack.snapshots.create(project))
        timed("structure", lambda: stack.structural.analyse(project, snapshot.id))
        timed("processing", lambda: stack.processing.process(project, snapshot.id))
        timed("catalog", lambda: stack.catalog.build(project, snapshot.id))
        architecture = ArchitectureService(
            SqlAlchemyArchitectureStore(stack.factory), stack.catalog
        )
        timed("architecture", lambda: architecture.build(project, snapshot.id))
        evidence = FlowEvidenceService(
            SqlAlchemyFlowEvidenceStore(stack.factory),
            SupervisedSourceReader,
            overall_timeout_seconds=600,
        )
        timed("evidence", lambda: evidence.extract(project, snapshot.id))
        store = SqlAlchemyFlowStore(stack.factory)
        flow = FlowService(store, evidence, stack.catalog, architecture)
        run = timed("model", lambda: flow.build(project, snapshot.id))
        measurements.update(
            {
                "project": str(project),
                "snapshot": str(snapshot.id),
                "run": str(run.id),
                "coverage": run.coverage,
                "graph_digest": run.graph_digest,
                "portable_digest": run.portable_digest,
            }
        )
        measurements["integrity"] = timed(
            "integrity_time", lambda: flow.integrity(project, snapshot.id, None)
        )
    except Exception as error:
        measurements["error"] = {
            "class": type(error).__name__,
            "code": getattr(error, "code", None),
        }
        raise
    finally:
        save(directory / f"build.{corpus_name}.json", measurements)
        engine.dispose()
        admin.dispose()


def runtime_for(directory: Path, name: str) -> tuple[LocalRuntime, dict[str, Any]]:
    info = load(directory / f"build.{name}.json")
    # Early report recovery used the phase name 'snapshot' for its wall-time field.
    # Normalize locally; retained evidence files are never rewritten.
    if isinstance(info.get("snapshot"), dict):
        info["snapshot"] = info["integrity"]["run"]["project_version_id"]
    url = load(directory / f"database.{name}.json")["url"]
    runtime = LocalRuntime(
        Settings(
            _env_file=None,
            database_url=url,
            ask_enabled=False,
            semantic_generation_enabled=False,
            lab_generation_enabled=False,
        )
    )
    return runtime, info


def node_target(node: Any, functions: list[dict[str, Any]], positions: dict[str, int]) -> str:
    line = positions.get(node.key)
    matches = [
        f
        for f in functions
        if f["locator"] == node.locator
        and f["name"] == node.label
        and (line is None or f["line"] <= line <= f.get("end_line", f["line"]))
    ]
    if not matches:
        matches = [f for f in functions if f["locator"] == node.locator and f["name"] == node.label]
    if not matches and line is not None:
        matches = [f for f in functions if f["locator"] == node.locator and f["line"] == line]
    if len(matches) == 1:
        return target_id(matches[0])
    if matches and line is not None:
        return target_id(min(matches, key=lambda f: abs(f["line"] - line)))
    return f"{node.locator}:{line}:{node.label}"


def project_trace(
    trace: dict[str, Any],
    nodes: dict[str, Any],
    functions: list[dict[str, Any]],
    positions: dict[str, int],
) -> dict[str, Any]:
    """Source-addressed whole-walk projection, preserving order, alternatives and unknowns.

    Framework synthetic scopes remain in the trace artifact but are not source scope visits.
    All guard/response/async/effect facets are compared separately; no output is removed to
    accommodate a label. This projection is fixed before the first observation.
    """
    result: dict[str, Any] = {facet: [] for facet in FACETS}
    hops = {h["id"]: h for h in trace["hops"]}
    address = {
        h["id"]: node_target(nodes[h["node"]], functions, positions)
        for h in trace["hops"]
        if h["node"] in nodes and h["node"].startswith("scope:")
    }
    result["path"] = [address[h["id"]] for h in trace["hops"] if h["id"] in address]
    for step in trace["steps"]:
        target = address.get(step["to"])
        if target:
            result["tiers"].append([target, step["resolution"]["tier"]])
        for term in step.get("path_condition", {}).get("terms", []):
            for atom in term:
                result["guards"].append(
                    [
                        target or hops.get(step["to"], {}).get("label"),
                        atom.get("op"),
                        atom.get("value"),
                    ]
                )
        attrs = step.get("attributes") or {}
        if step["relation"] in {"exits_with", "raises_to"}:
            result["responses"].append([attrs.get("status"), attrs.get("origin")])
        if step["relation"] == "responds":
            result["continuations"].append([attrs.get("status"), attrs.get("continuation")])
        if step["relation"] in {
            "registers",
            "awaits",
            "creates_coroutine",
            "creates_generator",
            "creates_async_generator",
            "resumes",
            "throws_into",
            "iterates",
            "continues_with",
            "invokes_until_first_await",
            "may_rerun_effect",
        }:
            result["async"].append(
                [step["relation"], target or hops.get(step["to"], {}).get("label"), step["lane"]]
            )
        if step["relation"] == "unresolved":
            result["unknowns"].append(hops.get(step["from"], {}).get("label"))
    result["effects"] = [
        {k: e.get(k) for k in ("class", "state", "tables", "commit_position")}
        for e in trace.get("effects", [])
    ]
    result["trace_status"] = trace["status"]
    result["trace_digest"] = trace["trace_digest"]
    return result


def observe(directory: Path, only: str | None = None, revision: str = "") -> None:
    prereg, packet, labels, index = [
        load(directory / f)
        for f in ("preregistration.json", "packet.json", "labels.json", "source-index.json")
    ]
    require_preregistration(prereg)
    validate_labels(packet, labels)
    output: dict[str, Any] = {
        "version": SYSTEM_VERSION,
        "packet_digest": packet["digest"],
        "labels_digest": labels["digest"],
        "calls": [],
        "cases": [],
        "queries": [],
    }
    supplement_path = directory / "source-positions.json"
    supplement = load(supplement_path) if supplement_path.exists() else None
    if supplement and (
        supplement["packet_digest"] != packet["digest"]
        or supplement.get("digest") != bound(supplement)["digest"]
    ):
        raise ValueError("Source-position supplement changed or belongs to another packet")

    def call_position(call: dict[str, Any]) -> tuple[Any, ...]:
        end = (
            supplement["positions"][call["id"]]
            if supplement
            else [call["end_line"], call["end_column"]]
        )
        return call["locator"], call["line"], call["column"], *end

    raw = directory / ("traces" + revision)
    raw.mkdir(exist_ok=True)
    for spec in prereg["corpora"]:
        name = spec["name"]
        if only is not None and name != only:
            continue
        runtime, info = runtime_for(directory, name)
        project, snapshot, run_id = (UUID(info[k]) for k in ("project", "snapshot", "run"))
        try:
            store = SqlAlchemyFlowStore(runtime.session_factory)
            nodes, edges = store.graph(run_id)
            functions = index[name]["functions"]
            with runtime.engine.connect() as connection:
                params = {
                    "r": str(
                        flow_service(runtime)
                        .select(project, snapshot, None)[0]
                        .flow_evidence_run_id
                    )
                }
                positions = {
                    f"scope:{r.locator}#{r.scope_key}": r.start_line
                    for r in connection.execute(
                        text(
                            "SELECT a.locator,s.scope_key,s.start_line FROM flow_scopes s "
                            "JOIN flow_artifact_coverage a "
                            "USING(flow_evidence_run_id,artifact_analysis_id) "
                            "WHERE s.flow_evidence_run_id=:r"
                        ),
                        params,
                    )
                }
                call_positions = list(
                    connection.execute(
                        text(
                            "SELECT a.locator,c.scope_key,c.ordinal,c.start_line,c.start_column, "
                            "c.end_line,c.end_column "
                            "FROM flow_callsites c JOIN flow_artifact_coverage a "
                            "USING(flow_evidence_run_id,artifact_analysis_id) "
                            "WHERE c.flow_evidence_run_id=:r"
                        ),
                        params,
                    )
                )
            outgoing: dict[str, list[Any]] = defaultdict(list)
            callsites: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
            for edge in edges:
                outgoing[edge.source_key].append(edge)
            for row in call_positions:
                key = f"call:{row.locator}#{row.scope_key}:{row.ordinal}"
                if key in nodes:
                    callsites[
                        (
                            row.locator,
                            row.start_line,
                            row.start_column,
                            row.end_line,
                            row.end_column,
                        )
                    ].append(nodes[key])
            for call in (c for c in packet["calls"] if c["corpus"] == name):
                found = callsites.get(call_position(call), [])
                targets = set()
                for site in found:
                    for edge in outgoing[site.key]:
                        if edge.relation in {"resolves_to", "dispatch_candidate", "constructs"}:
                            node = nodes.get(edge.target_key)
                            if (
                                node
                                and node.locator
                                and (node.key.startswith("scope:") or node.kind == "function")
                            ):
                                targets.add(node_target(node, functions, positions))
                output["calls"].append(
                    {"id": call["id"], "extracted": bool(found), "targets": sorted(targets)}
                )
            traces = flow_trace_service(runtime)
            for case in (c for c in packet["cases"] if c["corpus"] == name):
                if case.get("entry_call_id"):
                    source_call = next(
                        c for c in packet["calls"] if c["id"] == case["entry_call_id"]
                    )
                    candidates = [n.key for n in callsites.get(call_position(source_call), [])]
                else:
                    candidates = [
                        node.key
                        for node in nodes.values()
                        if node.key.startswith("scope:")
                        and node_target(node, functions, positions) == case.get("entry")
                    ]
                row: dict[str, Any] = {"id": case["id"], **{f: [] for f in FACETS}}
                if len(candidates) == 1:
                    try:
                        trace = traces.trace(
                            project,
                            snapshot,
                            None,
                            TraceQuery(candidates[0], case["direction"], case.get("max_depth", 8)),
                        )
                    except Exception as error:
                        row["error"] = type(error).__name__
                    else:
                        row.update(project_trace(trace, nodes, functions, positions))
                        trace_path = raw / f"{case['id']}.json"
                        if trace_path.exists():
                            if load(trace_path) != trace:
                                raise ValueError(
                                    "Recomputed trace differs from retained observation"
                                )
                        else:
                            save(trace_path, trace)
                else:
                    row["error"] = "ENTRY_ABSENT_OR_AMBIGUOUS"
                output["cases"].append(row)
            for query in (q for q in packet["queries"] if q["corpus"] == name):
                try:
                    result = traces.entries(
                        project, snapshot, None, q=query["q"], mode="lexical", limit=10
                    )
                except Exception as error:
                    output["queries"].append(
                        {
                            "id": query["id"],
                            "targets": [],
                            "mode": None,
                            "requested_mode": "lexical",
                            "error": type(error).__name__,
                        }
                    )
                    continue
                source_targets = []
                for entry in result["items"]:
                    key = entry.get("target")
                    if key and key.startswith("scope:"):
                        source_targets.append(node_target(nodes[key], functions, positions))
                    else:
                        handlers = [
                            e.target_key
                            for e in outgoing.get(key, [])
                            if e.relation == "handled_by"
                        ]
                        source_targets.extend(
                            node_target(nodes[k], functions, positions)
                            for k in handlers
                            if k in nodes
                        )
                output["queries"].append(
                    {
                        "id": query["id"],
                        "targets": source_targets,
                        "raw_entries": [e["key"] for e in result["items"]],
                        "mode": result["mode"],
                    }
                )
        finally:
            asyncio.run(runtime.close())
    filename = (f"observations.{only}" if only else "observations") + revision + ".json"
    save(directory / filename, bound(output))


def retained_performance_samples(
    directory: Path,
    corpus_name: str,
    attempts: list[str],
    input_digest: str,
    client_deadline: float | None,
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Resume explicitly named, protocol-bound attempts without replacing observations."""
    if len(attempts) != len(set(attempts)):
        raise ValueError("Duplicate resume attempt")
    journal = directory / f"performance-samples.{corpus_name}.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()] if attempts else []
    legacy_path = directory / "performance-legacy-attempts.json"
    legacy = load(legacy_path) if legacy_path.exists() else {}
    if legacy and legacy.get("digest") != bound(legacy)["digest"]:
        raise ValueError("Legacy attempt registry digest mismatch")
    retained = {}
    for attempt in attempts:
        selected = [row for row in rows if row["attempt"] == attempt]
        metadata_path = directory / f"performance-attempt.{corpus_name}.{attempt}.json"
        metadata = (
            load(metadata_path)
            if metadata_path.exists()
            else legacy.get("attempts", {}).get(attempt, {})
        )
        if metadata_path.exists() and metadata.get("digest") != bound(metadata)["digest"]:
            raise ValueError("Attempt metadata digest mismatch")
        if (
            not selected
            or metadata.get("corpus") != corpus_name
            or metadata.get("input_digest") != input_digest
            or "client_deadline_seconds" not in metadata
            or metadata["client_deadline_seconds"] != client_deadline
            or (
                not metadata_path.exists()
                and metadata.get("rows_digest") != bound({"rows": selected})["digest"]
            )
        ):
            raise ValueError("Resume attempt missing or protocol mismatch")
        for row in selected:
            key = (row["workload"], row["class"], row["index"])
            if key in retained:
                raise ValueError("Overlapping resume samples")
            retained[key] = row
    return retained


def performance(
    directory: Path,
    corpus_name: str,
    client_deadline: float | None = None,
    resume_attempts: list[str] | None = None,
) -> None:
    if client_deadline is not None and client_deadline <= 0:
        raise ValueError("Client deadline must be positive")
    prereg = load(directory / "preregistration.json")
    require_preregistration(prereg)
    runtime, info = runtime_for(directory, corpus_name)
    project, snapshot, run_id = (UUID(info[k]) for k in ("project", "snapshot", "run"))
    traces = flow_trace_service(runtime)
    store = SqlAlchemyFlowStore(runtime.session_factory)
    nodes, edges = store.graph(run_id)
    scopes = sorted(k for k in nodes if k.startswith("scope:"))
    rng = random.Random(151207)
    selected = rng.sample(scopes, min(20, len(scopes)))
    pairs = [
        (e.source_key, e.target_key) for e in edges if e.relation in {"invokes", "awaits", "enters"}
    ]
    pairs = sorted(set(pairs))
    pair_inputs = rng.sample(pairs, min(20, len(pairs)))
    queries = sorted(
        {str(n.label).replace("_", " ") for n in nodes.values() if n.kind == "entry_point"}
    )
    query_inputs = rng.sample(queries, min(20, len(queries)))
    inputs = {
        "forward": selected,
        "backward": selected,
        "path": pair_inputs,
        "entry_search": query_inputs,
    }
    frozen_inputs = bound({"preregistration_digest": prereg["digest"], "inputs": inputs})
    input_path = directory / f"performance-inputs.{corpus_name}.json"
    if input_path.exists():
        if load(input_path).get("digest") != frozen_inputs["digest"]:
            raise ValueError("Performance input mixture changed")
    else:
        save(input_path, frozen_inputs)
    journal = directory / f"performance-samples.{corpus_name}.jsonl"
    attempt = str(uuid4())
    retained = retained_performance_samples(
        directory, corpus_name, resume_attempts or [], frozen_inputs["digest"], client_deadline
    )
    save(
        directory / f"performance-attempt.{corpus_name}.{attempt}.json",
        bound(
            {
                "corpus": corpus_name,
                "input_digest": frozen_inputs["digest"],
                "preregistration_digest": prereg["digest"],
                "client_deadline_seconds": client_deadline,
                "resumed_from_attempts": resume_attempts or [],
                "attempt": attempt,
            }
        ),
    )
    summaries: dict[str, Any] = {}
    observations: dict[str, list[Any]] = defaultdict(list)
    run = store.run(project, snapshot, run_id)
    assert run is not None

    def query(kind: str, value: Any) -> None:
        if kind in {"forward", "backward"}:
            traces.trace(project, snapshot, None, TraceQuery(value, kind))
        elif kind == "path":
            traces.path(project, snapshot, None, *value)
        else:
            traces.entries(project, snapshot, None, q=value, mode="lexical")

    def bounded_query(kind: str, value: Any) -> None:
        if client_deadline is None:
            query(kind, value)
            return

        def timeout(signum: int, frame: Any) -> None:
            raise TimeoutError("Evaluation client deadline exceeded")

        previous = signal.signal(signal.SIGALRM, timeout)
        # A one-shot signal can be swallowed by a Python weakref finalizer. Repeat until
        # the query's finally block cancels it, and also check elapsed time on return.
        deadline_at = time.monotonic() + client_deadline
        signal.setitimer(signal.ITIMER_REAL, client_deadline, 0.1)
        try:
            query(kind, value)
            if time.monotonic() > deadline_at:
                raise TimeoutError("Evaluation client deadline exceeded")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    try:
        for workload in ("cold_application", "warm"):
            warmup_errors: dict[str, int] = defaultdict(int)
            for kind, values in inputs.items():
                if values:
                    warmup_start = time.perf_counter()
                    for _ in range(5):
                        try:
                            bounded_query(kind, rng.choice(values))
                        except Exception:
                            warmup_errors[kind] += 1
                    print(
                        json.dumps(
                            {
                                "warmup": corpus_name,
                                "workload": workload,
                                "class": kind,
                                "seconds": time.perf_counter() - warmup_start,
                                "errors": warmup_errors[kind],
                            }
                        ),
                        flush=True,
                    )
            for i in range(59):
                kinds = list(inputs)
                rng.shuffle(kinds)
                for kind in kinds:
                    if not inputs[kind]:
                        continue
                    value = rng.choice(inputs[kind])
                    previous_sample = retained.get((workload, kind, i))
                    if previous_sample is not None:
                        if (
                            bound({"input": value})["digest"]
                            != bound({"input": previous_sample["input"]})["digest"]
                        ):
                            raise ValueError("Retained sample does not match deterministic input")
                        observations[f"{workload}:{kind}"].append(
                            {k: previous_sample[k] for k in ("index", "input", "seconds", "error")}
                        )
                        continue
                    if workload == "cold_application":
                        traces._cache.evict(run)
                    start = time.perf_counter()
                    try:
                        bounded_query(kind, value)
                        error = None
                    except Exception as e:
                        error = type(e).__name__
                    sample = {
                        "index": i,
                        "input": value,
                        "seconds": time.perf_counter() - start,
                        "error": error,
                    }
                    observations[f"{workload}:{kind}"].append(sample)
                    with journal.open("a") as handle:
                        handle.write(
                            json.dumps(
                                {"attempt": attempt, "workload": workload, "class": kind, **sample}
                            )
                            + "\n"
                        )
                if i % 5 == 0:
                    print(
                        json.dumps({"performance": corpus_name, "workload": workload, "sample": i}),
                        flush=True,
                    )
            for kind, values in inputs.items():
                data = observations[f"{workload}:{kind}"]
                report = latency_summary([r["seconds"] for r in data], distinct_inputs=len(values))
                report["errors"] = sum(r["error"] is not None for r in data)
                report["warmup_errors"] = warmup_errors[kind]
                report["censored_timeouts"] = sum(r["error"] == "TimeoutError" for r in data)
                if report["censored_timeouts"]:
                    report["p95_upper95"] = None
                    report["confidence"] = None
                    report["latency_note"] = (
                        "Censored attempt durations; no completed-request quantile claim."
                    )
                if report["errors"]:
                    report["status"] = "failed"
                if workload == "warm" and warmup_errors[kind]:
                    report["status"] = "warmup_failed"
                summaries[f"{workload}:{kind}"] = report
        save(
            directory
            / (f"performance.{corpus_name}" + (".deadline" if client_deadline else "") + ".json"),
            bound(
                {
                    "preregistration_digest": prereg["digest"],
                    "client_deadline_seconds": client_deadline,
                    "attempt": attempt,
                    "resumed_from_attempts": resume_attempts or [],
                    "basis": "Post-observation deadline diagnostic; not original S7 quantiles"
                    if client_deadline
                    else "registered uncensored protocol",
                    "machine": platform.platform(),
                    "python": platform.python_version(),
                    "load": os.getloadavg(),
                    "database_buffers": "uncontrolled",
                    "summaries": summaries,
                    "observations": observations,
                }
            ),
        )
    finally:
        asyncio.run(runtime.close())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["build", "observe", "performance"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--corpus")
    parser.add_argument("--revision", default="")
    parser.add_argument("--client-deadline", type=float)
    parser.add_argument("--resume-attempt", action="append", default=[])
    parser.add_argument("--admin", default="postgresql+psycopg://postgres@127.0.0.1:55450/postgres")
    args = parser.parse_args()
    if args.command == "build":
        build(args.directory, args.corpus or "buildlens", args.admin)
    elif args.command == "observe":
        observe(args.directory, args.corpus, args.revision)
    else:
        performance(
            args.directory, args.corpus or "buildlens", args.client_deadline, args.resume_attempt
        )


if __name__ == "__main__":
    main()
