"""Stage 13 Milestone C: deterministic claim validation (§4.1 checks 1–9, §8.4)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from buildlens.application.semantic_generation import stale_reasons
from buildlens.application.semantic_prompt import (
    FactView,
    Package,
    PackageChunk,
    PackageFact,
)
from buildlens.application.semantic_validation import identifiers, validate
from buildlens.domain.semantic import EvidenceChunk, SemanticEntity, SemanticFact

CODE = (
    "def load(record, user_id):\n"
    '    """Load one user record."""\n'
    "    # look it up\n"
    "    value = fetch_user(user_id)\n"
    '    return {"id": user_id}\n'
)
DOC_START = CODE.index('"""')
DOC_END = CODE.index('"""', DOC_START + 3) + 3
COMMENT_START = CODE.index("# look")
COMMENT_END = CODE.index("\n", COMMENT_START)
STRING_START = CODE.index('"id"')


def _entity(**changes: Any) -> SemanticEntity:
    values: dict[str, Any] = {
        "entity_key": "symbol:1",
        "kind": "symbol",
        "language": "Python",
        "locator": "app/api.py",
        "artifact_identity_id": None,
        "symbol_identity_id": None,
        "subject_entity_key": None,
        "name": "load",
        "qualified_name": "load",
        "symbol_kind": "function",
        "start_line": 1,
        "end_line": 5,
        "ordinal": 0,
        "portable_key": "symbol:app/api.py#load:function:0",
        "stored_coverage": "complete",
        "coverage_reason": None,
        "context_coverage": "full",
        "context_reason": None,
        "package_digest": "a" * 64,
        "package_bytes": 100,
    }
    values.update(changes)
    return SemanticEntity(**values)


def _fact(text: str, kind: str = "signature", locator: str = "app/api.py") -> SemanticFact:
    return SemanticFact(
        uuid4().hex * 2,
        0,
        "symbol:1",
        kind,  # type: ignore[arg-type]
        ({"table": "symbols", "id": "x", "contract": None, "locator": locator},),
        {},
        "fact-renderer.v1",
        text,
        "verified",
    )


def _package(
    *,
    view: FactView | None = None,
    foreign: frozenset[str] = frozenset(),
    ranges: bool = True,
    neighbor: SemanticFact | None = None,
) -> Package:
    chunk = EvidenceChunk(uuid4(), "k" * 64, "h" * 64, 0, len(CODE), 1, 5, None, CODE, True)
    facts = [
        PackageFact(
            "F1", _fact("function load at app/api.py lines 1–5: def load(record, user_id)"), False
        )
    ]
    if neighbor is not None:
        facts.append(PackageFact("F2", neighbor, True))
    return Package(
        _entity(),
        (PackageChunk("E1", chunk, "app/api.py"),),
        tuple(facts),
        (),
        "a" * 64,
        "b" * 64,
        view or FactView(is_async=False, returns_value=True, calls=frozenset({"fetch_user"})),
        foreign,
        {
            "documentation": ((DOC_START, DOC_END), (COMMENT_START, COMMENT_END)),
            "string": ((STRING_START, STRING_START + 4), (DOC_START, DOC_END)),
        }
        if ranges
        else {},
        ranges,
    )


def _claim(
    text: str,
    quote: str = "value = fetch_user(user_id)",
    evidence_id: str = "E1",
    category: str = "behavior",
    assertion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "text": text,
        "evidence": [{"id": evidence_id, "quote": quote}],
        "assertion": assertion,
    }


def _run(package: Package, *claims: dict[str, Any], unknowns: list[str] | None = None) -> Any:
    return validate(
        json.dumps({"claims": list(claims), "unknowns": [{"text": t} for t in unknowns or []]}),
        package,
    )


def test_accepted_claim_records_citation_coordinates_without_quote_text() -> None:
    result = _run(
        _package(),
        _claim(
            "`load` returns a dictionary.",
            'return {"id": user_id}',
            assertion={"kind": "returns_value", "target": None},
        ),
    )
    assert result.status == "accepted" and result.rejection_codes == ()
    [claim] = result.claims
    assert (claim.status, claim.basis, claim.basis_label) == ("cited", "code", "cited")
    assert claim.assertion_outcome == "matches_fact"
    [citation] = claim.citations
    assert citation.kind == "chunk" and citation.quote is None and len(citation.quote_sha256) == 64
    assert CODE[citation.start : citation.end] == 'return {"id": user_id}'
    assert citation.start_line == 5


