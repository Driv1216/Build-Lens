# Stage 16 — Engineering Impact Intelligence

Stage 16 analyzes selected changes against bounded, verified repository evidence. The
implementation covers the supported fragments below. The existing Stage 13–15 qualification
and evaluation records retain their original scope and source identities.

The independent-review corrections use `impact.v5`, `impact-syntax.v8`, and migration
`0021_impact_provenance`. The precise capability matrix, evidence contracts, verification
receipts and outstanding scope proposals are in [STAGE16_CORRECTIONS.md](STAGE16_CORRECTIONS.md).
The understanding gate remains closed pending the recorded owner decisions.

## Use the workbench

Open **Engineering impact** from the project dashboard, Explore, Architecture or Flow Traces.
Select a project and a baseline snapshot. Use a flow node key or a catalog symbol key to look up
upstream exposure in a coherent flow run. For snapshot comparisons, select an after-snapshot
and optionally narrow the comparison to repository-relative source paths. Check the preparation
capabilities for one selected path and both sides before comparing. Prepare the selected
paths on each side for supplemental property comparisons. The workbench shows the syntax
producer match and the supported property domains separately. A domain can be partially
comparable when only some source patterns are supported. Historical snapshots can still supply
retained inventory and compatible continuing declarations if the original bytes are unavailable.
Snapshot comparisons accept an optional `architecture_rule_version` and one to 16 source
paths. The same immutable owner-declared rule set is evaluated for both snapshots. The
workbench uses the same rule version and scope as the CLI and API.

Structured hypotheses support requiring or removing a parameter, removing a return field,
changing a literal boolean return field, and changing a supported integer threshold. They also
support declaration-only rename/removal of eligible Python functions, removal/rename/requiredness
of supported API fields, literal endpoint method/path edits, removal of a matched ORM column,
changes to explicitly declared boolean column constraints, and uniquely matched `os.getenv`
defaults. API field operations select `query`, `body`, or `response`; database constraints select
`nullable`, `unique`, or `primary_key`. Configuration scope `__module__` selects a module lookup.
These are candidate property models. A rename does not rewrite references, and a path denotes
the full local route including a recognized router prefix. All operation preconditions refer
to the common baseline; conflicting candidate names and unsupported composition are rejected. The
developer proposal input recognizes common phrases for these operations and presents candidates
for confirmation. Preparation and analysis run locally. Provider-assisted proposal interpretation
and generated explanations remain deferred. The existing `OutboundGate` and structured generators
can support a future proposal capability. Their current tickets bind exact bytes for semantic
generation or flow explanations. A proposal capability needs its own versioned payload,
schema validation, and persisted single-use confirmation through that same authorization path.

The patch input accepts same-path UTF-8/LF unified diffs over admitted Python, JavaScript and
TypeScript source. Every file must have an exact verified preimage and context. The CLI/API also
accept fingerprint-checked baselines supplied as base64. Candidate source stays in memory.
A candidate has its own digest and facts. Its raw bytes are absent from saved reports.
The parser admits up to eight files and 256 KiB of patch text, with a 1 MiB ceiling on each
preimage and postimage. The repository's source limit can narrow that ceiling. Renames,
file creation and deletion, binary hunks, traversal paths, non-matching context, unused
supplied baselines and unadmitted artifacts are rejected before any facts are used. Supplied
history bytes must match the artifact fingerprint in the selected snapshot. Candidate facts
never enter the baseline preparation cache.
The workbench can show verified inbound exposure from baseline relationships. The candidate
call graph remains unresolved except for supported local source relationships.

## CLI and API

The additive Alembic revisions are `0020_impact` and `0021_impact_provenance`. Use the usual local migration workflow. The
downgrade refuses to remove retained Stage 16 evidence.

The CLI exposes `buildlens impact prepare`, `interpret`, `analyze`, `show` and `verify`.
`analyze` reads the same JSON request accepted by
`POST /api/v1/projects/{project_id}/impact/analyses`. For example, to analyze a prepared
file with a structured hypothesis, supply the project to the CLI and use this request.

```json
{
  "mode": "hypothesis",
  "before": {"snapshot_id": "00000000-0000-0000-0000-000000000001"},
  "locators": ["shop.py"],
  "operations": [
    {"kind": "require_parameter", "target": "reserve", "member": "notify"}
  ],
  "confirm": true,
  "save": true
}
```

