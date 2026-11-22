# Stage 16 — Independent-review corrective implementation report

Date: 2026-11-21. This is one bounded corrective pass against the five independent findings.
It preserves the existing working tree, historical evaluation packets and Stage 13–15 qualifications.
It does not establish independent acceptance or open the understanding gate.

## Outcome

C1–C4 have corrective implementations and focused verification. C5 now has concrete bounded
operations for all five named input families, together with artifact membership classification.
Broader variants of those families remain unsupported; the explicit scope proposals below require
owner acceptance. The understanding gate remains closed until that scope decision is resolved.

The analytical contract is `impact.v5`; the supplemental producer is `impact-syntax.v8`.
Migration `0021_impact_provenance` is additive and was exercised in disposable databases.
The final retained 448-file analyzer manifest has digest
`b35bace1a962ea977a087a19bc23668989b978a25c4eac03faf48d8d5714d319`.
Its original 445-file baseline was `74de18f1c49b3ede7e7c380732853d984dff86e1302aa1e8c82cb8aadb55e77f`.
The three additional files are the migration and two correction test modules.

Evidence is in `../.buildlens-eval/runs/stage16-corrections-20261121/`.
`final-retained-source-identity.json` identifies the final source;
`closure-verification.json`, the job receipts, and the earlier manifests retain each verification
revision independently. Existing historical receipts were not relabelled as final-revision passes.

## Findings and corrections

### C1 — False completeness

Before correction, changing `flag and emit()` to `flag or emit()` retained the same generic guarded
call shape and could report `complete_for_scope`. Python async declarations could be omitted while
the projection was marked complete.

`_Body.expression` now records `SHORT_CIRCUIT_CONDITION_COMPARISON_UNAVAILABLE` for unresolved
short-circuit conditions. Constant short-circuit stops still exclude operands that do not execute.
`_python` records omitted async/class bodies and unmodeled module execution. It retains a separate
body syntax digest. `compare_syntax` collects producer and function gaps, reports partial body/module
comparisons, and qualifies changed declaration semantics, including annotations and decorators.
Exact supported property differences remain reportable alongside those gaps.

This correction uses conservative uncertainty instead of introducing a new symbolic condition
engine. Positive signature/field cases, constant-short-circuit decoys, changed scalar returns,
async/class bodies, annotations and decorators have focused regressions.

### C2 — JavaScript value-transfer barriers

Before correction, `async function p(){ return {a:true}; }` followed by `p().a` could produce a
derived consumer finding for the resolved object's field. Container aliases could also preserve
unsafe field precision.

`_javascript` now retains a Promise result kind. `_findings` and `_field_consumers` reject precise
field transfer across that barrier. `_JsBody.expression` stops precise binding propagation across
unsupported containers and `await`, recording the relevant escape or unwrapping gap. Direct
synchronous field consumption remains supported. Discarded results and sibling-field consumers
remain negative controls. General Promise unwrapping and heap analysis remain unavailable.

The async reproduction, explicit await fallback, synchronous positive control, object/array escapes
and unrelated consumers have focused tests. Overall partial status is no longer used to excuse an
incorrect derived field mechanism.

### C3 — Preparation identity and cache reuse

Before correction, facts were hashed without their input identity. Editing a preparation fingerprint,
locator or producer could leave its proof unchanged.

New preparations carry `source_identity` and `input_seal_digest`. The seal binds the preparation ID,
project, originating snapshot, locator, input fingerprint, syntax/extraction contract, facts digest,
artifact identity, snapshot manifest/policy, fingerprint scheme/reader, language and admission
metadata. `_checked_preparation` verifies the originating snapshot and artifact relationship as well
as the seal. Current selection still requires an admitted artifact and matching bytes/producer.
Syntax reuse does not grant semantic relationship reuse across snapshot authorities.

Migration `0021` leaves old records unsealed. The reusable-input uniqueness index applies only to
sealed records, permitting a fresh extraction to coexist with retained legacy facts. Legacy rows
are never sealed merely by hashing their current contents. They remain inspectable with explicit
unverified provenance; fresh trusted preparation requires fingerprint-verified bytes. Historical
bytes being unavailable therefore remains a legitimate capability boundary.

Tests cover each preparation identity column, seal removal, valid identical-byte reuse across
snapshots, live fingerprint tampering, legacy inspection, and fresh preparation beside a legacy row.
The seal is a consistency digest anchored in retained producer/report identities, not protection
against a privileged actor rewriting every trusted root.

