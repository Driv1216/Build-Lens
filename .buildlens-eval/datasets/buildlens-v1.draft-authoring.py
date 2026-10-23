"""Explicit draft authoring aid; never invoked by eval commands. No approval is assigned."""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent
A = "backend/src/buildlens/application/"
D = "backend/src/buildlens/domain/"
I = "backend/src/buildlens/infrastructure/"
H = "backend/src/buildlens/api/"
EXCERPTS = {}


def target(case_id, index, locator, symbol):
    raw = (ROOT / locator).read_bytes()
    source = raw.decode("utf-8")
    lines = source.splitlines(keepends=True)
    if locator.endswith(".py"):
        nodes = {}
        def walk(node, prefix=""):
            for child in ast.iter_child_nodes(node):
                scope = prefix
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope = f"{prefix}.{child.name}" if prefix else child.name
                    nodes[scope] = child
                walk(child, scope)
        walk(ast.parse(source))
        node = nodes[symbol]
        start = sum(map(len, lines[:node.lineno - 1])) + len(lines[node.lineno - 1].encode()[:node.col_offset].decode())
        end = sum(map(len, lines[:node.end_lineno - 1])) + len(lines[node.end_lineno - 1].encode()[:node.end_col_offset].decode())
    else:
        start = source.index(symbol)
        end = source.find("\n\n", start)
        if end == -1:
            end = len(source)
    key = f"{case_id}.e{index}"
    excerpt = source[start:end]
    record = {"id": key, "locator": locator,
              "artifact_fingerprint": hashlib.sha256(raw).hexdigest(),
              "start_offset": start, "end_offset": end,
              "excerpt_hash": hashlib.sha256(excerpt.encode()).hexdigest(), "label": symbol}
    EXCERPTS[key] = {**record, "excerpt": excerpt}
    return record


