"""Stage 15 Milestone A: statement-level CFGs, control-dependence guards and scope evidence.

Every expected guard set below was written by hand from the language semantics before the
lowering was run (plan §3.2 examples plus a construct matrix). Guards are read back through
the stored display text of each branch node, so the expectations read like the source.
"""

from __future__ import annotations

from typing import Any

import pytest

from buildlens.domain import flow_cfg as C
from buildlens.domain import flow_evidence as E
from buildlens.domain.flow_evidence import FlowExtraction, FlowScope, display, literal
from buildlens.infrastructure.structural.python_flow import extract_python_flow
from buildlens.infrastructure.structural.typescript_flow import extract_ts_flow

Guard = frozenset[tuple[str, str]]


def _scope(extraction: FlowExtraction, name: str | None) -> FlowScope:
    matches = [s for s in extraction.scopes if s.name == name]
    assert len(matches) == 1, [s.name for s in extraction.scopes]
    return matches[0]


def _texts(extraction: FlowExtraction) -> dict[str, str | None]:
    return {t.key: t.text for t in extraction.display_texts}


def _guards(extraction: FlowExtraction, scope: FlowScope, node: int) -> set[Guard]:
    """Guards of a CFG node as {frozenset{(branch source text, outcome)}} (DNF)."""
    texts = _texts(extraction)
    graph = C.Graph.of(scope.cfg)
    found = C.guards(graph, node)
    assert not found.approximate
    values: set[Guard] = set()
    for term in found.terms:
        parts = []
        for dep in term:
            branch = scope.cfg["nodes"][dep.branch]
            label = texts.get(branch.get("text", ""), None) or str(
                branch.get("atom", branch["kind"])
            )
            parts.append(
                (label, dep.label if dep.kind in {C.CASE, C.NO_MATCH} and dep.label else dep.kind)
            )
        values.add(frozenset(parts))
    return values


def _token() -> str:
    """A recognizable token built at run time, so this test file is never screened itself."""
    import string

    return "ghp" + "_" + "".join(string.ascii_letters[(i * 7) % 52] for i in range(36))


def _call(scope: FlowScope, name: str, occurrence: int = 0) -> Any:
    matches = [
        c
        for c in scope.callsites
        if c.callee.get("id") == name or (c.callee.get("path") or [None])[-1] == name
    ]
    return matches[occurrence]


def _reachable(scope: FlowScope, target: int, *, normal_only: bool = False) -> bool:
    return target in C.Graph.of(scope.cfg).reachable(C.ENTRY, normal_only=normal_only)


def py(source: str) -> FlowExtraction:
    return extract_python_flow(source.encode(), {})


def ts(source: str, locator: str = "a.tsx") -> FlowExtraction:
    return extract_ts_flow(locator, source.encode(), [])


# ----------------------------------------------------------------------------- Python


def test_early_exit_guards_come_from_control_dependence_not_nesting() -> None:
    x = py(
        "@router.post('/login')\n"
        "async def login(body, svc=Depends(get_auth)):\n"
        "    user = await svc.find(body.email)\n"
        "    if user is None:\n"
        "        raise HTTPException(status_code=401)\n"
        "    if not svc.verify(user, body.password):\n"
        "        raise HTTPException(status_code=401)\n"
        "    token = svc.issue(user)\n"
        "    return {'token': token}\n"
    )
    login = _scope(x, "login")
    assert login.callable_kind == "coroutine_function"
    find, verify, issue = _call(login, "find"), _call(login, "verify"), _call(login, "issue")
    assert find.awaited and not verify.awaited
    assert _guards(x, login, find.cfg_node) == set()
    assert _guards(x, login, verify.cfg_node) == {frozenset({("user is None", "false")})}
    # A region tree would give issue() no guard; the early exits make it depend on both.
    assert _guards(x, login, issue.cfg_node) == {
        frozenset({("user is None", "false"), ("svc.verify(user, body.password)", "true")})
    }
    first_raise = _call(login, "HTTPException", 0)
    assert _guards(x, login, first_raise.cfg_node) == {frozenset({("user is None", "true")})}
    # Decorator and default argument calls run at definition time, in the module scope.
    module = _scope(x, None)
    assert {c.callee.get("id") or c.callee["path"][-1] for c in module.callsites} == {
        "post",
        "Depends",
    }
    atoms = [n.get("atom") for n in login.cfg["nodes"] if n["kind"] == "branch"]
    assert {"op": "is_none", "var": "user", "neg": False} in atoms


