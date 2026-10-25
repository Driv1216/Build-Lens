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
