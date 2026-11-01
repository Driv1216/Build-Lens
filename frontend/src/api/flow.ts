import { request } from './client'
import type { Tier } from './architecture'

// ---- Stage 15: flow evidence (A), the flow model (B) and FlowTraces (C). Read-only except
// the explicit build and pin actions; every read is verified server-side and fails closed.

export type FlowEvidenceRun = { run_id: string; revision: number; outcome: string; coverage: Record<string, unknown>; superseded: boolean }
export type FlowEvidenceStatus = {
  latest_attempt: FlowEvidenceRun | null
  active_run: FlowEvidenceRun | null
  freshness: 'fresh' | 'stale' | 'none'
  freshness_reasons: string[]
  structure_available: boolean
}
export type FlowRun = {
  run_id: string
  revision: number
  outcome: 'complete' | 'partial' | 'failed'
  graph_digest: string | null
  coverage: Record<string, unknown>
  failure_code: string | null
  superseded: boolean
  contract: Record<string, unknown>
}
export type FlowStatus = {
  latest_attempt: FlowRun | null
  active_run: FlowRun | null
  freshness: 'fresh' | 'stale' | 'none'
  freshness_reasons: string[]
  inputs_ready: boolean
}

export type EntryKind = 'http_endpoint' | 'dom_event' | 'effect' | 'cli_command' | 'asyncio_run' | 'module'
export type FlowEntry = {
  key: string
  kind: EntryKind
  label: string
  locator: string | null
  target: string | null
  tier: Tier | null
  conditions: string[]
  reason?: { hit_rank: number; chunk_locator: string | null; matched_scope: string; distance: number }
}
export type EntryPage = {
  run_id: string
  freshness_reasons: string[]
  mode: string | null
  requested_mode?: string
  items: FlowEntry[]
  total: number
  selection?: string
}

export type Atom = {
  op: string
  value: boolean
  var?: { scope: string; name: string; definitions: number[] }
  lit?: unknown[]
  typeof?: string
  reason?: string
  branch?: { scope: string; cfg_node: number }
}
export type Feasibility = 'refuted_in_fragment' | 'consistent_in_fragment' | 'undetermined'
export type Reachability = { verdict: string; code?: string; raised_at_cfg_nodes?: number[] }
export type TraceHop = {
  id: string
  node: string
  kind: string | null
  label: string | null
  locator: string | null
  context: string | null
  depth: number
  lane: string
  layer: string
  identity: string | null
  phase?: string
}
export type TraceStep = {
  id: string
  from: string
  to: string | null
  edge: string
  relation: string
  family: string
  resolution: { tier: Tier; rule: string; conditions: string[] }
  reachability: Reachability
  path_condition: { terms: Atom[][]; approximate: boolean }
  feasibility: { verdict: Feasibility; fragment: string; assumptions: string[] }
  attributes: Record<string, unknown>
  lane: string
  evidence: string[]
  callsite?: string
  line?: number
  shared?: boolean
  cycle_ref?: boolean
  segment?: string
  candidate?: boolean
  body_runs?: boolean
}
export type TraceSegment = { id: string; at: string; reason: string; alternatives: string[]; steps: string[] }
export type TraceLane = { id: string; order: string }
export type TraceGap = { kind?: string; step?: string; hop?: string; reasons?: string[]; callsite?: string; node?: string }
export type DbEffect = {
  step: string
  operation: string
  class: string
  tables: string[]
  state: string
  commit_position: { code: string; via: string; alternatives?: string[] } | null
  conditions: string[]
  claim: string
}
export type Absence = { hop: string; verdict: string; reasons: string[] }
export type FlowTrace = {
  contract: string
  direction: 'forward' | 'backward'
  status: 'complete' | 'truncated_depth' | 'truncated_limit' | 'deadline' | 'incomplete_evidence'
  hops: TraceHop[]
  steps: TraceStep[]
  segments: TraceSegment[]
  lanes: TraceLane[]
  gaps: TraceGap[]
  boundaries: TraceGap[]
  caveats: string[]
  path_tier: Tier
  effects: DbEffect[]
  absence: Absence[]
  levels: Record<string, string>
  run_id: string
  graph_digest: string | null
  query: Record<string, unknown>
  verification: string
  trace_digest: string
  freshness_reasons: string[]
}
export type PinnedTrace = {
  status: 'current' | 'stale' | 'integrity_failed'
  freshness_reasons: string[]
  failures: string[]
  trace_digest: string
  run_id: string
  query: Record<string, unknown>
}

