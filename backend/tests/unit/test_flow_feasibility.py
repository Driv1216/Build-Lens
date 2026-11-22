"""Stage 15 Milestone C: the sound predicate fragment (``flow-feasibility.v1``) as pure rules."""

from __future__ import annotations

from buildlens.domain.flow_trace import Atom, decide, decide_dnf, literal_key, trace_digest

X = ("scope:a.py#m.0", "x", (0,))
Y = ("scope:a.py#m.0", "y", (1,))
X_REDEFINED = ("scope:a.py#m.0", "x", (2,))
ONE, TWO = ("int", 1), ("int", 2)


def test_contradictions_in_the_fragment_refute() -> None:
    assert decide((Atom("nullish", X, True), Atom("nullish", X, False))) == "refuted_in_fragment"
    assert decide((Atom("eq", X, True, ONE), Atom("eq", X, True, TWO))) == "refuted_in_fragment"
    assert decide((Atom("eq", X, True, ONE), Atom("eq", X, False, ONE))) == "refuted_in_fragment"
    assert decide((Atom("truthy", X, True), Atom("truthy", X, False))) == "refuted_in_fragment"
    assert decide((Atom("nullish", X, True), Atom("truthy", X, True))) == "refuted_in_fragment"
    assert decide((Atom("eq", X, True, ("int", 0)), Atom("truthy", X, True))) == (
        "refuted_in_fragment"
    )
    typeofs = (Atom("typeof", X, True, text="string"), Atom("typeof", X, True, text="number"))
    assert decide(typeofs) == "refuted_in_fragment"
    assert decide((Atom("const", None, True, ("bool", False)),)) == "refuted_in_fragment"


def test_consistent_only_when_every_atom_is_decided() -> None:
    assert decide((Atom("eq", X, True, ONE), Atom("eq", Y, True, TWO))) == (
        "consistent_in_fragment"
    )
    assert decide((Atom("nullish", X, False), Atom("truthy", X, True))) == (
        "consistent_in_fragment"
    )
    assert decide(()) == "consistent_in_fragment"
    # Cross-type numeric comparison is not decided (``True == 1`` in Python).
    assert decide((Atom("eq", X, True, ("bool", True)), Atom("eq", X, True, ONE))) == (
        "undetermined"
    )


def test_opaque_atoms_are_never_used_to_refute() -> None:
    opaque = Atom("opaque", None, True, reason="CALL")
    assert decide((opaque, Atom("opaque", None, False, reason="CALL"))) == "undetermined"
    assert decide((opaque, Atom("eq", X, True, ONE))) == "undetermined"
    # A redefinition is a different variable: no contradiction across definitions.
    assert decide((Atom("eq", X, True, ONE), Atom("eq", X_REDEFINED, True, TWO))) == (
        "consistent_in_fragment"
    )


def test_one_activation_takes_one_outcome_at_a_non_loop_branch() -> None:
    branch = ("scope:a.py#m.0", 7)
    left = Atom("opaque", None, True, reason="CALL", branch=branch)
    right = Atom("opaque", None, False, reason="CALL", branch=branch)
    assert decide((left, right)) == "refuted_in_fragment"
    assert decide((left, Atom("opaque", None, True, reason="CALL", branch=branch))) == (
        "undetermined"
    )


def test_disjunctions_and_approximation() -> None:
    refuted = (Atom("nullish", X, True), Atom("nullish", X, False))
    fine = (Atom("nullish", X, True),)
    assert decide_dnf([refuted, refuted], False) == "refuted_in_fragment"
    assert decide_dnf([refuted, fine], False) == "consistent_in_fragment"
    # An approximate (capped) guard set never refutes.
    assert decide_dnf([refuted], True) == "undetermined"
    assert decide_dnf([], True) == "undetermined"


def test_literal_identity_and_trace_digest_are_canonical() -> None:
    text = {"t": "str", "sha256": "ab" * 32, "len": 3}
    assert literal_key(text) == ("str", "ab" * 32, 3)
    assert literal_key({"t": "float", "sha256": "x"}) is None
    one = trace_digest("g" * 64, {"entry": "e"}, {"steps": [1]})
    assert one == trace_digest("g" * 64, {"entry": "e"}, {"steps": [1]})
    assert one != trace_digest("g" * 64, {"entry": "e"}, {"steps": [2]})


def test_routes_into_a_shared_hop_are_alternatives_within_bounds() -> None:
    from buildlens.application.flow_trace import _disjoin
    from buildlens.domain.flow_trace import DNF_TERMS

    Route = tuple[list[tuple[Atom, ...]], bool]
    none, not_none = Atom("nullish", X, True), Atom("nullish", X, False)
    refuted_route: Route = ([(none, not_none)], False)
    # Alternatives, never one conjunction: a feasible route keeps the hop feasible.
    feasible_route: Route = ([(Atom("eq", Y, True, ONE),)], False)
    merged = _disjoin([refuted_route, feasible_route])
    assert merged == ([(none, not_none), (Atom("eq", Y, True, ONE),)], False)
    assert decide_dnf(*merged) == "consistent_in_fragment"
    # Two refuted routes stay refuted; a repeated term is kept once.
    assert _disjoin([refuted_route, refuted_route]) == ([(none, not_none)], False)
    assert decide_dnf(*_disjoin([refuted_route, refuted_route])) == "refuted_in_fragment"
    # An exact unconditional route makes the hop unconditional; an approximate one does not
    # make the merge exact.
    unconditional: Route = ([()], False)
    unconditional_approximate: Route = ([()], True)
    assert _disjoin([refuted_route, unconditional]) == ([()], False)
    assert _disjoin([refuted_route, unconditional_approximate]) == ([()], True)
    # Beyond the DNF bound the merge is truncated and approximate: never refuted, even when
    # every kept term contradicts.
    many: list[Route] = [
        ([(Atom("eq", X, True, ("int", i)), Atom("eq", X, True, ("int", i + 1)))], False)
        for i in range(DNF_TERMS + 1)
    ]
    terms, approximate = _disjoin(many)
    assert len(terms) == DNF_TERMS and approximate
    assert all(decide(t) == "refuted_in_fragment" for t in terms)
    assert decide_dnf(terms, approximate) == "undetermined"
