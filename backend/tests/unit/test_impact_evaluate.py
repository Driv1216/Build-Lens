import pytest

from scripts.impact_evaluate import (
    CORPORA,
    PERFORMANCE_MODES,
    QUALITY_STRATA,
    _seal,
    performance,
    quality,
    quality_readiness,
)


def test_quality_keeps_unknown_and_abstained_units_in_their_dimensions() -> None:
    cases = [
        {"id": f"{stratum}:{i}", "stratum": stratum, "expected_completion": "complete_for_scope"}
        for stratum in sorted(QUALITY_STRATA)
        for i in range(4)
    ]
    packet = {"cases": cases}
    labels = {
        "packet_digest": _seal(packet),
        "reviewed_cases": [
            {"case": row["id"], "reviewer": "fixture", "independent": False} for row in cases
        ],
        "units": [
            {
                "case": cases[0]["id"],
                "dimension": "entity",
                "claim": "known",
                "verdict": "expected",
            },
            {
                "case": cases[0]["id"],
                "dimension": "entity",
                "claim": "unknown",
                "verdict": "unknown",
            },
            {
                "case": cases[0]["id"],
                "dimension": "property",
                "claim": "value",
                "verdict": "expected",
            },
        ],
    }
    observed = {
        "label_digest": _seal(labels),
        "cases": [
            {"case": row["id"], "status": "complete_for_scope", "findings": []} for row in cases
        ],
        "baselines": {
            name: [{"case": cases[0]["id"], "entity": "known"}] for name in ("file", "symbol")
        },
    }
    observed["cases"][0]["findings"] = [
        {"dimension": "entity", "claim": "unknown", "assertion": "definitive"},
        {"dimension": "property", "claim": "value", "assertion": "uncertain"},
    ]
    result = quality(
        {**packet, "digest": _seal(packet)},
        {**labels, "digest": _seal(labels)},
        {**observed, "digest": _seal(observed)},
    )
    assert result["status"] == "inconclusive"
    assert result["models"]["entity"]["false_definitive_unknowns"] == 1
    assert result["models"]["entity"]["precision"] == 0
    assert result["models"]["property"]["recall"] == 0
    assert result["models"]["property"]["abstentions"] == 1
    assert result["models"]["property"]["abstained_expected"] == 1


def test_quality_waits_for_frozen_scope_and_rejects_out_of_scope_baselines() -> None:
    cases = [
        {
            "id": f"{stratum}:{i}",
            "stratum": stratum,
            "scope": ["example.py"],
            "expected_completion": "complete_for_scope",
        }
        for stratum in sorted(QUALITY_STRATA)
        for i in range(4)
    ]
    packet = {"cases": cases}
    packet["digest"] = _seal(packet)
    assert quality_readiness(packet)["status"] == "inconclusive"
    labels = {
        "packet_digest": packet["digest"],
        "units": [],
        "reviewed_cases": [
            {
                "case": row["id"],
                "reviewer": "independent",
                "independent": True,
                "scope_entities": ["example.py::target"],
            }
            for row in cases
        ],
    }
    labels["reviewed_cases"][0].pop("scope_entities")
    labels["digest"] = _seal(labels)
    assert quality_readiness(packet, labels)["unlabelled_cases"] == [cases[0]["id"]]
    labels["reviewed_cases"][0]["scope_entities"] = ["example.py::target"]
    labels["digest"] = _seal(labels)
    assert quality_readiness(packet, labels)["status"] == "ready_for_scoring"
    observed = {
        "label_digest": labels["digest"],
        "cases": [
            {"case": row["id"], "status": "complete_for_scope", "findings": []} for row in cases
        ],
        "baselines": {
            "file": [{"case": cases[0]["id"], "entity": "outside.py::decoy"}],
            "symbol": [],
        },
    }
    observed["digest"] = _seal(observed)
    with pytest.raises(ValueError, match="outside the frozen scope"):
        quality(packet, labels, observed)


def test_performance_refuses_missing_attempts_and_counts_a_fast_deadline_as_incomplete() -> None:
    packet = {
        "cases": [
            {
                "corpus": corpus,
                "mode": mode,
                "expected_completion": "complete_for_scope",
                "required_findings": ["specific"],
            }
            for corpus in sorted(CORPORA)
            for mode in sorted(PERFORMANCE_MODES)
        ]
    }
    samples = [
        {
            "corpus": row["corpus"],
            "mode": row["mode"],
            "state": state,
            "repeat": repeat,
            "status": "complete_for_scope",
            "findings": ["specific"],
            "verification": "verified",
            "wall_seconds": 1,
            "cleanup_completed_seconds": 1,
        }
        for row in packet["cases"]
        for state in ("cold_application", "warm")
        for repeat in (1, 2)
    ]
    packet["digest"] = _seal(packet)
    observed = {"packet_digest": packet["digest"], "samples": samples}
    for sample in samples[:3]:
        sample.update(status="deadline", findings=[])
    observed["digest"] = _seal(observed)
    score = performance(packet, observed)
    group = score["corpora"][samples[0]["corpus"]]
    assert score["status"] == "failed"
    assert group["classified_within_three_seconds"] == 20
    assert group["verified_within_two_seconds"] == 17
    samples.pop()
    observed["digest"] = _seal(observed)
    with pytest.raises(ValueError, match="incomplete"):
        performance(packet, observed)
