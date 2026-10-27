import { request } from './client'

export type CatalogRun = {
  run_id: string
  snapshot_id: string
  revision: number
  started_at: string
  completed_at: string
  outcome: 'complete' | 'partial' | 'failed'
  structural_analysis_id: string
  processing_run_id: string
  catalog_digest: string | null
  portable_digest: string | null
  coverage: Record<string, unknown>
  diagnostics: string[]
  superseded: boolean
  quarantined: boolean
}
export type CatalogStatus = {
  latest_attempt: CatalogRun | null
  active_run: CatalogRun | null
  freshness: 'fresh' | 'stale' | 'none'
  freshness_reasons: string[]
}
export type EntityKind = 'file' | 'symbol' | 'component' | 'hook' | 'api_endpoint' | 'db_table'
export type Entity = {
  entity_key: string
  kind: EntityKind
  language: string | null
  locator: string
  name: string
  qualified_name: string | null
  symbol_kind: string | null
  start_line: number | null
  end_line: number | null
  subject_entity_key: string | null
  stored_coverage: 'complete' | 'incomplete' | 'unavailable' | 'coordinate_mismatch'
  coverage_reason: string | null
  context_coverage: 'full' | 'outline' | 'not_eligible'
  context_reason: string | null
  package_digest: string | null
  package_bytes: number
  limitations: string[]
  interpretation_state?: 'current' | 'stale' | 'absent' | null
}
export type EntityPage = { run_id: string; freshness_reasons: string[]; total: number; limit: number; offset: number; items: Entity[] }
export type SourceRef = { table: string; id: string; contract: string | null; locator: string }
export type Fact = {
  fact_digest: string
  fact_kind: string
  subject_entity_key: string
  basis: 'parsed' | 'pattern_with_local_evidence' | 'pattern_only'
  rendered_text: string
  renderer_version: string
  source_refs: SourceRef[]
  derived: { met?: string[]; unmet?: string[]; recognizer?: string; [key: string]: unknown }
  verification: string | null
}
export type Relation = {
  kind: string
  source_entity_key: string
  target_entity_key: string | null
  target_text: string | null
  resolution_status: string | null
  fact_digest: string
}
export type EvidenceChunk = {
  chunk_id: string
  stable_key: string
  content_hash: string
  start_offset: number
  end_offset: number
  start_line: number
  end_line: number
  symbol_identity_id: string | null
  content: string | null
  verified: boolean | null
}
export type EntityDetail = {
  run_id: string
  freshness_reasons: string[]
  entity: Entity
  gaps: { count?: number; characters?: number }
  package_chunks: string[]
  omitted: { entity_key: string; start_line: number | null; end_line: number | null }[]
  evidence: EvidenceChunk[]
  facts: Fact[]
  neighbor_facts: Fact[]
  outgoing: Relation[]
  incoming: Relation[]
  integrity_errors: string[]
  related: Record<string, string>
}
export type EntityFilters = {
  kind?: string
  language?: string
  stored_coverage?: string
  context_coverage?: string
  locator_prefix?: string
  q?: string
}

const base = (projectId: string, snapshotId: string) => `/projects/${projectId}/snapshots/${snapshotId}/semantic`

export const getCatalog = (projectId: string, snapshotId: string, signal?: AbortSignal) =>
  request<CatalogStatus>(`${base(projectId, snapshotId)}/catalog`, { signal })
export const buildCatalog = (projectId: string, snapshotId: string, signal?: AbortSignal) =>
  request<CatalogStatus>(`${base(projectId, snapshotId)}/catalog`, { method: 'POST', body: '{}', signal })

export function listEntities(projectId: string, snapshotId: string, filters: EntityFilters, offset: number, signal?: AbortSignal) {
  const query = new URLSearchParams({ limit: '200', offset: String(offset) })
  for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value)
  return request<EntityPage>(`${base(projectId, snapshotId)}/entities?${query}`, { signal })
}
export const getEntity = (projectId: string, snapshotId: string, key: string, signal?: AbortSignal) =>
  request<EntityDetail>(`${base(projectId, snapshotId)}/entities/${encodeURIComponent(key)}`, { signal })