Use `POST /impact/preparations` for selected source paths or explicit flow slice sealing,
`POST /impact/proposals/interpret` for local interpretation,
and `GET /impact/analyses/{result_digest}?verify=true` for saved-report verification.
These paths share the same project prefix. Report identity excludes attempt duration and ID.
An explicitly saved patch report retains the candidate and pre/postimage digests and the
screened analytical output. Reconstructing the candidate requires the original inputs.

The request digest binds the project, normalized selection, resolved authorities, contracts,
and the exact patch digest when applicable. The execution-attempt ID identifies one run.
Preparations reached, verified witnesses and any unfinished frontier belong to the result
digest. The same request can produce different partial results at different deadlines. Replay
over the same pinned evidence and completed analysis scope is deterministic over the analytical
payload. Preparing facts again under a different producer, or resolving different upstream runs,
changes the evidence available for replay. Syntax preparation is reusable within the project for
the same locator, verified bytes and producer contract. Semantic links remain scoped to the
snapshot and upstream run that established them.

Preparation publishes each completed artifact in its own transaction. Deadline or source
unavailability can leave a selected scope partially prepared, and a later attempt reuses
the completed immutable work. Explicit flow sealing publishes its summaries atomically.
An analysis can still use retained declarations or relationship exposure when the supplemental
producer or original bytes are unavailable. A structured source hypothesis needs its selected
artifact's compatible preparation.

When a report stops at a resource frontier, use the listed source paths to narrow a snapshot
comparison. Use a frontier node key for a new entity query against the same snapshot and flow
run. Each follow-up has its own scope and request identity. The report's **Show more** control
reveals already returned results and does not restart analysis. Preparation contention, a
request deadline, and a bounded traversal can therefore require different follow-up scopes.

PostgreSQL advisory locks provide one interactive slot and one preparation slot per database,
including across application workers that share that database. The interactive pool also bounds
connection checkout. A disconnect signals the application to cancel at a checkpoint. Active
statements use the remaining wall-clock statement timeout. A verified partial report contains
only completed proof checks. A canceled operation and a timeout without a retained report have
separate receipts. The performance packet measures the full ASGI request, including validation,
database access, computation, verification, response encoding and connection cleanup.

## Propagation semantics

The indexed worklist keys each cell by origin, snapshot side, entity, mechanism, property and
bounded call context. Its state is a finite set of condition alternatives and a set of evidence
gaps. Each alternative is a conjunction of signed atoms. Join takes the union of the alternatives
and gaps. An explicit top state represents alternatives that exceeded the atom or disjunction
budget. This order permits late alternatives to extend a previously processed cell. Transfer
conjoins the edge condition, rejects certified contradictions and requires matching context for
a modeled return. Context exhaustion and traversal limits record an unfinished frontier.
Finite cell and condition bounds, monotone joins and capped expansion guarantee termination.
The three published witnesses are representatives kept separately from the analytical state.

Comparisons use old evidence for removed effects and new evidence for introduced effects.
Continuing entities and properties require shared identity and compatible producers. An absent
fact on an unavailable side leaves the property unknown. A combined signature change can
invalidate a new-side continuation through a now-incompatible caller, so property transfer uses
the supported combined postimage while retaining the originating change. Invocation summaries
describe lexical sites. A changed site count alone leaves execution multiplicity unresolved.

## Evidence and current coverage

- Entity and snapshot propagation use indexed inbound Stage 15 relationship reads. Entity
  lookups can also fall back to verified Stage 13 imports, references and inheritance. They
  report potential exposure with intermediate nodes, edges and cited evidence. Before and after
  snapshot routes retain their sides and origins. A read can use positive, verified witnesses
  from unsealed scopes. It cannot infer absence from those scopes.
- Explicit flow sealing reproduces the upstream graph membership digest and seals count/digest
  summaries for inbound and outbound buckets. Interactive reads check only the relevant
  buckets. The expected whole-run digest is the published Stage 15 graph digest. Sealing checks
  that digest before accepting bucket counts and ordered memberships. Row and witness checks bind
  individual relationships, nodes and supporting evidence back to their producer authorities.
  Complete bucket membership is stronger than verifying each returned row, and unsealed slices
  qualify absence or suppression. Whole-run inspection remains part of explicit preparation.
