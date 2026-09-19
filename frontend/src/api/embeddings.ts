import { request } from './client'

export type EmbeddingContract = {
  digest: string
  provider: string
  provider_version: string
  model_id: string
  model_revision: string
  dimensions: number
  backend: string
  device: string
  precision: string
  document_format: string
  query_format: string
  token_limit: number
  window_policy: string
  pooling: string
  normalization: string
}
export type EmbeddingRun = {
  run_id: string
  project_id: string
  snapshot_id: string
  processing_run_id: string
  processing_revision: number
  revision: number
  contract: EmbeddingContract
  membership_digest: string
  coverage: Record<string, number>
  limits: Record<string, unknown>
  diagnostics: string[]
  chunk_count: number
  started_at: string
  completed_at: string
  outcome: 'complete' | 'failed'
  superseded: boolean
  quarantined: boolean
}
export type EmbeddingStatus = {
  model: { available: boolean; path: string; reason: string | null }
  contract: EmbeddingContract
  prepared: boolean
  search_available: boolean
  active_processing_run_id: string | null
  freshness_reasons: string[]
  latest_attempt: EmbeddingRun | null
  active_run: EmbeddingRun | null
}
export type SemanticSearchFilters = {
  locator_prefix?: string
  language?: string
  kind?: string
}
export type SemanticSearchHit = {
  score: number
  embedding_id: string
  embedding_run_id: string
  logical_embedding_key: string
  input_hash: string
  contract_digest: string
  window_count: number
  execution_mode: 'generated' | 'reused'
  reuse_source_embedding_id: string | null
  project_id: string
  snapshot_id: string
  processing_run_id: string
  processing_revision: number
  artifact_processing_id: string
  artifact_id: string
  locator: string
  chunk_id: string
  stable_key: string
  content_hash: string
  kind: string
  ordinal: number
  span: { start_line: number; end_line: number; start_column: number; end_column: number }
  symbol_id: string | null
  symbol_identity_id: string | null
  heading_path: string[]
  json_path: string | null
  content: string
}
export type SemanticSearchResponse = {
  contract: EmbeddingContract
  embedding_run: EmbeddingRun
  hits: SemanticSearchHit[]
}
export type RetrievalMode = 'exact' | 'lexical' | 'semantic' | 'hybrid'
export type RetrievalComponent = {
  state: 'not_requested' | 'completed'
  candidate_count: number
  candidate_count_is_bounded: boolean
  truncated: boolean
}
export type RetrievalHit = {
  rank: number
  score_kind: 'exact' | 'lexical' | 'cosine' | 'rrf'
  score: number | null
  exact_rank: number | null
  exact_matches: { field: 'symbol_name' | 'symbol_qualified_name' | 'locator'; value: string }[]
  lexical: { rank: number; score: number; field_scores: Record<string, number> } | null
  semantic: ({ rank: number; cosine_similarity: number } & Pick<SemanticSearchHit,
    'embedding_id' | 'embedding_run_id' | 'logical_embedding_key' | 'input_hash' |
    'contract_digest' | 'window_count' | 'execution_mode' | 'reuse_source_embedding_id'>) | null
  hybrid: { lexical_contribution: number; semantic_contribution: number } | null
  artifact_processing_id: string
  artifact_id: string
  locator: string
  chunk_id: string
  stable_key: string
  content_hash: string
  kind: string
  ordinal: number
  span: SemanticSearchHit['span']
  symbol_id: string | null
  symbol_identity_id: string | null
  heading_path: string[]
  json_path: string | null
  content: string
}
export type RetrievalResponse = {
  requested_mode: RetrievalMode
  executed_mode: RetrievalMode
  policy: { version: string; candidate_depth: number; rrf_constant: number; exact_precedence: boolean }
  project_id: string
  snapshot_id: string
  processing_run_id: string
  processing_revision: number
  processing_coverage: Record<string, number>
  embedding_run: EmbeddingRun | null
  embedding_contract: EmbeddingContract | null
  components: { exact: RetrievalComponent; lexical: RetrievalComponent; semantic: RetrievalComponent }
  candidate_depth: number
  candidates_truncated: boolean
  returned_count: number
  hits: RetrievalHit[]
}
export type EmbeddingHistory = { items: EmbeddingRun[]; total: number; limit: number; offset: number }

const base = (projectId: string, snapshotId: string) =>
  `/projects/${projectId}/snapshots/${snapshotId}`

export const getEmbeddings = (projectId: string, snapshotId: string, signal?: AbortSignal) =>
  request<EmbeddingStatus>(`${base(projectId, snapshotId)}/embeddings`, { signal })

export const prepareEmbeddings = (projectId: string, snapshotId: string, signal?: AbortSignal) =>
  request<EmbeddingStatus>(`${base(projectId, snapshotId)}/embeddings`, {
    method: 'POST',
    body: '{}',
    signal,
  })

export const getEmbeddingHistory = (
  projectId: string,
  snapshotId: string,
  signal?: AbortSignal,
) => request<EmbeddingHistory>(`${base(projectId, snapshotId)}/embeddings/runs?limit=20&offset=0`, { signal })

export const semanticSearch = (
  projectId: string,
  snapshotId: string,
  query: string,
  topK: number,
  filters: SemanticSearchFilters,
  signal?: AbortSignal,
) =>
  request<SemanticSearchResponse>(`${base(projectId, snapshotId)}/semantic-search`, {
    method: 'POST',
    body: JSON.stringify({ query, top_k: topK, filters }),
    signal,
  })

export const retrievalSearch = (
  projectId: string,
  snapshotId: string,
  mode: RetrievalMode,
  query: string,
  topK: number,
  filters: SemanticSearchFilters,
  signal?: AbortSignal,
) =>
  request<RetrievalResponse>(`${base(projectId, snapshotId)}/search`, {
    method: 'POST',
    body: JSON.stringify({ mode, query, top_k: topK, filters }),
    signal,
  })
