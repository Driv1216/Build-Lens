import { ApiError } from '../../api/client'
import type { Atom, Feasibility, FlowTrace, TraceHop } from '../../api/flow'
import { visible } from '../explore/visible'

/** Neutral wording per relation: a structural step is never described as an execution, and
 * scheduling ("registers") is kept apart from running ("invokes"). */
export const RELATION_TEXT: Record<string, string> = {
  enters: 'enters',
  invokes: 'invokes',
  awaits: 'awaits',
  invokes_until_first_await: 'runs until its first await',
  creates_coroutine: 'creates a coroutine (body does not run)',
  creates_generator: 'creates a generator (body does not run)',
  creates_async_generator: 'creates an async generator (body does not run)',
  iterates: 'iterates the generator',
  resumes: 'resumes the generator',
  throws_into: 'throws into the generator',
  registers: 'registers (runs later)',
  continues_with: 'continues with',
  invokes_callback: 'hands over a callback',
  dispatch_candidate: 'override candidate',
  injects: 'injects dependency',
  issues_request: 'issues a request',
  calls_endpoint: 'may reach endpoint',
  handled_by: 'handled by',
  exits_with: 'may exit with',
  raises_to: 'raises to',
  response_boundary: 'response boundary',
  responds: 'response continues at',
  performs: 'database operation',
  on_table: 'on table',
  in_transaction: 'inside transaction',
  schedules_render: 'schedules a render (may bail out)',
  may_rerun_effect: 'may re-run effect',
  may_refetch: 'may refetch queries',
  navigates: 'navigates',
  crosses_boundary: 'crosses into',
  constructs: 'constructs',
  unresolved: 'unresolved call',
}

export const FEASIBILITY_TEXT: Record<Feasibility, string> = {
  refuted_in_fragment: 'refuted in fragment',
  consistent_in_fragment: 'consistent in fragment',
  undetermined: 'undetermined',
}

export const REACHABILITY_TEXT: Record<string, string> = {
  reachable_in_model: 'reachable in model',
  unreachable_static: 'statically unreachable',
  unreachable_after_non_returning_call: 'after a call that never returns',
  reachable_if_raises: 'only if something raises',
  scope_reached: 'scope reached',
}

export const STATUS_TEXT: Record<string, string> = {
  complete: 'complete within bounds',
  incomplete_evidence: 'incomplete evidence',
  truncated_depth: 'truncated at the depth bound',
  truncated_limit: 'truncated at the hop limit',
  deadline: 'stopped at the deadline',
}

export function hopsById(trace: FlowTrace): Map<string, TraceHop> {
  return new Map(trace.hops.map(hop => [hop.id, hop]))
}

export function relation(value: string): string {
  return RELATION_TEXT[value] ?? value.replaceAll('_', ' ')
}

export function shown(value: string | null | undefined): string {
  return visible(value ?? '')
}

export function atomText(atom: Atom): string {
  const name = atom.var ? visible(atom.var.name) : null
  const polarity = atom.value ? '' : 'not '
  switch (atom.op) {
    case 'nullish': return `${name} is ${polarity}null/None`
    case 'truthy': return `${name} is ${polarity}truthy`
    case 'eq': return `${name} ${atom.value ? '==' : '!='} ${literal(atom.lit)}`
    case 'typeof': return `typeof ${name} ${atom.value ? '===' : '!=='} "${visible(atom.typeof ?? '')}"`
    case 'const': return `constant is ${polarity}truthy`
    default: return `opaque condition (${atom.reason ?? 'unknown'}) is ${atom.value ? 'true' : 'false'}`
  }
}

function literal(value: unknown[] | undefined): string {
  if (!value || value.length === 0) return '?'
  const [type, ...rest] = value
  if (type === 'none' || type === 'undefined') return String(type)
  if (type === 'str' || type === 'bytes') return `${type} literal (length ${String(rest[1] ?? '?')})`
  return `${String(type)} ${String(rest[0])}`
}

/** Keep the flow error code's explanation through ``useRequest`` (which keeps a message). */
export function explained<T>(load: Promise<T>): Promise<T> {
  return load.catch((reason: unknown) => {
    if (reason instanceof DOMException && reason.name === 'AbortError') throw reason
    throw new Error(describeFlow(reason))
  })
}

export function describeFlow(reason: unknown): string {
  if (reason instanceof ApiError) {
    switch (reason.code) {
      case 'FLOW_REQUIRED': return 'No flow model yet: build the flow evidence and the flow model from the snapshot panel.'
      case 'FLOW_STALE': return 'The flow model is stale because its inputs changed: rebuild the flow evidence and the flow model.'
      case 'FLOW_INTEGRITY_FAILED': return 'Stored flow rows failed verification, so no partial trace is shown. Run the flow integrity check for details.'
      case 'FLOW_TRACE_TOO_LARGE': return 'This trace needs more verification than one request allows: lower the depth bound.'
      case 'FLOW_NOT_FOUND': return 'That flow node is not in the active run.'
      case 'FLOW_TRACE_NOT_FOUND': return 'That pinned trace is not stored for this snapshot.'
      case 'FLOW_TRACE_NOT_PINNABLE': return 'A trace that stopped at its deadline is not reproducible, so it cannot be pinned.'
      case 'FLOW_ENTRY_SEARCH_UNAVAILABLE': return 'Entry-point search needs retrieval to be configured.'
      case 'FLOW_EVIDENCE_REQUIRED': return 'Run the flow evidence extraction first.'
      case 'FLOW_EVIDENCE_STALE': return 'The flow evidence is stale: extract it again.'
      case 'FLOW_INPUTS_INCONSISTENT': return 'Flow evidence, catalog and architecture must be current over one structural analysis.'
    }
  }
  if (reason instanceof DOMException && reason.name === 'AbortError') return ''
  return reason instanceof Error ? reason.message : String(reason)
}