- The supplemental Python producer supports direct unshadowed same-file calls, selected
  ordered local assignments and field reads, literal returns, explicit raises and one-binding
  integer comparisons. It qualifies unsupported expression order, alias escapes, nested
  scopes and ambiguous continuation. Direct local consumers and transparent return forwarding
  are supported in a subset of JS/TS function declarations. Snapshot comparisons can match a
  synchronous cross-file call by the verified flow callsite’s full source span and caller
  fingerprint, then follow a bounded forwarding chain. A Python signature comparison can
  evaluate a byte-identical continuing cross-file caller under both signatures. Changed
  caller bytes, unresolved dispatch, asynchronous return transfer and general dependence
  remain qualified or unavailable. An oversized Python body can leave dependence unknown
  while its signature remains comparable. Conditional reaching definitions follow surviving
  return paths. A verified direct builtin integer type guard can narrow an unchanged formal
  input for comparison against a signed literal. The resulting regions apply when the shared
  integer input reaches the comparison. Overwritten inputs and unsupported expressions leave
  the regions unknown.
- Architecture rule comparisons read indexed outgoing import and re-export edges from the
  selected paths. Exact module keys and path globs are supported in forbid and layer rules.
  Positive witnesses verify both modules, the dependency edge and its cited Stage 14 evidence.
  A violation observed on one side leaves the other side unresolved when indexed membership
  cannot be established. Container and workspace selectors and overlapping layer membership
  carry explicit gaps.
- FastAPI comparisons cover literal method and path changes, explicit scalar `Query` fields,
  and scalar fields of a simple same-file Pydantic request or response model. Direction matters.
  A continuing client that omits a newly required input is exposed on the request side. A
  client that reads an omitted response field is exposed on the response side. Modeled HTTP
  links retain the Stage 15 qualification and preserve uncertainty about actual traffic.
  Same-file TestClient facts can establish a literal submitted body and an explicit response
  field assertion. Dynamic envelopes, validators, cross-file test fixtures, runtime schema
  and arbitrary client deserializers remain outside these proofs.
- SQLAlchemy comparisons support explicit mapped columns, selected direct projected reads,
  and constructor keyword inputs staged through an annotated `Session`. Cross-file column
  findings require the verified table mapping, an exact callsite span, and prepared bytes
  matching the caller fingerprint. A lexical sequence comparison can detect supported
  session-operation and return ordering changes. Shared-table exposure stays visible when
  column facts are incomplete. Runtime row identity, migration safety, raw SQL value lineage
  and durability remain unresolved.
- Configuration comparisons cover a uniquely aligned `os.getenv` lookup and selected exact
  pins in admitted `pyproject.toml` and `package.json` files. Default values are compared
  by digest. A changed fallback is conditional on no environment override. Effective
  precedence, deployed dependency versions and resulting framework behavior remain unresolved.
- Test intelligence distinguishes potentially executing a route, asserting the affected field,
  and supplying a literal input in the changed integer region. Supported same-file
  `TestClient` instances and parameter fixtures are bound to a known app. Detected override
  setup is visible and effective dependency overrides and condition-specific execution remain
  unmeasured. A verification gap means an applicable assertion was unavailable in the
  inspected scope.
- The workbench shows changes, findings, proof references, verification gaps and limits.
  It orders review by mechanism and known gaps. Independent quality evaluation remains open.

The mandatory fixture exercises a signature change, a returned-field value change and a
conditional side-effect change independently. These regressions are designed evidence.
The independently labelled 24-pair comparative evaluation remains open. The deadline and
resource limits remain scoped product defaults.

## Evaluation tooling

`backend/scripts/impact_evaluate.py` seals local JSON packets and scores normalized observations.
The scored files bind observations to a digest-checked packet and, for quality, to its separately
frozen labels. The quality scorer expects four cases per stratum and reports entity, property,
mechanism, witness and test metrics separately. It treats reviewer independence as a provenance
requirement and excludes abstentions from discoveries. The performance scorer expects two cold
and two warm attempts per corpus and mode. It counts deadline responses and insufficient findings
against completion, and includes cleanup in wall time.