### C4 — Retained evidence, freshness and compatibility

Before correction, declaration-only and singular-local-witness reports could bypass important
evidence and freshness checks.

`declarations` now supplies a proof binding symbol identity, source observation, producer, input
fingerprint, signature, declaration state and span. `_retained_declarations` records both proofs.
`_verify_declaration_changes` rechecks old and new observations and producer identities.

`_properties` records comparison inputs and digests. Snapshot inputs reference sealed preparations;
hypotheses retain a screened candidate property projection; patches retain analysis-safe projections
bound to their overlay facts digests. Raw patch/preimage bytes and raw configuration-default
strings are not retained. `_verify_comparisons` checks consumed inputs and supported local findings.
Historical contracts are qualified when current rules cannot safely replay their comparison.

`verify_report` checks captured authority even without graph paths, verifies inventory observations,
and accepts either singular or plural graph witness presentation. Missing local provenance qualifies
the verification. Modified/missing consumed evidence fails closed. Retained graph witnesses still
use Stage 16's own intermediate-row verification; Stage 15 F3 was not reopened.

`show(..., verify=True)` separately returns:

| Field | Meaning |
| --- | --- |
| `integrity` | Verified retained evidence, partial provenance, or unverified legacy provenance |
| `freshness` | Current authority or stale relative to active pointers |
| `compatibility` | Current or changed analytical/producer contract |
| `evidence_verification` | Complete verification of consumed evidence or explicitly partial |
| `analysis_completeness` | The retained analysis's original completeness classification |

`status: verified` requires no outstanding qualification reasons. A historically valid but stale
report remains readable. The browser displays these dimensions separately, and the CLI returns the
same service result.

Live tests verify local-only reports, declaration-only reports, alterations on both sides, changed
producer identity, absent local proof references, legacy contracts and stale authority. API and CLI
verification operate on the same saved result.

### C5 — Change-input capabilities

The validator, property model and workbench now expose 15 operations. Each requires matching baseline
facts. Unsupported source shapes are rejected rather than converted to authoritative properties.
Module operations validate preconditions against the common baseline. Duplicate writes, unsupported
sequential composition, and candidate rename collisions are rejected.

`PostgresImpactView.absent` and `_snapshots` distinguish established artifact membership from unknown
observation: the missing side must have complete nonlegacy inventory and no excluded/pruned parent;
the present side must be admitted and fingerprinted. Incomplete inventory, exclusion, missing
fingerprints and unsupported observations retain `unavailable`. Artifact introduction/removal alone
does not establish every contained property; reports explicitly retain that unexamined scope.

## Supported-capability matrix

All structured operations below use confirmed, prepared property models through the shared
application service, API, CLI JSON request and browser. Local proposal grammar retains its original
five common phrases; other proposals require structured input or remain unsupported.

| Capability | Supported now | Explicit boundary |
| --- | --- | --- |
| Symbol rename | `rename_symbol`, `target`, `new_name`: uniquely bound, undecorated top-level synchronous Python function in the supported static module | Declaration-only rename. References are not rewritten. Unchanged direct callsites receive qualified removed-target exposure. Classes, methods, async declarations and export/import alias rewrites are unsupported. |
| Symbol removal | `remove_symbol` with the same declaration prerequisites | Replacement/builtin/external binding remains unknown; no unconditional runtime `NameError` claim. |
| Parameters | Existing `require_parameter` and `remove_parameter` | Existing explicitly observed parameter, supported binding kinds and valid combined signature required. |
| Return properties | Existing `remove_return_field`, `set_return_boolean`, `set_integer_threshold` | Literal fields and the established integer fragment; no general return equivalence or precise async unwrapping. |
| API fields | `remove_api_field`, `rename_api_field`, `require_api_field`; `family` is `query`, `body` or `response` | One uniquely matched local route, explicit scalar Query or simple same-file Pydantic fields. Unsupported schemas are rejected. Structured field addition/type/default edits remain unavailable. Snapshot comparison can observe supported field membership/type/requiredness changes on continuing routes. |
| Endpoint method/path | `set_endpoint_method`, `set_endpoint_path` | Literal recognized method and full local route, including any known router prefix. Mounting, rewrites, dynamic routes and external clients remain qualified. Structured endpoint creation/removal is unavailable. |
| Database column removal | `remove_database_column`, model target and column member | Explicit mapped column and matched model inventory. Existing local column consumers remain distinguishable from table exposure. |
| Database constraints | `set_database_constraint`; boolean `value`; `constraint` is `nullable`, `unique` or `primary_key` | Old value must already be an explicit supported boolean declaration. Implicit/inferred constraints are not silently invented. |
| Other database changes | Snapshot/patch comparison retains the existing matched column additions/removals, selected declaration differences and session-operation ordering | Structured column additions/renames/type/default edits and literal Alembic-operation comparison remain unsupported. No deployed migration, row identity or durability proof. |
| Configuration default | `set_configuration_default`, unique lookup scope, environment key member, scalar value; `__module__` selects module scope | Direct unshadowed `os.getenv`, one supported lookup per scope, known old literal kind. Bool/int/string/null property values only; strings are digested. Effective overrides and precedence stay unknown. JS environment defaults and arbitrary configuration systems remain unavailable. |
| Artifact additions/removals | Snapshot pairs classify admitted, fingerprinted presence against established complete searched absence | Ignored/pruned parents, incomplete/legacy inventory and unavailable observations remain unknown. Exact patches retain their existing no-file-operation boundary. |