def test_try_except_finally_routes_every_completion_through_its_own_finally_copy() -> None:
    x = py(
        "def save(session, item):\n"
        "    try:\n"
        "        session.add(item)\n"
        "        session.commit()\n"
        "    except IntegrityError:\n"
        "        session.rollback()\n"
        "        raise\n"
        "    finally:\n"
        "        session.close()\n"
        "    done()\n"
    )
    save = _scope(x, "save")
    graph = C.Graph.of(save.cfg)
    add, commit, rollback = _call(save, "add"), _call(save, "commit"), _call(save, "rollback")
    close, done = _call(save, "close"), _call(save, "done")
    # commit is reached normally only after add completed normally.
    normal = graph.successors(normal_only=True)
    assert commit.cfg_node == add.cfg_node or any(
        t == commit.cfg_node for t, _, _ in normal[add.cfg_node]
    )
    # rollback is reachable only from an exception of add or commit.
    assert set(C.exceptional_sources(graph, rollback.cfg_node)) >= {add.cfg_node}
    assert not _reachable(save, rollback.cfg_node, normal_only=True)
    # One callsite record for close(), evaluated in the normal and the raise copies.
    assert len([c for c in save.callsites if c.callee.get("path") == ["close"]]) == 1
    copies = {close.cfg_node, *close.copies}
    completions = {save.cfg["nodes"][n].get("completion") for n in _entries_before(save, copies)}
    assert completions >= {"normal", "raise"}
    # done() runs only on normal completion: it is not reachable from the raise copy.
    assert _reachable(save, done.cfg_node, normal_only=True)
    raise_entries = [
        n["id"]
        for n in save.cfg["nodes"]
        if n["kind"] == "finally_entry" and n.get("completion") == "raise"
    ]
    assert raise_entries
    assert done.cfg_node not in C.Graph.of(save.cfg).reachable(raise_entries[0])
    assert save.summary["raises"] == [{"k": "reraise"}]


def _entries_before(scope: FlowScope, nodes: set[int]) -> set[int]:
    """finally_entry nodes from which any of ``nodes`` is reached without another entry."""
    graph = C.Graph.of(scope.cfg)
    entries = [n["id"] for n in scope.cfg["nodes"] if n["kind"] == "finally_entry"]
    return {e for e in entries if graph.reachable(e) & nodes}


def test_return_through_finally_does_not_fall_through_to_the_code_after_try() -> None:
    x = py(
        "def f():\n"
        "    try:\n"
        "        if ready():\n"
        "            return value()\n"
        "        work()\n"
        "    finally:\n"
        "        cleanup()\n"
        "    after()\n"
    )
    f = _scope(x, "f")
    after, work = _call(f, "after"), _call(f, "work")
    # after() depends on ready() being false, exactly like work(); the return path through
    # the finally copy for ``return`` can never reach it.
    assert _guards(x, f, work.cfg_node) == {frozenset({("ready()", "false")})}
    assert _guards(x, f, after.cfg_node) == {frozenset({("ready()", "false")})}
    assert f.summary["normal_exit_reachable"] is True


def test_for_else_runs_only_on_exhaustion_and_break_skips_it() -> None:
    x = py(
        "def probe(client):\n"
        "    for attempt in range(3):\n"
        "        if client.ping():\n"
        "            break\n"
        "    else:\n"
        "        notify_down()\n"
        "    proceed()\n"
    )
    probe = _scope(x, "probe")
    graph = C.Graph.of(probe.cfg)
    notify, proceed = _call(probe, "notify_down"), _call(probe, "proceed")
    exhausted = [n["id"] for n in probe.cfg["nodes"] if n["kind"] == "loop_exhausted"][0]
    assert notify.cfg_node in graph.reachable(exhausted)
    breaks = [n["id"] for n in probe.cfg["nodes"] if n["kind"] == "break"]
    assert notify.cfg_node not in graph.reachable(breaks[0])
    assert proceed.cfg_node in graph.reachable(breaks[0])
    assert proceed.cfg_node in graph.reachable(exhausted)
    assert _guards(x, probe, proceed.cfg_node) == set()