Freeze the actual sources, scopes, required findings and expected completion states before
collecting performance observations. For quality, freeze the source-only cases, have the reviewer
supply expected, forbidden and unknown units, then compare Stage 16 and the two baselines over
the same frozen entity population. The `quality` command with just the packet reports whether
independent labels are available. Add a frozen label file to check readiness and supply normalized
observations only after the reviewer fixed the expected units and canonical entity scope.
Neither command creates review labels or collects corpus measurements.

## Implementation verification

The November 20, 2026 local receipts are retained in
`.buildlens-eval/runs/stage16-implementation-20261120/`. The pinned disposable PostgreSQL
16.11/pgvector 0.8.6 selection passed 32 Python 3.14 tests and both Stage 16 integration
tests on Python 3.11. Coverage included the new migration round trip, sealed-relation deletion
and affected Stage 15 integration tests. The affected offline backend selections passed
444 tests on 3.14 and 427 on 3.11 with 10 skips; the existing lab runtime selection was
excluded after its process-memory supervision failed under the sandbox. The final frontend
lint, production build and 56 tests passed. The browser smoke prepared a synthetic source,
confirmed a parameter hypothesis, opened the compatibility witness and reloaded the saved
report. The CLI verified that same report. The evaluation scorer has synthetic regression
coverage. No independent comparative or corpus latency gate was evaluated.

The follow-up source-only 24-pair packet is retained in
`.buildlens-eval/runs/stage16-followup-20261120/quality-source-packet.v2.json`.
It includes the three independent variants of the same reserve fixture.
The corresponding label template contains no reviewer labels.

The follow-up implemented verified, callsite-specific cross-file return and Python
signature findings, directional literal-route exposure, and two-sided table exposure.
The PostgreSQL 16 focused regressions covered a cross-file forwarding chain, discarded and
overwritten results, incompatible calls in a combined edit, and the existing HTTP inference
caveat. The first frozen 60-observation campaign measured 20 qualifying completions each on
the FastAPI template and Polar. BuildLens qualified on 8 of 20 requests. Its oversized target
body suppressed required signature evidence for hypotheses, comparisons and overlays. The
campaign failed the BuildLens completion gate. Application-cold samples used new processes and
pools with process startup outside the request timer. PostgreSQL buffers were uncontrolled
with equal case priming. The retained cancellation and fan-out probes demonstrated release of
the active slot without saving a canceled result and qualified relationship-page truncation.

The one allowed correction reused the packet, preserved both snapshot authorities, and
re-extracted the six fingerprint-checked input artifacts under the updated syntax producer.
All 60 cases met their frozen completion and evidence expectations within two seconds. The
slowest measured request was 0.909 seconds. The 48 qualified results were expected to carry
incomplete-evidence status for their declared scopes. The remaining 12 were complete proposal
interpretations. Receipts and source identity are in the follow-up `performance-correction/`
directory. This run measures the recorded analyzer revision and local test conditions.

After the request identity and dependence refinements, five targeted BuildLens requests spanning
all modes still met the frozen requirements. The affected backend selection passed 24 tests
on each of Python 3.11 and 3.14. The focused database regression also passed for changed row
content, a modified retained witness and missing indexed relationships. Frontend lint, build and
all 56 tests passed. The quality readiness receipt remains inconclusive for all 24 pairs because
independent labels are unavailable.

The November 21 closure for `impact.v4` retained its analyzer identity and exit-status receipts
in `.buildlens-eval/runs/stage16-closure-20261121/`. The affected backend checks passed 42 tests
on Python 3.14, including the Stage 14 and Stage 15 integration selection, and 28 on Python 3.11.
Frontend lint, production build and 57 tests passed. The final analyzer met the frozen
requirements on a targeted BuildLens request in each of the five modes. The full 60-observation
score remains tied to the earlier measured revision. The independently labelled quality gate
remains inconclusive.

The November 21 Codex-authored exploratory review covers all 24 frozen quality pairs.
It collected and reverified 24 reports against 103 expected claims. Stage 16 recovered 49
of 53 expected structural entities and 21 of 32 expected property claims. Its entity recall
fell below both dependency baselines. The short-circuit case also missed its frozen
completion expectation. Independent quality acceptance remains inconclusive. The sealed
labels, raw reports and reproduced score are in
`.buildlens-eval/runs/stage16-codex-review-20261121/`.