@pytest.mark.parametrize(
    ("claim", "code"),
    [
        ({"category": "behavior", "text": "x"}, "CLAIM_STRUCTURE"),
        (_claim("`load` reads.", evidence_id="E9"), "EVIDENCE_UNKNOWN"),
        (_claim("`load` reads.", quote="fetch"), "QUOTE_LENGTH"),
        (_claim("`load` reads.", quote="not in the chunk at all"), "QUOTE_NOT_FOUND"),
        (_claim("`load` reads.", quote="user_id"), "QUOTE_LENGTH"),
        (_claim("`load` reads it.", quote="user_id)"), "QUOTE_AMBIGUOUS"),
        (_claim("`missing_helper` is used.", category="purpose"), "UNGROUNDED_IDENTIFIER"),
        (_claim("The code is secure.", category="purpose"), "LEXICON_VERDICT"),
        (
            _claim("`load` reads.", quote='"""Load one user record."""'),
            "BEHAVIOR_WITHOUT_CODE_EVIDENCE",
        ),
        (_claim("`load` reads.", quote="# look it up"), "BEHAVIOR_WITHOUT_CODE_EVIDENCE"),
        (
            _claim("`load` is declared.", quote="def load(record, user_id)", evidence_id="F1"),
            "BEHAVIOR_WITHOUT_CODE_EVIDENCE",
        ),
        (
            _claim("Uses token ghp_" + "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0hJ3kL6", category="purpose"),
            "SECRET_SCREENED",
        ),
    ],
)
def test_each_check_rejects_with_its_code(claim: dict[str, Any], code: str) -> None:
    result = _run(_package(), claim)
    assert result.status == "rejected" and result.claims == ()
    assert result.rejection_codes == (f"claim0:{code}",)


def test_basis_labels_and_unclassified_quotes() -> None:
    documented = _run(
        _package(),
        _claim("`load` loads a record.", '"""Load one user record."""', category="purpose"),
    )
    assert documented.claims[0].basis_label == "per_documentation"
    facts = _run(
        _package(), _claim("`load` takes `user_id`.", "def load(record, user_id)", "F1", "purpose")
    )
    assert facts.claims[0].basis_label == "from_declarations_only"
    # Without comment/string ranges (legacy facts) nothing counts as code evidence.
    legacy = _run(_package(ranges=False), _claim("`load` calls `fetch_user`."))
    assert legacy.rejection_codes == ("claim0:BEHAVIOR_WITHOUT_CODE_EVIDENCE",)


def test_omitted_implementation_is_rejected_unless_a_matching_calls_assertion() -> None:
    package = _package(foreign=frozenset({"fetch_user"}))
    # External callee: describing what it does is rejected.
    callee = _run(package, _claim("`fetch_user` queries the database for the user."))
    assert callee.rejection_codes == ("claim0:OMITTED_IMPLEMENTATION_REFERENCED",)
    # The allowed form: the claim describes the call, asserted against parsed facts.
    allowed = _run(
        package,
        _claim(
            "`load` calls `fetch_user` with `user_id`.",
            assertion={"kind": "calls", "target": "fetch_user"},
        ),
    )
    assert allowed.status == "accepted" and allowed.claims[0].assertion_outcome == "matches_fact"
    # An outline child or neighbour name is foreign in exactly the same way.
    neighbour = _package(
        foreign=frozenset({"session"}),
        neighbor=_fact(
            "function session at app/db.py lines 13–14: def session()", locator="app/db.py"
        ),
    )
    named = _run(
        neighbour, _claim("Opens a session before calling `fetch_user`.", category="side_effect")
    )
    assert named.rejection_codes == ("claim0:OMITTED_IMPLEMENTATION_REFERENCED",)
    # A calls assertion that does not match parsed facts is not enough.
    wrong = _run(
        package,
        _claim("`load` calls `fetch_user` twice.", assertion={"kind": "calls", "target": "other"}),
    )
    assert wrong.rejection_codes == ("claim0:OMITTED_IMPLEMENTATION_REFERENCED",)


