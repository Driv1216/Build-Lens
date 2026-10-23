# Draft benchmark assets

`buildlens-v1.draft.json` contains 30 agent-authored questions, six per category.
`buildlens-v1.draft-evidence.json` contains their exact proposed source spans and excerpts.
`buildlens-v1.draft-authoring.py` is an explicit authoring aid, never called by evaluation.

Status: **draft, pending human approval, corpus not pinned, no approved relevance judgments**.
The draft uses conservative whole-declaration targets. Review and narrow them where the required
fact needs a smaller span; they are proposals rather than established ground truth. Unsupported
cases contain plausible code distractors and missing production/runtime facts, not an assumed
absence of a feature.

Prepare a new compatible snapshot explicitly after the shared exclusion boundary exists, inspect
and pin its corpus manifest, resolve the proposed evidence, and record a new reviewed definition
revision. Do not rerun the authoring aid against a frozen approved version. Run directories and
working answer reviews remain Git-ignored. Git tracking never makes this directory ingestible.

See `docs/EVALUATION.md` for commands, review fields and independent verification states.

## Bound 2026-10-02 revision

`corpus-20261002.json` pins the newly prepared Stage 11 PostgreSQL/native evidence.
`buildlens-v3.bound-draft.json` contains all 30 exactly bound cases, still draft with no relevance
judgments or approval. Its evidence companion has exact proposed excerpts; v2/v3 revision files
record explicit agent reauthoring and 11 case refinements. The original v1 draft is preserved.
The real 90-result run is `runs/retrieval-20261002`; human candidate review packets are in
`reviews/relevance-20261002`. Human labels/approval are pending; do not treat binding or benchmark
execution as approval. See the actual measured results and unsupported-corpus limits in
`docs/EVALUATION.md`. No provider answer was generated; external Gemini sharing authorization
remains pending after automatic approval review rejected the preflight.
