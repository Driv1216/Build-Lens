import { request } from './client'

export type Tier = 'observed' | 'derived' | 'inferred' | 'unknown'
export type ClassSet = 'S0' | 'S1' | 'S2' | 'S3' | 'S4'
export type EvidenceLink = { source: 'catalog' | 'architecture'; digest: string }

export type ArchitectureRun = {
  run_id: string
  snapshot_id: string
  revision: number
  started_at: string
  completed_at: string
  outcome: 'complete' | 'partial' | 'failed'
  catalog_run_id: string
  graph_digest: string | null
  architecture_digest: string | null
  portable_digest: string | null
  coverage: Record<string, unknown>
  diagnostics: string[]
  superseded: boolean
  quarantined: boolean
}
export type ArchitectureStatus = {
  latest_attempt: ArchitectureRun | null
  active_run: ArchitectureRun | null
  freshness: 'fresh' | 'stale' | 'none'
  freshness_reasons: string[]
  catalog_ready: boolean
}
export type ArchNode = {
  key: string
  kind: string
  parent_key: string | null
  name: string
  locator: string | null
  language: string | null
  catalog_entity_key: string | null
  coverage_state: string
  attributes: Record<string, unknown>
  metrics: Record<string, number | null>
  evidence: EvidenceLink[]
}
export type ArchEdge = {
  key: string
  kind: string
  source_key: string
  target_key: string | null
  target_text: string | null
  tier: Tier
  resolution_status: string | null
  import_class: string | null
  count: number
  attributes: Record<string, unknown>
  conditions: string[]
  evidence: EvidenceLink[]
}
export type ArchComponent = {
  key: string
  level: 'module' | 'container'
  class_set: ClassSet
  scope_key: string | null
  members: string[]
  witness: string[]
  attributes: Record<string, unknown>
  evidence: EvidenceLink[]
}
export type ArchDiagnostic = {
  ordinal: number
  code: string
  category: string
  tier: Tier
  subjects: string[]
  attributes: Record<string, unknown>
  evidence: EvidenceLink[]
}
type Scoped = { run_id: string; freshness_reasons: string[]; verification: string }
export type Overview = Scoped & {
  workspaces: ArchNode[]
  node_counts: Record<string, number>
  edge_counts: Record<string, Record<string, number>>
  component_counts: Record<string, number>
  diagnostic_counts: Record<string, number>
  coverage: Record<string, unknown>
}
export type Tree = Scoped & { parent: ArchNode | null; children: ArchNode[]; diagnostic_counts: Record<string, number> }
export type NodeDetail = Scoped & {
  node: ArchNode
  chain: ArchNode[]
  children: ArchNode[]
  outgoing: ArchEdge[]
  incoming: ArchEdge[]
  outgoing_total: number
  incoming_total: number
  components: ArchComponent[]
  diagnostics: ArchDiagnostic[]
}
export type Gaps = { complete: boolean; unknown_exits: ArchEdge[]; coverage_gaps: string[]; run_gaps: string[] }
export type Dependencies = Scoped & {
  origin: string
  origins: string[]
  direction: 'out' | 'in'
  class_set: ClassSet
  max_depth: number
  search: string
  frontier: number
  reached: { node: string; depth: number; path: string[] }[]
  nodes: Record<string, ArchNode>
  edges: Record<string, ArchEdge>
  gaps: Gaps
  by_container: Record<string, number>
}
export type PathResult = Scoped & { source: string; target: string; class_set: ClassSet; status: string; path: ArchEdge[]; gaps: Gaps }
export type Cycles = Scoped & { items: ArchComponent[]; edges: Record<string, ArchEdge>; total: number }
export type MatrixCell = { source: string; target: string; count: number; edges: number; classes: Record<string, number> }
export type Matrix = Scoped & { parent: ArchNode; class_set: ClassSet; children: ArchNode[]; cells: MatrixCell[]; cyclic_pairs: [string, string][] }
export type Neighbourhood = Scoped & { origin: string; class_set: ClassSet; radius: number; nodes: ArchNode[]; edges: ArchEdge[]; truncated: boolean; total_nodes: number }
export type DiagnosticPage = Scoped & { items: ArchDiagnostic[]; total: number; limit: number; offset: number; counts: Record<string, number> }
export type Evidence = {
  run_id: string
  digest: string
  kind: string
  source_refs: Record<string, unknown>[]
  derived: Record<string, unknown>
  rendered_text: string | null
  verification: string
  label: string
}

const base = (projectId: string, snapshotId: string) => `/projects/${projectId}/snapshots/${snapshotId}/architecture`
const query = (values: Record<string, string | number | undefined | null>) => {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  const text = params.toString()
  return text ? `?${text}` : ''
}

export const getArchitecture = (p: string, s: string, signal?: AbortSignal) => request<ArchitectureStatus>(base(p, s), { signal })
export const buildArchitecture = (p: string, s: string, signal?: AbortSignal) =>
  request<ArchitectureStatus>(base(p, s), { method: 'POST', body: '{}', signal })
export const getOverview = (p: string, s: string, signal?: AbortSignal) => request<Overview>(`${base(p, s)}/overview`, { signal })
export const getTree = (p: string, s: string, parent: string | null, signal?: AbortSignal) =>
  request<Tree>(`${base(p, s)}/tree${query({ parent })}`, { signal })
export const getNode = (p: string, s: string, key: string, signal?: AbortSignal) =>
  request<NodeDetail>(`${base(p, s)}/node${query({ key })}`, { signal })
export const getDependencies = (p: string, s: string, node: string, direction: 'out' | 'in', classSet: ClassSet, signal?: AbortSignal) =>
  request<Dependencies>(`${base(p, s)}/dependencies${query({ node, direction, class_set: classSet })}`, { signal })
export const getPath = (p: string, s: string, source: string, target: string, classSet: ClassSet, signal?: AbortSignal) =>
  request<PathResult>(`${base(p, s)}/path${query({ source, target, class_set: classSet })}`, { signal })
export const getCycles = (p: string, s: string, signal?: AbortSignal) => request<Cycles>(`${base(p, s)}/cycles${query({ limit: 500 })}`, { signal })
export const getMatrix = (p: string, s: string, parent: string, classSet: ClassSet, signal?: AbortSignal) =>
  request<Matrix>(`${base(p, s)}/matrix${query({ parent, class_set: classSet })}`, { signal })
export const getNeighbourhood = (p: string, s: string, node: string, classSet: ClassSet, radius: number, signal?: AbortSignal) =>
  request<Neighbourhood>(`${base(p, s)}/neighbourhood${query({ node, class_set: classSet, radius })}`, { signal })
export const getDiagnostics = (p: string, s: string, offset: number, signal?: AbortSignal) =>
  request<DiagnosticPage>(`${base(p, s)}/diagnostics${query({ limit: 200, offset })}`, { signal })
export const getEvidence = (p: string, s: string, digest: string, signal?: AbortSignal) =>
  request<Evidence>(`${base(p, s)}/evidence/${encodeURIComponent(digest)}`, { signal })