def test_infinite_loop_without_break_has_no_normal_exit() -> None:
    x = py("def serve():\n    while True:\n        handle()\n    unreachable()\n")
    serve = _scope(x, "serve")
    assert serve.summary["normal_exit_reachable"] is False
    assert not _reachable(serve, _call(serve, "unreachable").cfg_node)


def test_short_circuit_conditional_expression_and_assert_are_branches() -> None:
    x = py(
        "def g(a, b):\n"
        "    ok = a and check(b)\n"
        "    v = left() if b else right()\n"
        "    assert valid(v), message()\n"
        "    use(v)\n"
    )
    g = _scope(x, "g")
    assert _guards(x, g, _call(g, "check").cfg_node) == {
        frozenset({(str({"op": "truthy", "var": "a"}), "true")})
    }
    assert _guards(x, g, _call(g, "left").cfg_node) == {frozenset({("b", "true")})}
    assert _guards(x, g, _call(g, "right").cfg_node) == {frozenset({("b", "false")})}
    assert _guards(x, g, _call(g, "message").cfg_node) == {frozenset({("valid(v)", "false")})}
    assert _guards(x, g, _call(g, "use").cfg_node) == {frozenset({("valid(v)", "true")})}
    assert any(n.get("condition") == "ASSERTIONS_ENABLED_ASSUMED" for n in g.cfg["nodes"])


def test_match_cases_guards_and_irrefutable_wildcard() -> None:
    x = py(
        "def route(cmd):\n"
        "    match cmd:\n"
        "        case 'go' if armed():\n"
        "            go()\n"
        "        case Stop(reason=r):\n"
        "            stop(r)\n"
        "        case _:\n"
        "            idle()\n"
        "    finish()\n"
    )
    route = _scope(x, "route")
    graph = C.Graph.of(route.cfg)
    go, stop, idle = _call(route, "go"), _call(route, "stop"), _call(route, "idle")
    assert _guards(x, route, go.cfg_node) == {frozenset({("'go'", "c0"), ("armed()", "true")})}
    # The wildcard is irrefutable: no "no match" edge leaves the last case.
    assert not any(e[2] == C.NO_MATCH and e[0] == idle.cfg_node for e in graph.edges)
    assert {b["name"] for b in route.summary["bindings"] if b["kind"] == "match_capture"} == {"r"}
    assert _reachable(route, _call(route, "finish").cfg_node)
    assert stop.cfg_node != idle.cfg_node


def test_with_exit_runs_on_return_and_suppression_is_an_explicit_edge() -> None:
    x = py(
        "def tx(session):\n"
        "    with session.begin():\n"
        "        if cached():\n"
        "            return 1\n"
        "        write()\n"
        "    after()\n"
    )
    tx = _scope(x, "tx")
    exits = [n for n in tx.cfg["nodes"] if n["kind"] == "with_exit"]
    # __exit__ runs for normal completion and for the return: one copy each.
    entries = {n.get("completion") for n in tx.cfg["nodes"] if n["kind"] == "finally_entry"}
    assert entries == {"normal", "return"} and len(exits) == 2
    suppressed = [e for e in tx.cfg["edges"] if len(e) > 3 and e[3] == "suppressed"]
    assert len(suppressed) == 1


