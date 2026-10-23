import { request } from './client'
import type { ContextDecision, ContextEvidence, ContextResponse, RankingPolicy, RetrievalMode, SemanticSearchFilters } from './embeddings'

export type GenerationStatus = {
  enabled: boolean
  dependency_installed: boolean
  key_present: boolean
  configured: boolean
  provider: string
  model_id: string
  policy_version: string
  prompt_version: string
  guidance: string
}
export type AskEvidence = {
  evidence_id: string
  project_id: string
  snapshot_id: string
  processing_run_id: string
  processing_revision: number
  embedding_run_id: string | null
  coordinate_convention: string
  item: ContextEvidence
}
export type AskResponse = {
  request_id: string
  status: 'answered' | 'insufficient_evidence'
  reason: 'no_usable_evidence' | 'evidence_does_not_support_answer' | null
  answer: { text: string; statements: { text: string; support: 'direct' | 'inference'; evidence_ids: string[] }[] }
  citations: AskEvidence[]
  evidence: AskEvidence[]
  context: ContextResponse
  generation_decisions: ContextDecision[]
  generation_policy: Record<string, unknown>
  model_contract: Record<string, unknown>
  returned_model: string | null
  generation_state: 'completed' | 'skipped_no_evidence'
  original_context_digest: string
  evidence_digest: string
  prompt_version: string
  output_version: string
  packing_version: string
  fixed_tokens: number | null
  question_tokens: number | null
  prompt_measurement: { input_tokens: number; prepared_request_digest: string } | null
  usage_input_tokens: number | null
  usage_output_tokens: number | null
  context_seconds: number
  counting_seconds: number
  generation_seconds: number
  total_seconds: number
}

export const getGeneration = (signal?: AbortSignal) =>
  request<GenerationStatus>('/generation', { signal })

export const askBuildLens = (
  projectId: string, snapshotId: string, question: string,
  retrievalMode: RetrievalMode, rankingPolicy: RankingPolicy,
  filters: SemanticSearchFilters, signal?: AbortSignal,
) => request<AskResponse>(`/projects/${projectId}/snapshots/${snapshotId}/ask`, {
  method: 'POST', body: JSON.stringify({ question, retrieval_mode: retrievalMode, ranking_policy: rankingPolicy, filters }), signal,
})
