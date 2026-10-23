import { request } from './client'

export type ParamSpec = {
  kind: 'int' | 'float' | 'bool' | 'choice' | 'int_list' | 'float_list'
  default: unknown
  minimum: number | null
  maximum: number | null
  choices: string[]
  help: string
}

export type PipelineCatalogEntry = {
  id: string
  family: 'native' | 'langchain' | 'llamaindex' | 'langchain_postgres'
  label: string
  outcome: string
  corpus_modes: string[]
  native_chunk_identity: boolean
  available: boolean
  reasons: string[]
  params: Record<string, ParamSpec>
}

export type LabStatus = {
  contract: string
  python: string
  extras: Record<string, Record<string, string | null>>
  extras_installed: Record<string, boolean>
  isolated_environments: Record<string, boolean>
  lab_database_configured: boolean
  generation: { enabled: boolean; state: string }
  models: Record<string, { available: boolean; contract?: string } | null>
  pipelines: PipelineCatalogEntry[]
}

export type ExperimentConfig = {
  name: string
  pipeline: string
  corpus?: { mode?: string; include_unsupported?: boolean; filters?: Record<string, string | null> }
  embedding?: { source?: string }
  params?: Record<string, unknown>
  digest?: string
  contract?: string
}

export type LabSpan = { start_offset: number; end_offset: number } | null

export type LabItem = {
  ref: string
  rank: number
  text_sha256: string
  text_bytes: number
  locator: string | null
  artifact_fingerprint: string | null
  span: LabSpan
  anchored: boolean
  chunk_id: string | null
  score: number | null
  score_kind: string | null
  detail: Record<string, unknown>
}

export type LabStep = {
  seq: number
  kind: string
  name: string
  framework_event: string
  duration_ms: number
  attributes: Record<string, unknown>
  item_refs: string[]
  parent: number | null
}

export type LabResult = {
  pipeline: string
  config_digest: string
  index_id?: string | null
  question: string
  retrieved: LabItem[]
  ranked: LabItem[]
  context: LabItem[]
  packing: Record<string, unknown>
  steps: LabStep[]
  answer: Record<string, unknown> | null
  info: Record<string, unknown>
  embedding?: Record<string, unknown> | null
  bridge?: Record<string, unknown> | null
  timings: Record<string, number>
  texts: Record<string, string>
  verification: Record<string, unknown>
}

export type ProbeResponse = {
  question: string
  results: ({ ok: true; result: LabResult } | { ok: false; pipeline: string; error: { code: string; message: string } })[]
  overlap: Record<string, Record<string, number>>
}

export type IndexSummary = { index_id: string; config: ExperimentConfig; built_at: string; nodes: number | null; embedding: Record<string, unknown> | null; reuse_verification: Record<string, unknown> | null; corpus_digest: string }
export type Boundaries = { index_id: string; pipeline: string; locator: string; framework_spans: [number | null, number | null, string, string][]; native_spans: [number, number, string, string][]; text: string | null }
export type RunListing = { run_id: string; files: string[] }
export type MetricCell = { mean: number; n: number }
export type Scores = {
  version: string
  run_id: string
  corpus_digest: string
  dataset: { id: string; version: string; review_status: string }
  tiers: Record<string, string>
  summary: { means: Record<string, Record<string, MetricCell>>; latency: Record<string, { n: number; p50_ms: number | null; p95_ms: number | null; mean_ms: number | null }> }
  comparisons: { metric: string; baseline: string; candidate: string; n: number; mean_delta: number; wins: number; losses: number; ties: number; bootstrap_95: [number, number] }[]
  builds: Record<string, Record<string, unknown>>
  counts: Record<string, number>
  status: string
}

const scope = (projectId: string, snapshotId: string) => `/projects/${projectId}/snapshots/${snapshotId}/lab`

export const getLabStatus = (signal?: AbortSignal) => request<LabStatus>('/lab/status', { signal })
export const listExperiments = (signal?: AbortSignal) => request<{ experiments: (ExperimentConfig & { valid: boolean; digest: string })[] }>('/lab/experiments', { signal })
export const validateConfig = (config: ExperimentConfig, signal?: AbortSignal) =>
  request<ExperimentConfig & { digest: string; ingestion_identity: Record<string, unknown> }>('/lab/validate', { method: 'POST', body: JSON.stringify({ config }), signal })
export const saveExperiment = (config: ExperimentConfig) =>
  request<ExperimentConfig & { digest: string }>('/lab/experiments', { method: 'POST', body: JSON.stringify({ config }) })
export const buildIndex = (projectId: string, snapshotId: string, config: ExperimentConfig, signal?: AbortSignal) =>
  request<Record<string, unknown>>(`${scope(projectId, snapshotId)}/indexes`, { method: 'POST', body: JSON.stringify({ config }), signal })
export const probe = (projectId: string, snapshotId: string, question: string, configs: ExperimentConfig[], signal?: AbortSignal) =>
  request<ProbeResponse>(`${scope(projectId, snapshotId)}/probe`, { method: 'POST', body: JSON.stringify({ question, configs }), signal })
export const listIndexes = (signal?: AbortSignal) => request<{ indexes: IndexSummary[] }>('/lab/indexes', { signal })
export const getIndex = (indexId: string, signal?: AbortSignal) => request<Record<string, unknown>>(`/lab/indexes/${indexId}`, { signal })
export const getBoundaries = (indexId: string, locator: string, signal?: AbortSignal) =>
  request<Boundaries>(`/lab/indexes/${indexId}/boundaries?locator=${encodeURIComponent(locator)}`, { signal })
export const listRuns = (signal?: AbortSignal) => request<{ runs: RunListing[] }>('/lab/runs', { signal })
export const getRunFile = <T,>(runId: string, name: string, signal?: AbortSignal) => request<T>(`/lab/runs/${runId}/files/${name}`, { signal })
export const listRunCases = (runId: string, signal?: AbortSignal) => request<{ cases: string[] }>(`/lab/runs/${runId}/cases`, { signal })
export const getRunCase = (runId: string, name: string, signal?: AbortSignal) => request<Record<string, unknown>>(`/lab/runs/${runId}/cases/${name}`, { signal })
export const runLoaderDiff = (seed: number) => request<Record<string, unknown>>('/lab/loader-diff', { method: 'POST', body: JSON.stringify({ seed }) })