// ---- Stage 13 Milestone C: plan-authorised generation (no request is sent without a plan).
export type GenerationConfig = {
  enabled: boolean
  provider: 'local' | 'gemini' | 'openai'
  model: string
  sharing_class: 'cloud' | 'loopback'
  generator_profile_digest: string
  retention_note: string
  status: Record<string, unknown>
  caps: Record<string, unknown>
}
export type PlanLocator = { artifact_identity_id: string | null; locator: string; role: string; bytes: number }
export type PlanItem = {
  ordinal: number
  entity_key: string
  disposition: 'generate' | 'reuse' | 'ineligible'
  reason: string | null
  context_coverage: string
  request_digest: string | null
  request_bytes: number
  contributing_locators: PlanLocator[]
  reuse_interpretation_id: string | null
  reuse_profile_digest: string | null
  reuse_provider: string | null
  reuse_returned_model: string | null
}
export type GenerationPlan = {
  plan_id: string
  plan_digest: string
  issued_at: string
  expires_at: string
  mode: 'generate' | 'reuse_only'
  provider_config: Record<string, unknown>
  sharing_class: 'cloud' | 'loopback'
  retention_note: string
  caps: Record<string, unknown>
  totals: Record<string, unknown>
  contributing_locators: { locator: string; role: string; bytes: number }[]
  items: PlanItem[]
  run_id: string | null
}
export type GenerationAttempt = {
  attempt_id: string
  entity_key: string
  attempt_no: number
  outcome: string | null
  returned_model: string | null
  rejection_codes: string[]
  late_receipts: number
}
export type GenerationRun = {
  run_id: string
  state: 'executing' | 'complete' | 'partial' | 'failed' | 'superseded' | 'abandoned'
  attempts_started: number
  totals: Record<string, unknown>
  diagnostics: string[]
  attempts: GenerationAttempt[]
}
export type Citation = {
  evidence_id: string
  kind: 'chunk' | 'fact'
  basis: string
  locator: string | null
  start_line: number | null
  end_line: number | null
  verification: string | null
  quote: string | null
}
export type Claim = {
  ordinal: number
  category: string
  text: string | null
  status: 'cited' | 'conflicts_with_fact'
  basis_label: 'cited' | 'per_documentation' | 'from_declarations_only'
  assertion: { kind?: string; target?: string | null } | null
  assertion_outcome: string | null
  scope_warning: boolean
  citations: Citation[]
}
export type InterpretationView = {
  interpretation_id: string
  provider: string
  requested_model: string
  returned_model: string | null
  generator_profile_digest: string
  status: string
  created_at: string
  context_coverage: string | null
  unknowns: string[]
  claims: Claim[]
  integrity_errors: string[]
}
export type Entry = {
  entity_key: string
  state: 'current' | 'stale' | 'absent'
  stale_reasons: string[]
  mode: 'generated' | 'reused' | 'retained' | null
  reused_from_profile: string | null
  interpretation: InterpretationView | null
}

export const getGenerationConfig = (signal?: AbortSignal) =>
  request<GenerationConfig>('/semantic-generation', { signal })
export const getEntry = (projectId: string, snapshotId: string, key: string, signal?: AbortSignal) =>
  request<Entry>(`${base(projectId, snapshotId)}/entities/${encodeURIComponent(key)}/interpretation`, { signal })
export const getEntryHistory = (projectId: string, snapshotId: string, key: string, signal?: AbortSignal) =>
  request<{ entity_key: string; items: Entry[] }>(`${base(projectId, snapshotId)}/entities/${encodeURIComponent(key)}/history`, { signal })
export const createPlan = (projectId: string, snapshotId: string, entityKeys: string[], signal?: AbortSignal) =>
  request<GenerationPlan>(`${base(projectId, snapshotId)}/generation/plan`, {
    method: 'POST',
    body: JSON.stringify({ entity_keys: entityKeys }),
    signal,
  })
export const executePlan = (projectId: string, snapshotId: string, plan: GenerationPlan, signal?: AbortSignal) =>
  request<GenerationRun>(`${base(projectId, snapshotId)}/generation`, {
    method: 'POST',
    body: JSON.stringify({ plan_id: plan.plan_id, plan_digest: plan.plan_digest, confirm: true }),
    signal,
  })
