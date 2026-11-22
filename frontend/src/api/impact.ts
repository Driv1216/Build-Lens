import { request } from './client'

export type ImpactSelection = { snapshot_id: string; flow_run_id?: string }
export type ImpactOperation = {
  kind: 'require_parameter' | 'remove_parameter' | 'remove_return_field' | 'set_return_boolean' | 'set_integer_threshold'
    | 'rename_symbol' | 'remove_symbol' | 'remove_api_field' | 'require_api_field' | 'rename_api_field'
    | 'set_endpoint_path' | 'set_endpoint_method' | 'remove_database_column' | 'set_database_constraint' | 'set_configuration_default'
  target: string
  member?: string
  value?: boolean | number | string | null
  new_name?: string
  family?: 'query' | 'body' | 'response'
  constraint?: 'nullable' | 'unique' | 'primary_key'
}
export type ImpactInput =
  | { mode: 'entity'; before: ImpactSelection; entity: string }
  | { mode: 'snapshots'; before: ImpactSelection; after: ImpactSelection; locators: string[]; architecture_rule_version?: number }
  | { mode: 'hypothesis'; before: ImpactSelection; locators: string[]; operations: ImpactOperation[]; confirm: true }
  | { mode: 'patch'; before: ImpactSelection; patch: string }
export type ImpactReport = {
  contract: string
  status: string
  result_digest: string
  architecture_rules?: { version: number; digest: string; old_graph: string; new_graph: string }
  gaps: string[]
  changes: { id: string; entity?: string; field?: string; locator: string; property: string; comparison: string; before: unknown; after: unknown }[]
  findings: { origin: string; entity: string; label?: string; locator?: string; mechanism: string; consequence: string; distance: string; witnesses?: unknown[]; witness?: unknown; relationship_tier: string; side?: string; priority?: { level: string; reasons: string[] } }[]
  tests: { origin: string; test: string; locator: string; property_assertions: unknown[]; matching_input?: number; condition_specific_execution: string }[]
  verification_gaps: { origin: string; property: string; locator: string; reason: string }[]
  frontier: unknown[]
}
export type Interpretation = {
  candidate: ImpactOperation | null
  targets: { key: string; locator: string; line: number | null }[]
  unknowns: string[]
}
export type Coverage = {
  sides: Record<string, { inventory: string; declarations: string; relationship_exposure: string; supplemental_properties: string; properties: Record<string, string> }>
  supplemental_comparison: string | null
  property_comparison: Record<string, string>
}
export type Preparation = {
  status: string
  completed: { locator: string; id: string; reused: boolean }[]
  gaps: { locator: string; reason: string }[]
  seal: { id: string; reused: boolean } | null
}
export type ImpactVerification = {
  status: string
  reasons: string[]
  integrity: string
  freshness: string
  compatibility: string
  evidence_verification: string
  analysis_completeness: string
}

const base = (project: string) => `/projects/${encodeURIComponent(project)}/impact`
const post = <T>(url: string, data: unknown, signal: AbortSignal) =>
  request<T>(url, { method: 'POST', body: JSON.stringify(data), signal })

export const analyzeImpact = (project: string, input: ImpactInput, save: boolean, signal: AbortSignal) =>
  post<ImpactReport>(`${base(project)}/analyses`, { ...input, save }, signal)
export const prepareImpact = (project: string, selection: ImpactSelection, locators: string[], sealFlow: boolean, signal: AbortSignal) =>
  post<Preparation>(`${base(project)}/preparations`, { selection, locators, seal_flow: sealFlow }, signal)
export const interpretImpact = (project: string, selection: ImpactSelection, proposal: string, signal: AbortSignal) =>
  post<Interpretation>(`${base(project)}/proposals/interpret`, { selection, text: proposal }, signal)
export const loadImpact = (project: string, id: string, signal: AbortSignal) =>
  request<ImpactReport>(`${base(project)}/analyses/${encodeURIComponent(id)}`, { signal })
export const verifyImpact = (project: string, id: string, signal: AbortSignal) =>
  request<ImpactVerification>(`${base(project)}/analyses/${encodeURIComponent(id)}?verify=true`, { signal })

export function impactCoverage(project: string, selection: ImpactSelection, locator: string | undefined, after: string, signal: AbortSignal): Promise<Coverage> {
  const query = new URLSearchParams()
  if (locator) query.set('locator', locator)
  if (after) query.set('after_snapshot_id', after)
  if (selection.flow_run_id) query.set('flow_run_id', selection.flow_run_id)
  return request<Coverage>(`${base(project)}/preparations/${encodeURIComponent(selection.snapshot_id)}?${query}`, { signal })
}