const base = (projectId: string, snapshotId: string) => `/projects/${projectId}/snapshots/${snapshotId}/flow`

export const getFlowEvidence = (projectId: string, snapshotId: string, signal?: AbortSignal) =>
  request<FlowEvidenceStatus>(`${base(projectId, snapshotId)}/evidence`, { signal })
export const buildFlowEvidence = (projectId: string, snapshotId: string, signal?: AbortSignal) =>
  request<FlowEvidenceStatus>(`${base(projectId, snapshotId)}/evidence`, { method: 'POST', body: '{}', signal })
export const getFlow = (projectId: string, snapshotId: string, signal?: AbortSignal) =>
  request<FlowStatus>(base(projectId, snapshotId), { signal })
export const buildFlow = (projectId: string, snapshotId: string, signal?: AbortSignal) =>
  request<FlowStatus>(base(projectId, snapshotId), { method: 'POST', body: '{}', signal })

export function listEntries(projectId: string, snapshotId: string, options: { kind?: string; q?: string; mode?: string }, signal?: AbortSignal) {
  const query = new URLSearchParams({ limit: '200' })
  if (options.kind) query.set('kind', options.kind)
  if (options.q) { query.set('q', options.q); query.set('mode', options.mode ?? 'lexical') }
  return request<EntryPage>(`${base(projectId, snapshotId)}/entries?${query}`, { signal })
}

export function getTrace(projectId: string, snapshotId: string, entry: string, direction: 'forward' | 'backward', maxDepth: number, signal?: AbortSignal) {
  const query = new URLSearchParams({ entry, direction, max_depth: String(maxDepth) })
  return request<FlowTrace>(`${base(projectId, snapshotId)}/trace?${query}`, { signal })
}

export const pinTrace = (projectId: string, snapshotId: string, entry: string, direction: 'forward' | 'backward', maxDepth: number, signal?: AbortSignal) =>
  request<FlowTrace>(`${base(projectId, snapshotId)}/traces`, { method: 'POST', body: JSON.stringify({ entry, direction, max_depth: maxDepth }), signal })
export const getPinned = (projectId: string, snapshotId: string, digest: string, signal?: AbortSignal) =>
  request<PinnedTrace>(`${base(projectId, snapshotId)}/traces/${digest}`, { signal })

// ---- Milestone E: plan-gated explanations (never evidence; validated against the pinned trace).
export type ExplanationConfig = {
  enabled: boolean
  provider: string
  model: string
  sharing_class: string
  retention_note: string
  status: Record<string, unknown>
  contract: Record<string, unknown>
}
export type ExplanationPlan = {
  plan_id: string
  trace_digest: string
  package_digest: string
  request_digest: string
  request_bytes: number
  provider: string
  model: string
  retention_note: string
  plan_digest: string
  expires_at: string
  package: { steps: number; excerpts: number; truncated: boolean; caveats: string[] }
  enabled: boolean
  sent: number
}
export type ExplanationClaim = { ordinal: number; level: string; text: string; steps: string[]; citations: { id: string }[]; claim_digest: string }
export type ExplanationRun = {
  run_id: string
  plan_id: string
  trace_digest: string
  state: string
  outcome: string | null
  returned_model: string | null
  rejection_codes: string[]
  unknowns: string[]
  requests_sent: number
  claims: ExplanationClaim[]
  label: string
}

export const getExplanationConfig = (projectId: string, snapshotId: string, signal?: AbortSignal) =>
  request<ExplanationConfig>(`${base(projectId, snapshotId)}/explanations/config`, { signal })
export const planExplanation = (projectId: string, snapshotId: string, traceDigest: string, signal?: AbortSignal) =>
  request<ExplanationPlan>(`${base(projectId, snapshotId)}/explanations/plan`, { method: 'POST', body: JSON.stringify({ trace_digest: traceDigest }), signal })
export const runExplanation = (projectId: string, snapshotId: string, plan: ExplanationPlan, signal?: AbortSignal) =>
  request<ExplanationRun>(`${base(projectId, snapshotId)}/explanations`, { method: 'POST', body: JSON.stringify({ plan_id: plan.plan_id, plan_digest: plan.plan_digest, confirm: true }), signal })
export const listExplanations = (projectId: string, snapshotId: string, traceDigest: string, signal?: AbortSignal) =>
  request<{ items: ExplanationRun[]; total: number }>(`${base(projectId, snapshotId)}/explanations?trace_digest=${traceDigest}`, { signal })