def test_assertions_against_facts_and_conflicts() -> None:
    conflict = _run(
        _package(),
        _claim(
            "`load` is a coroutine function.",
            "def load(record, user_id):",
            category="purpose",
            assertion={"kind": "is_async", "target": None},
        ),
    )
    assert conflict.status == "accepted_with_conflicts"
    assert (conflict.claims[0].status, conflict.claims[0].assertion_outcome) == (
        "conflicts_with_fact",
        "conflicts_with_fact",
    )
    unknown = _run(
        _package(view=FactView()),
        _claim(
            "`load` raises on a missing record.",
            assertion={"kind": "raises", "target": "LookupError"},
        ),
    )
    assert unknown.claims[0].assertion_outcome == "not_checkable"
    truncated = _run(
        _package(view=FactView(calls=frozenset({"x"}), references_complete=False)),
        _claim("`load` calls `fetch_user`.", assertion={"kind": "calls", "target": "fetch_user"}),
    )
    assert truncated.claims[0].assertion_outcome == "not_checkable"


def test_quantifiers_flag_scope_and_unknowns_are_checked() -> None:
    result = _run(
        _package(),
        _claim(
            "`load` always calls `fetch_user`.", assertion={"kind": "calls", "target": "fetch_user"}
        ),
        unknowns=[
            "Whether `fetch_user` can fail is not shown.",
            "This is correct. And safe.",
            "`ghost_name` is unclear.",
        ],
    )
    assert result.claims[0].scope_warning is True
    assert result.unknowns == ("Whether `fetch_user` can fail is not shown.",)
    assert result.rejection_codes == (
        "unknown1:UNKNOWN_NOT_ONE_SENTENCE",
        "unknown2:UNGROUNDED_IDENTIFIER",
    )


def test_interpretation_statuses_and_malformed_output() -> None:
    package = _package()
    assert _run(package).status == "insufficient_evidence"
    assert _run(package, {"category": "purpose"}).status == "rejected"
    for raw in (
        "",
        "not json",
        '{"claims": [], "unknowns": [], "extra": 1}',
        '{"claims": [], "claims": []}',
    ):
        assert validate(raw, package).rejection_codes == ("OUTPUT_INVALID",)


def test_identifier_extraction() -> None:
    assert identifiers("`load()` uses fetch_user and Service.run, e.g. via HttpClient.") == {
        "load",
        "fetch_user",
        "Service.run",
        "HttpClient",
    }


def test_stale_reasons_from_manifest_diff() -> None:
    entity = _entity(
        package_chunks=("k1",),
        package_facts=("f1",),
        neighbor_facts=(),
        evidence=(EvidenceChunk(uuid4(), "k1", "h1", 0, 1, 1, 1, None),),
    )
    manifest = {
        "prompt_version": "semantic-prompt.v1",
        "schema_version": "semantic-claims.v1",
        "validator_version": "semantic-validator.v1",
        "context_coverage": "full",
        "chunks": [["k1", "h1"]],
        "facts": ["f1"],
        "neighbors": [],
        "omitted": [],
    }
    assert stale_reasons(manifest, None) == ("ENTITY_REMOVED",)
    assert stale_reasons({**manifest, "chunks": [["k1", "old"]]}, entity) == ("SOURCE_CHANGED",)
    assert stale_reasons({**manifest, "facts": ["f0"]}, entity) == ("FACTS_CHANGED",)
    assert stale_reasons({**manifest, "neighbors": ["n"]}, entity) == ("NEIGHBOR_FACTS_CHANGED",)
    assert stale_reasons({**manifest, "context_coverage": "outline"}, entity) == (
        "CONTEXT_COVERAGE_CHANGED",
    )
    assert stale_reasons({**manifest, "prompt_version": "old"}, entity) == ("CONTRACT_CHANGED",)