def test_scope_kinds_generators_coroutines_comprehensions_and_lambdas() -> None:
    x = py(
        "def gen():\n    yield step()\n"
        "async def agen():\n    yield await step()\n"
        "async def co():\n    await step()\n"
        "class K(Base):\n    x = setup()\n"
        "    def m(self):\n        return [f(i) for i in items() if keep(i)]\n"
        "handler = lambda e: react(e)\n"
    )
    kinds = {s.name or s.kind: s.callable_kind for s in x.scopes}
    assert kinds["gen"] == "generator_function"
    assert kinds["agen"] == "async_generator_function"
    assert kinds["co"] == "coroutine_function"
    assert kinds["K"] == "class_code"
    assert kinds["m"] == "sync_function"
    assert kinds["comprehension"] == "sync_function"
    assert kinds["lambda"] == "sync_function"
    gen = _scope(x, "gen")
    assert gen.summary["yield_points"] == 1
    resume = [e for e in gen.cfg["edges"] if e[2] in {C.RESUME_NORMAL, C.RESUME_THROW}]
    assert {e[2] for e in resume} == {C.RESUME_NORMAL, C.RESUME_THROW}
    module = next(s for s in x.scopes if s.kind == "module")
    k = _scope(x, "K")
    # The class body is executed at definition; the lambda is only created.
    assert [e[1] for e in module.summary["executes"]] == [k.key]
    assert k.kind == "class_body" and _call(k, "setup")
    m = _scope(x, "m")
    comprehension = next(s for s in x.scopes if s.kind == "comprehension")
    assert comprehension.parent_key == m.key
    assert [e[1] for e in m.summary["executes"]] == [comprehension.key]
    assert _call(m, "items")  # the first iterable is evaluated in the enclosing scope
    assert _guards(x, comprehension, _call(comprehension, "f").cfg_node) == {
        frozenset({("loop_head", "true"), ("keep(i)", "true")})
    }


def test_bindings_parameters_and_invokes_parameter_summary() -> None:
    x = py(
        "def run(callback, svc: Service = Depends(get_svc), *rest, key=None, **extra):\n"
        "    client = Client(svc)\n"
        "    for row in client.rows():\n"
        "        callback(row)\n"
        "    with open('x') as handle:\n"
        "        pass\n"
        "    self_ok = (n := compute())\n"
        "    return client\n"
    )
    run = _scope(x, "run")
    params = {p["name"]: p for p in run.summary["parameters"]}
    assert params["svc"]["annotation"] == {"k": "name", "id": "Service"}
    assert params["svc"]["default"]["callee"] == {"k": "name", "id": "Depends"}
    assert [p["kind"] for p in run.summary["parameters"]] == [
        "positional_or_keyword",
        "positional_or_keyword",
        "variadic_positional",
        "keyword_only",
        "variadic_keyword",
    ]
    assert run.summary["invokes_parameter"] == [0]
    bindings = {(b["name"], b["kind"]) for b in run.summary["bindings"]}
    assert {
        ("client", "assign"),
        ("row", "for_target"),
        ("handle", "with_as"),
        ("n", "walrus"),
    } <= bindings
    client = next(b for b in run.summary["bindings"] if b["name"] == "client")
    assert client["value"]["k"] == "call" and client["value"]["callee"] == {
        "k": "name",
        "id": "Client",
    }
    assert run.summary["returns"] == [{"k": "name", "id": "client"}]


def test_analysis_facts_keep_exact_identifiers_and_hash_string_literals() -> None:
    homoglyph = "pаssword_check"  # Cyrillic а
    x = py(f"def f(x):\n    if x == 'admin':\n        {homoglyph}()\n    send('/api/users')\n")
    f = _scope(x, "f")
    assert _call(f, homoglyph).callee == {"k": "name", "id": homoglyph}
    assert not [c for c in f.callsites if c.callee.get("id") == "password_check"]
    atom = next(n["atom"] for n in f.cfg["nodes"] if n["kind"] == "branch")
    assert atom["op"] == "eq" and atom["lit"]["t"] == "str" and "v" not in atom["lit"]
    assert "admin" not in str(f.cfg) and "admin" not in str(f.summary)


def test_display_text_is_bounded_screened_and_flags_bidi() -> None:
    bidi = display("k", "if role == 'user‮':")
    assert bidi.text is not None and "BIDI_CONTROL" in bidi.flags
    token = _token()
    withheld = display("k", f"call({token!r})")
    assert withheld.text is None and withheld.flags == ("WITHHELD_SCREENING",)
    long = display("k", "x" * 1000)
    assert (
        long.text is not None
        and len(long.text.encode()) <= E.DISPLAY_BYTES
        and "TRUNCATED" in long.flags
    )