Examples for the new structured model:

```json
{"kind":"rename_symbol","target":"reserve","new_name":"reserve_items"}
{"kind":"remove_api_field","target":"items","family":"response","member":"ready"}
{"kind":"set_endpoint_method","target":"items","value":"PUT"}
{"kind":"set_database_constraint","target":"Item","member":"quantity","constraint":"nullable","value":false}
{"kind":"set_configuration_default","target":"settings","member":"CACHE_MODE","value":"public-default"}
```

These are operation objects within the existing hypothesis request, not independent executable edits.

## Scope proposals requiring owner decision

The bounded matrix is implemented. It must not silently replace the approved plan's broader wording.
The following are proposed deferrals, not already approved acceptance changes:

1. **Other symbol kinds and reference-preserving renames.** The supplemental candidate model contains
   top-level functions; the upstream catalog alone cannot establish candidate descriptor, inheritance,
   export or import rebinding. Extending these claims needs a versioned candidate declaration/binding
   projection and corresponding relinking rules. Proposed boundary: retain the function subset above.
2. **General structured schema edits and migration declarations.** New columns/fields, types/defaults,
   endpoint membership and Alembic operations need additional typed candidate specifications and
   producer/comparator contracts. ORM attribute identity, wire field identity and migration operation
   identity cannot be interchanged safely. Proposed boundary: retain the implemented field, constraint
   and literal-route operations, with snapshot/patch fallback only for properties already supported.
3. **JS configuration defaults and broader configuration consumers.** Current configuration authority
   is the uniquely bound Python lookup and selected manifest declarations. JS environment binding,
   fallback operators and build-time substitution require their own extraction and conditional
   consumer rules. Proposed boundary: retain the Python/scalar subset and explicit unknowns.

These proposals keep this corrective pass bounded. An owner may accept the matrix or require a
separately scoped extension. Until then, C5's broader acceptance reconciliation remains blocking.

## Verification and provenance

Both required execution skills were followed. Long/database selections used the installed background
runner, with terminal exit statuses retained. Short focused checks ran directly and saved outputs.

| Evidence | Observed result and revision qualification |
| --- | --- |
| Final affected unit/scorer selection | **56 passed on Python 3.14 and 56 on 3.11**; `closure-check-1.log`, `closure-check-2.log` |
| PostgreSQL affected selection | **9 passed on 3.14**: new correction cases, existing impact regressions and migration/ORM parity; job `06ca644cb62a` |
| Final Python 3.11 database correction cases | **4 passed**; job `b1f3fdb52b7d` |
| Explicit pruned-parent absence check | **1 passed on 3.14**, including complete, incomplete and ignored-parent inventory; job `8fbf3a6ce041` |
| Static checks | Ruff and mypy passed. The retained final-check run first caught an `Any` return; its later local annotation fix passed. `typing-only-receipt.json` proves equal AST after erasing annotations. The final C5 composition guard has passing unit/static evidence. |
| Frontend | Lint, production build and **59 tests passed**; job `a0c7cdc827b5` |
| API/CLI | A saved local hypothesis was analyzed over HTTP, verified over HTTP and verified by the real CLI; the integration receipt contains its digest and request timing. |
| Browser | Isolated cached Chromium completed hypothesis confirmation, save, current verification and historical stale-authority inspection. No JavaScript or API failures. A pre-existing favicon 404 made the wrapper exit 1; the raw failure and its nonblocking assessment are retained. Desktop bridge initialization timed out twice before the isolated fallback. |

