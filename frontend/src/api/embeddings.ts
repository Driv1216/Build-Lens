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
export type RankingPolicy = 'local_cross_encoder' | 'retrieval_order'
export type RerankerStatus = {
  available: boolean
  path: string
  reason: string | null
  runtime_verified: boolean
  acquisition_command: string
  contract: {
    provider: string
    provider_version: string
    model_id: string
    model_revision: string
    tokenizer_id: string
    tokenizer_revision: string
    backend: string
    device: string
    precision: string
    input_format: string
    sequence_limit: number
    query_token_limit: number
    window_policy: string
    aggregation: string
    max_windows_per_candidate: number
    batch_size: number
    transformers_version: string
    torch_version: string
  }
}
export type ContextDecision = {
  chunk_id: string
  stable_key: string
  locator: string
  stage: string
  disposition: 'included' | 'excluded' | 'observed'
  reason: string
  related_chunk_ids: string[]
  retrieval_rank: number | null
  rerank_rank: number | null
  rerank_score: number | null
}
export type ContextEvidence = Omit<RetrievalHit, 'rank' | 'score_kind' | 'score' | 'exact_rank' | 'exact_matches' | 'lexical' | 'semantic' | 'hybrid'> & {
  unit_anchor: string
  part_index: number
  retrieval: RetrievalHit | null
  retrieved_directly: boolean
  selected_as_seed: boolean
  expanded_from: string[]
  rerank_score: number | null
  rerank_rank: number | null
  rerank_window_count: number | null
  rerank_winning_window: number | null
  context_cost: number
}
export type ContextResponse = {
  project_id: string
  snapshot_id: string
  query: string
  ranking_policy: RankingPolicy
  policy: {
    version: string
    candidate_depth: number
    rerank_depth: number
    seed_count: number
    neighbours_per_seed: number
    expanded_observation_limit: number
    item_limit: number
    byte_limit: number
    max_windows_per_candidate: number
    max_scoring_windows: number
    inference_batch_size: number
    request_deadline_seconds: number
    rerank_deadline_seconds: number
    hydrated_source_byte_limit: number
    response_byte_limit: number
  }
  processing_run_id: string
  processing_revision: number
  processing_contract: Record<string, unknown>
  embedding_run_id: string | null
  retrieval: {
    requested_mode: RetrievalMode
    executed_mode: RetrievalMode
    candidate_count: number
    candidate_depth: number
    candidates_truncated: boolean
    exact_count: number
    lexical_count: number
    semantic_count: number
  }
  reranker_state: 'completed' | 'not_requested' | 'not_needed'
  reranker_contract: RerankerStatus['contract'] | null
  item_count: number
  items: ContextEvidence[]
  decisions: ContextDecision[]
  budget: {
    measurement: {
      unit: 'utf8_bytes'
      policy: string
      exact: boolean
      tokenizer_id: string | null
      tokenizer_revision: string | null
    }
    limit: number
    used: number
    framing_cost: number
    remaining: number
    per_item_costs: Record<string, number>
    generation_token_count: null
  }
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

export const getReranker = (signal?: AbortSignal) =>
  request<RerankerStatus>('/reranker', { signal })

export const buildContext = (
  projectId: string,
  snapshotId: string,
  retrievalMode: RetrievalMode,
  rankingPolicy: RankingPolicy,
  query: string,
  filters: SemanticSearchFilters,
  signal?: AbortSignal,
) =>
  request<ContextResponse>(`${base(projectId, snapshotId)}/context`, {
    method: 'POST',
    body: JSON.stringify({
      query,
      retrieval_mode: retrievalMode,
      ranking_policy: rankingPolicy,
      filters,
    }),
    signal,
  })