# Concrete source spans are proposals for human review, not approved relevance labels.
ENTRIES = {
    "identifier": [
        ("Which function applies BuildLens path exclusions, including excluded ancestors?", [(D + "eligibility.py", "exclusion_reason", "Identify the shared exclusion function and ancestor checks.")]),
        ("Where is canonical Stage 9 evidence serialized for UTF-8 byte measurement?", [(A + "context.py", "ContextSizer.serialize", "Identify the canonical evidence serializer and its framing.")]),
        ("Which function validates grounded statements and evidence IDs?", [(A + "grounded_prompt.py", "validate_output", "Identify strict output validation and citation membership checks.")]),
        ("Which method verifies PostgreSQL retrieval authority and freshness?", [(I + "persistence/retrieval.py", "_SqlAlchemyRetrievalSession.verify", "Identify authority verification and its freshness gates.")]),
        ("Which method admits candidates to the local cross-encoder?", [(A + "context.py", "ContextService._rerank", "Identify reranker admission and the 32-candidate bound.")]),
        ("Where is the native processing compatibility contract assembled?", [(D + "source.py", "processing_contract", "Identify the versioned processing contract builder.")]),
    ],
    "semantic": [
        ("How does BuildLens measure context exactly when source contains Unicode or JSON escapes?", [(A + "context.py", "ContextSizer", "Explain canonical JSON framing and UTF-8 byte measurement.")]),
        ("What happens when Ask receives no usable context?", [(A + "ask.py", "AskService.ask", "Explain insufficient-evidence status and skipped counting and generation.")]),
        ("How are expanded neighbours prevented from surviving without their supporting seeds?", [(A + "context.py", "ContextService.repack", "Explain surviving-seed support during whole-item repacking.")]),
        ("How does reranking handle a candidate that exceeds the reranker input window policy?", [(A + "context.py", "ContextService._rerank", "Explain explicit input-limit exclusion and validation without fallback.")]),
        ("How does BuildLens detect changed evidence after answer generation completes?", [(A + "ask.py", "AskService.ask", "Explain final evidence verification after output validation.")]),
        ("How does BuildLens keep repository text from becoming OpenAI system instructions?", [(I + "generation/openai.py", "_prepared", "Explain separate system rules and escaped untrusted evidence JSON.")]),
    ],
    "cross_file": [
        ("Trace an Ask HTTP request through production statement and citation validation.", [(H + "ask_routes.py", "ask", "Map the HTTP request into AskService."), (A + "ask.py", "AskService.ask", "Describe the grounded generation and validation flow."), (A + "grounded_prompt.py", "validate_output", "Explain evidence ID checks.")]),
        ("Trace context expansion from selected seeds through the PostgreSQL neighbour query.", [(A + "context.py", "ContextService.build", "Describe selection and expansion."), (I + "persistence/retrieval.py", "_SqlAlchemyRetrievalSession.neighbours", "Explain adjacent split-part selection and filters.")]),
        ("Trace shared exclusions from inventory discovery through secure source reads.", [(I + "discovery/filesystem.py", "LocalRepositoryInspector.inspect", "Explain inventory pruning."), (I + "structural/reader.py", "PosixSecureSourceReader._consume", "Explain direct-reader exclusion checks."), (D + "eligibility.py", "exclusion_reason", "Explain excluded ancestors.")]),
        ("Trace generation budget checking through repacking and Gemini generation.", [(A + "ask.py", "AskService.ask", "Explain exact counting and selected generation evidence."), (A + "context.py", "ContextService.repack", "Explain whole-item repacking and seed support."), (I + "generation/gemini.py", "GeminiProvider.generate", "Explain actual generation request construction.")]),
        ("Trace HTTP runtime ownership and worker/provider cleanup.", [(H + "main.py", "create_app", "Describe lifespan ownership of the shared runtime."), (I + "runtime.py", "LocalRuntime.close", "Describe worker, provider and database-engine cleanup.")]),
        ("Trace final context verification into PostgreSQL processing and embedding authority checks.", [(A + "context.py", "ContextService._final_verify", "Explain final source rehydration."), (I + "persistence/retrieval.py", "_SqlAlchemyRetrievalSession.verify", "Explain authority validation under the snapshot lock.")]),
    ],
    "architecture": [
        ("How are embeddings, reranking and generation dependencies kept optional?", [("backend/pyproject.toml", "[project.optional-dependencies]", "Explain optional dependency groups and the base installation.")]),
        ("What are the Stage 9 context limits and how are they configured?", [("backend/src/buildlens/core/config.py", "Settings", "Identify configurable byte, item and timeout limits."), (D + "context.py", "ContextPolicy", "Describe candidate, reranker, seed, item and byte budgets.")]),
        ("How does BuildLens select Gemini versus OpenAI and enable Ask?", [("backend/src/buildlens/core/config.py", "Settings", "Explain the configured provider and opt-in Ask flag."), (I + "runtime.py", "LocalRuntime.__init__", "Explain provider construction without fallback.")]),
        ("What thinking setting does the Gemini policy request, and what is actually transmitted?", [(D + "ask.py", "GenerationPolicy.for_provider", "Describe the requested Gemini policy."), (I + "generation/gemini.py", "GeminiProvider.generate", "Explain the omitted thinking argument and unknown effective setting.")]),
        ("How does hybrid retrieval combine component ranks and resolve ties?", [(A + "retrieval.py", "RetrievalService._rank_hybrid", "Explain exact precedence, RRF and deterministic tie-breaking.")]),
        ("How does the local HTTP application check mutation origins and content types?", [(H + "main.py", "create_app", "Explain allowed local origins and JSON admission.")]),
    ],
    "unsupported": [
        ("What was this installation's total billed Gemini cost yesterday?", [(D + "ask.py", "GroundedAskResult", "Per-request token usage is a distractor, not billing history.")]),
        ("What is the current GPU temperature of the deployed BuildLens machine?", [("backend/src/buildlens/core/config.py", "Settings", "Worker resource settings are a distractor, not live GPU telemetry.")]),
        ("How many unique users queried the deployed API in the last 24 hours?", [(H + "main.py", "create_app", "HTTP routes are a distractor, not production traffic history.")]),
        ("Which processing run is active right now in the production database for project payments-service?", [(I + "persistence/retrieval.py", "_SqlAlchemyRetrievalSession.verify", "Authority checks are a distractor, not that production project's state.")]),
        ("What was the deployed service's measured p95 Ask latency this week?", [(D + "ask.py", "GroundedAskResult", "Per-answer durations are a distractor, not a weekly percentile.")]),
        ("At what UTC time will the next production outage occur, and how long will it last?", [(D + "ask.py", "GenerationPolicy", "Timeout settings are a distractor, not future outage evidence.")]),
    ],
}


def main():
    cases = []
    for category, entries in ENTRIES.items():
        for number, (question, groups) in enumerate(entries, 1):
            case_id = f"{category}-{number:02d}"
            targets = [target(case_id, i, locator, symbol) for i, (locator, symbol, _) in enumerate(groups, 1)]
            answerable = category != "unsupported"
            cases.append({"id": case_id, "question": question, "category": category,
                          "filters": {"locator_prefix": None, "language": None, "kind": None},
                          "answerable": answerable,
                          "required_facets": [facet for _, _, facet in groups] if answerable else [],
                          "forbidden_claims": ["Invented runtime values or unobserved production facts."] if not answerable else ["Claiming runtime behavior or verified outcomes absent from the evidence."],
                          "targets": targets,
                          "requirements": [{"id": f"{case_id}.r{i}", "fact": facet,
                                            "alternatives": [[targets[i - 1]["id"]]]}
                                           for i, (_, _, facet) in enumerate(groups, 1)] if answerable else [],
                          "judgments": []})
    dataset = {"schema_version": "stage11.dataset.v1", "id": "buildlens-native", "version": "1-draft",
               "corpus_digest": None, "corpus_manifest": None, "review_status": "draft", "approval": None,
               "rubric_version": "stage11.human-review.v1", "cases": cases}
    for name, value in (("buildlens-v1.draft.json", dataset), ("buildlens-v1.draft-evidence.json", EXCERPTS)):
        (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(f"Authored {len(cases)} drafts; no corpus pinned, no judgments or approval assigned.")


if __name__ == "__main__":
    main()
