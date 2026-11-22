"""Interrupted measurements cannot silently replace samples or mix timeout protocols."""

import json
from pathlib import Path

import pytest

from buildlens.application.flow_evaluation import bound
from scripts.flow_evaluation_run import retained_performance_samples


def setup_attempts(tmp_path: Path) -> None:
    rows = []
    for index, attempt in enumerate(["first", "second"]):
        rows.append({"attempt": attempt, "workload": "warm", "class": "forward", "index": index})
        metadata = bound(
            {"corpus": "demo", "input_digest": "inputs", "client_deadline_seconds": None}
        )
        (tmp_path / f"performance-attempt.demo.{attempt}.json").write_text(json.dumps(metadata))
    (tmp_path / "performance-samples.demo.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows)
    )


def test_resume_preserves_multiple_disjoint_attempts(tmp_path: Path) -> None:
    setup_attempts(tmp_path)
    rows = retained_performance_samples(tmp_path, "demo", ["first", "second"], "inputs", None)
    assert list(rows) == [("warm", "forward", 0), ("warm", "forward", 1)]


@pytest.mark.parametrize("bad", ["deadline", "inputs", "overlap", "missing", "duplicate"])
def test_resume_refuses_wrong_protocol_and_duplicate_observations(tmp_path: Path, bad: str) -> None:
    setup_attempts(tmp_path)
    attempts, inputs, deadline = ["first", "second"], "inputs", None
    if bad == "deadline":
        deadline = 2.0
    elif bad == "inputs":
        inputs = "changed"
    elif bad == "missing":
        attempts = ["missing"]
    elif bad == "duplicate":
        attempts = ["first", "first"]
    else:
        journal = tmp_path / "performance-samples.demo.jsonl"
        rows = [json.loads(line) for line in journal.read_text().splitlines()]
        rows[1]["index"] = 0
        journal.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError):
        retained_performance_samples(tmp_path, "demo", attempts, inputs, deadline)


def test_legacy_resume_is_bound_to_the_exact_retained_rows(tmp_path: Path) -> None:
    setup_attempts(tmp_path)
    (tmp_path / "performance-attempt.demo.first.json").unlink()
    journal = tmp_path / "performance-samples.demo.jsonl"
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    metadata = {
        "corpus": "demo",
        "input_digest": "inputs",
        "client_deadline_seconds": None,
        "rows_digest": bound({"rows": rows[:1]})["digest"],
    }
    registry = bound({"attempts": {"first": metadata}})
    (tmp_path / "performance-legacy-attempts.json").write_text(json.dumps(registry))
    assert len(retained_performance_samples(tmp_path, "demo", ["first"], "inputs", None)) == 1
    rows[0]["index"] = 10
    journal.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="protocol"):
        retained_performance_samples(tmp_path, "demo", ["first"], "inputs", None)