def test_secret_in_source_fails_the_artifact_without_echoing_it() -> None:
    token = _token()
    x = py(f"KEY = '{token}'\n")
    assert x.outcome == "failed" and x.scopes == ()
    assert token not in repr(x)


def test_owner_binding_uses_the_frozen_parser_position() -> None:
    from uuid import uuid4

    owner = uuid4()
    x = extract_python_flow(b"def f():\n    g()\n", {(1, 0, "f"): owner})
    assert _scope(x, "f").owner_symbol_id == owner and x.outcome == "extracted"
    unmatched = extract_python_flow(b"def f():\n    g()\n", {})
    assert unmatched.outcome == "partial"
    assert [d.code for d in unmatched.diagnostics] == ["FLOW_DECLARATION_UNMATCHED"]


def test_scope_cap_truncates_without_losing_other_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    from buildlens.infrastructure.structural import python_flow

    monkeypatch.setattr(python_flow, "CALLSITES_PER_SCOPE", 3)
    x = py("def big():\n    a(); b(); c(); d()\ndef small():\n    a()\n")
    big, small = _scope(x, "big"), _scope(x, "small")
    assert big.truncated == "FLOW_CALLSITE_LIMIT" and big.callsites == () and big.cfg == {}
    assert small.truncated is None and len(small.callsites) == 1
    assert x.outcome == "partial"


def test_extraction_is_deterministic() -> None:
    source = "def f(a):\n    try:\n        g(a)\n    finally:\n        h()\n"
    assert py(source).evidence_digest == py(source).evidence_digest


# ----------------------------------------------------------------------------- TS/JS


def test_switch_fallthrough_default_and_breaks() -> None:
    x = ts(
        "switch (action.kind) {\n"
        "  case 'save': validate();\n"
        "  case 'submit': submit(); break;\n"
        "  default: reset();\n"
        "}\n"
        "after();\n"
    )
    m = _scope(x, None)
    assert _guards(x, m, _call(m, "validate").cfg_node) == {frozenset({("'save'", "c0")})}
    assert _guards(x, m, _call(m, "submit").cfg_node) == {
        frozenset({("'save'", "c0")}),
        frozenset({("'save'", "no_match"), ("'submit'", "c1")}),
    }
    assert _guards(x, m, _call(m, "reset").cfg_node) == {
        frozenset({("'save'", "no_match"), ("'submit'", "default")})
    }
    assert _guards(x, m, _call(m, "after").cfg_node) == set()


def test_optional_call_and_early_return_in_an_arrow_function() -> None:
    x = ts(
        "const onSubmit = () => {\n"
        "  if (!form.valid) return;\n"
        "  props.onSubmitted?.(form.values);\n"
        "  setBusy(true);\n"
        "};\n"
    )
    arrow = next(s for s in x.scopes if s.kind == "arrow")
    submitted, busy = _call(arrow, "onSubmitted"), _call(arrow, "setBusy")
    assert submitted.callee.get("optional_call") is True
    guards = _guards(x, arrow, submitted.cfg_node)
    assert len(guards) == 1
    (term,) = guards
    assert ("form.valid", "true") in term and len(term) == 2
    assert _guards(x, arrow, busy.cfg_node) == {frozenset({("form.valid", "true")})}