The nine-check integration receipt predates the final declaration qualification. That qualification
has focused C1 tests, both final unit selections and the comparison smoke below. Later changes were
the annotation-only correction, C5 composition validation, and a test-only pruned-parent fixture.
Each corresponding source identity is retained; the report does not imply every receipt ran over
the final byte-for-byte tree.

Five frozen BuildLens requests were measured on an isolated copy of its evaluation database:
all five met their original declared result requirements, maximum **0.994 seconds**. After the final
C1 qualification, only its affected snapshot/patch modes were rechecked: **0.398 / 1.019 seconds**,
both meeting the frozen requirements. These measurements precede the later annotation-only and
new-operation composition guards. The latter do not change the measured original operations.
The original evaluation database, authorities and scores were preserved. This is no new full
three-corpus campaign or population latency guarantee.

The final file manifest differs from the preceding closure manifest only in the last test-fixture
extension. `closure-verification.json` and the final retained manifest preserve that distinction.

## Historical results and inherited qualifications

The original 24-pair score remains **inconclusive**, with explicitly non-independent Codex labels.
Its 49/53 entity and 21/32 property recoveries and original short-circuit completion mismatch remain
historical observations. The corrected short-circuit case now produces a specific incomplete status;
the 24-pair experiment and its scores were not rerun or overwritten.

The initial performance failure and the corrected **60/60** pass, maximum **0.909 seconds**, retain
their earlier analyzer identities. Expected incomplete-evidence results remain distinct from
timeouts, unexpected truncation and missing required findings.

Stage 13 real-model/recognizer/schema qualifications, Stage 14 human API labels and remediation
re-review, and Stage 15 independent/cross-layer evaluation, TypeScript precision, F2/F3/F5 and
historical performance qualifications remain unchanged. Final F1 and F4 corrections retain their
recorded status. Inferred HTTP crossings keep `HTTP_LINK_INDEPENDENT_EVALUATION_PENDING`.

## Exact implementation files

- `backend/src/buildlens/domain/impact.py`: analytical version.
- `backend/src/buildlens/infrastructure/structural/impact.py`: `_Body.expression`, `_python`,
  `_JsBody.expression`, `_javascript`, producer version.
- `backend/src/buildlens/application/impact_properties.py`: `compare_syntax`, `_findings`,
  `_field_consumers`, `_scalar`, `_module_operation`, `hypothetical`.
- `backend/src/buildlens/application/impact.py`: `Operation`, port/DTO contracts, `_snapshots`,
  `_retained_declarations`, `_prepared`, `_hypothesis`, `_patch`, `_properties`, `show`.
- `backend/src/buildlens/infrastructure/persistence/impact.py`: `absent`, preparation seal/source
  checks, `declarations`, `_verify_comparisons`, `_verify_retained_authority`,
  `_verify_declaration_changes`, `verify_report`; local type annotation in `_architecture_run`.
- `backend/src/buildlens/infrastructure/persistence/models.py` and
  `backend/alembic/versions/0021_impact_provenance.py`: sealed-input persistence and legacy retention.
- `frontend/src/api/impact.ts`, `frontend/src/components/impact/ImpactPage.tsx`,
  `frontend/src/components/impact/Impact.test.tsx`: typed operations and separated verification UI.
- New `backend/tests/unit/test_impact_corrections.py` and
  `backend/tests/integration/test_impact_corrections.py`: focused correction evidence.
- `CURRENT_STATE.md`, `docs/IMPACT_ANALYSIS.md`, and this report: current status and scope disclosure.

## Remaining classification and stopping point

**Blocking:** owner resolution of the explicit C5 scope proposals. No remaining reproduced C1–C4
defect is being carried as a successful correction.

**Nonblocking disclosed limits:** conservative short-circuit and async abstention, bounded local
dependence, incomplete runtime/external evidence, legacy provenance qualifications, limited final
latency observations, and the existing favicon asset omission. Independent quality acceptance is
still inconclusive under the approved evaluation contract.

No Stage 17 work or second independent review was initiated. This report ends the corrective pass;
the understanding gate remains closed.