def test_try_catch_finally_jsx_bindings_and_hook_callbacks() -> None:
    x = ts(
        "export function SaveButton({ onSaved }: Props) {\n"
        "  const save = useCallback(async () => {\n"
        "    setBusy(true);\n"
        "    try { const r = await createItem(form); onSaved(r.id); navigate(`/items/${r.id}`); }\n"
        "    catch (e) { setError(describe(e)); }\n"
        "    finally { setBusy(false); }\n"
        "  }, [form]);\n"
        "  return <Panel onClose={close} {...rest}>"
        "<button onClick={save} disabled={busy}>Save</button></Panel>;\n"
        "}\n"
    )
    component = _scope(x, "SaveButton")
    callback = next(s for s in x.scopes if s.parent_key == component.key)
    assert callback.callable_kind == "js_async_function"
    assert _call(callback, "createItem").awaited
    finals = [c for c in callback.callsites if c.callee.get("id") == "setBusy"]
    assert len(finals) == 2 and len(finals[1].copies) >= 1  # one record, several finally copies
    nav = _call(callback, "navigate")
    template = nav.arguments[0]["shape"]
    assert template["k"] == "template" and template["parts"][0]["s"]["path"] == "/items/"
    jsx = {(j.element, j.attribute): j for j in component.jsx}
    assert jsx[("button", "onClick")].intrinsic and jsx[("button", "onClick")].value == {
        "k": "name",
        "id": "save",
    }
    assert not jsx[("Panel", "onClose")].intrinsic
    assert jsx[("Panel", "...")].value == {"k": "starred", "value": {"k": "name", "id": "rest"}}
    params = component.summary["parameters"]
    assert params[0]["pattern"] == "object_pattern"
    assert {b["name"] for b in component.summary["bindings"] if b["kind"] == "param"} == {"onSaved"}


def test_labelled_continue_do_while_for_of_and_logical_assignment() -> None:
    x = ts(
        "outer: for (const row of rows) {\n"
        "  for (let i = 0; i < n; i++) {\n"
        "    if (skip(row, i)) continue outer;\n"
        "    work(row, i);\n"
        "  }\n"
        "}\n"
        "do { poll(); } while (pending());\n"
        "cache ??= load();\n"
    )
    m = _scope(x, None)
    graph = C.Graph.of(m.cfg)
    continues = [e for e in graph.edges if e[2] == C.CONTINUE]
    assert continues and continues[0][3] == "outer"
    # work() runs when the for-of produced a row, i < n held, and skip() was falsy.
    assert _guards(x, m, _call(m, "work").cfg_node) == {
        frozenset({("loop_head", "true"), ("i < n", "true"), ("skip(row, i)", "false")})
    }
    assert any(b["name"] == "row" and b["kind"] == "for_target" for b in m.summary["bindings"])
    load = _call(m, "load")
    (term,) = _guards(x, m, load.cfg_node)
    assert {kind for _, kind in term} == {"true"}  # evaluated only when cache is nullish


def test_ts_owner_binding_and_partial_parse() -> None:
    from uuid import uuid4

    source = "export function ok() { go(); }\nconst = ;\n"
    owner = uuid4()
    start = source.index("export")
    x = extract_ts_flow(
        "a.ts", source.encode(), [(owner, "function", start, source.index("}") + 1)]
    )
    assert x.outcome == "partial"
    assert "SYNTAX_RECOVERED" in [d.code for d in x.diagnostics]
    assert _scope(x, "ok").owner_symbol_id == owner


def test_ts_strict_equality_typeof_and_loose_equality_atoms() -> None:
    x = ts(
        "if (x === 1) a();\n"
        "if (typeof y === 'string') b();\n"
        "if (z == null) c();\n"
        "if (w == 0) d();\n"
    )
    atoms = [n["atom"]["op"] for n in _scope(x, None).cfg["nodes"] if n["kind"] == "branch"]
    assert atoms == ["strict_eq", "typeof", "nullish", "opaque"]


def test_literal_shapes_never_store_arbitrary_text() -> None:
    assert literal("secret-ish") == {
        "k": "lit",
        "t": "str",
        "sha256": __import__("hashlib").sha256(b"secret-ish").hexdigest(),
        "len": 10,
    }
    assert literal(None) == {"k": "lit", "t": "none"}
    assert literal(3) == {"k": "lit", "t": "int", "v": 3}


def test_single_parameter_arrow_without_parentheses() -> None:
    # Regression: found by the Milestone A measurement on 26 real BuildLens TS/TSX files.
    x = ts("const visible = text => text.replace(BIDI, mark);\nitems.map(i => use(i));\n", "a.ts")
    assert x.outcome == "extracted"
    arrows = [s for s in x.scopes if s.kind == "arrow"]
    assert [p["name"] for p in arrows[0].summary["parameters"]] == ["text"]
    assert {b["name"] for b in arrows[1].summary["bindings"] if b["kind"] == "param"} == {"i"}
