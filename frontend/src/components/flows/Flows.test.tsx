import { act, fireEvent, render, screen } from '@testing-library/react'
import { Profiler } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request } from '../../api/client'
import { FlowsPage } from './FlowsPage'

vi.mock('../../api/client', async (original) => ({ ...(await original<typeof import('../../api/client')>()), request: vi.fn() }))
const mocked = vi.mocked(request)

const CAVEAT = 'HTTP_LINK_INDEPENDENT_EVALUATION_PENDING'
const run = { run_id: 'r1', revision: 1, outcome: 'complete', graph_digest: 'g', coverage: {}, failure_code: null, superseded: false, contract: {} }
const fresh = { latest_attempt: run, active_run: run, freshness: 'fresh', freshness_reasons: [], inputs_ready: true }
const hop = (id: string, label: string, extra: Record<string, unknown> = {}) => ({
  id, node: `n:${id}`, kind: 'scope', label, locator: 'app/api.py', context: null, depth: 0, lane: 'main', layer: 'Service', identity: null, ...extra,
})
const verdicts = (extra: Record<string, unknown> = {}) => ({
  resolution: { tier: 'derived', rule: 'py-call-resolution.v1', conditions: [] },
  reachability: { verdict: 'reachable_in_model' },
  path_condition: { terms: [], approximate: false },
  feasibility: { verdict: 'consistent_in_fragment', fragment: 'flow-feasibility.v1', assumptions: ['ADMISSIBLE_LOCALS_ONLY'] },
  attributes: {}, lane: 'main', evidence: ['e'.repeat(64)], ...extra,
})
const step = (id: string, from: string, to: string | null, relation: string, extra: Record<string, unknown> = {}) => ({
  id, from, to, edge: `${relation}|${from}|${to}`, relation, family: 'invocation', ...verdicts(extra),
})

function trace(marker: string) {
  return {
    contract: 'flow-trace.v1', direction: 'forward', status: 'incomplete_evidence',
    hops: [
      hop('H0', '<button onClick>', { kind: 'entry_point', lane: 'dom_event:onClick', layer: 'Entry' }),
      hop('H1', 'save', { depth: 1, lane: 'dom_event:onClick', layer: 'Frontend', identity: 'id-1' }),
      hop('H2', 'POST', { kind: 'http_request', depth: 2, layer: 'HTTP' }),
      hop('H3', 'create_item endpoint', { kind: 'api_endpoint', depth: 3, layer: 'HTTP' }),
      hop('H4', marker, { depth: 4, layer: 'Route', identity: 'id-2' }),
      hop('H5', '201 (return)', { kind: 'response_outcome', depth: 5, layer: 'Route' }),
      hop('H6', '400 (raise)', { kind: 'response_outcome', depth: 5, layer: 'Route' }),
      hop('H7', 'notify', { depth: 6, lane: 'fastapi_background' }),
      hop('H8', 'evil‮name', { depth: 2 }),
    ],
    steps: [
      step('S0', 'H0', 'H1', 'enters', { lane: 'dom_event:onClick' }),
      step('S1', 'H1', 'H2', 'issues_request', { callsite: 'call:a', line: 2 }),
      step('S2', 'H2', 'H3', 'calls_endpoint', { resolution: { tier: 'inferred', rule: 'api-matching.v3', conditions: [CAVEAT, 'LOOPBACK_ORIGIN_ASSUMED_LOCAL_BACKEND'] }, segment: 'G0' }),
      step('S3', 'H3', 'H4', 'handled_by'),
      step('S4', 'H4', 'H5', 'exits_with', { attributes: { exit: 'return' } }),
      step('S5', 'H4', 'H6', 'raises_to', { path_condition: { terms: [[{ op: 'opaque', value: false, reason: 'CALL' }]], approximate: false }, feasibility: { verdict: 'undetermined', fragment: 'flow-feasibility.v1', assumptions: [] } }),
      step('S6', 'H5', 'H2', 'responds', { shared: true, attributes: { continuation: 'fulfilled', status: 201 }, resolution: { tier: 'inferred', rule: 'r', conditions: [CAVEAT] } }),
      step('S7', 'H6', 'H2', 'responds', { shared: true, attributes: { continuation: 'rejected', status: 400 }, resolution: { tier: 'inferred', rule: 'r', conditions: [CAVEAT] } }),
      step('S8', 'H6', 'H7', 'response_boundary', { lane: 'fastapi_background', feasibility: { verdict: 'refuted_in_fragment', fragment: 'flow-feasibility.v1', assumptions: [] } }),
      step('S9', 'H1', 'H8', 'invokes'),
    ],
    segments: [{ id: 'G0', at: 'n:H2', reason: 'AMBIGUOUS_CANDIDATE', alternatives: ['a', 'b'], steps: ['S2'] }],
    lanes: [{ id: 'main', order: 'SEQUENTIAL_BY_CFG_WITHIN_A_SCOPE' }, { id: 'dom_event:onClick', order: 'ORDER_NOT_DETERMINED' }, { id: 'fastapi_background', order: 'ORDER_NOT_DETERMINED' }],
    gaps: [{ kind: 'unresolved_callsite', step: 'S9', reasons: ['RECEIVER_TYPE_UNKNOWN'] }],
    boundaries: [], caveats: [CAVEAT], path_tier: 'inferred',
    effects: [{ step: 'S4', operation: 'dbop:x', class: 'stage', tables: ['db_table:items'], state: 'commit_reached', commit_position: { code: 'COMMIT_AFTER_RESPONSE_SENT', via: 'dependency_teardown' }, conditions: ['SESSION_IDENTITY_BY_PARAMETER'], claim: 'a commit is reached on the path; it may still raise' }],
    absence: [], levels: {}, run_id: 'r1', graph_digest: 'g', query: {}, verification: 'verified', trace_digest: 'd'.repeat(64), freshness_reasons: [],
  }
}
const entries = { run_id: 'r1', freshness_reasons: [], mode: null, items: [
  { key: 'entry:a', kind: 'http_endpoint', label: 'POST /api/items', locator: 'app/api.py', target: 's', tier: 'derived', conditions: [] },
  { key: 'entry:b', kind: 'http_endpoint', label: 'GET /api/items', locator: 'app/api.py', target: 's', tier: 'derived', conditions: [] },
], total: 2 }
const candidates = { ...entries, mode: 'lexical', requested_mode: 'lexical', selection: 'the user chooses', total: 1, items: [
  { ...entries.items[0], reason: { hit_rank: 1, chunk_locator: 'app/api.py', matched_scope: 's', distance: 0 } },
] }
const entity = {
  entity: { name: 'create_item', locator: 'app/api.py', start_line: 1, end_line: 3, stored_coverage: 'full', coverage_reason: null, context_coverage: 'full' },
  evidence: [{ chunk_id: 'c1', stable_key: 'k1', start_offset: 0, end_offset: 30, start_line: 1, end_line: 3, content: 'def create_item():\n    request()\n    return 1\n', verified: true }],
  package_chunks: [], integrity_errors: [], freshness_reasons: [],
}

function route(path: string): unknown {
  if (path === '/projects') return [{ id: 'p1', name: 'Demo', root_path: '/tmp/demo', created_at: '' }]
  if (path.endsWith('/flow')) return fresh
  if (path.includes('/flow/entries') && path.includes('q=')) return candidates
  if (path.includes('/flow/entries')) return entries
  if (path.includes('/flow/trace')) return trace(path.includes('entry%3Ab') ? 'only-in-trace-B' : 'only-in-trace-A')
  if (path.includes('/semantic/entities/')) return entity
  throw new Error(`unexpected ${path}`)
}

function show(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/flows" element={<FlowsPage />} /></Routes></MemoryRouter>)
}

let reactErrors: unknown[][] = []
beforeEach(() => {
  mocked.mockReset()
  reactErrors = []
  vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => { reactErrors.push(args) })
})
afterEach(() => {
  expect(reactErrors).toEqual([])
  vi.restoreAllMocks()
})

describe('FlowsPage', () => {
  it('renders four separate verdicts, caveats, alternatives, lanes and responses', async () => {
    mocked.mockImplementation(async (path: string) => route(path))
    show('/flows?project=p1&snapshot=s1&entry=entry:a')
    expect(await screen.findByText('only-in-trace-A')).toBeInTheDocument()
    expect(screen.getByText(/the Stage 14 API matching has not been independently evaluated/)).toBeInTheDocument()
    expect(screen.getAllByText('HTTP link not independently evaluated').length).toBeGreaterThan(0)
    expect(screen.getByText('may reach endpoint')).toBeInTheDocument()
    expect(screen.getByText(/alternative · G0/)).toBeInTheDocument()
    expect(screen.getAllByText(/^L2 reachable in model/).length).toBeGreaterThan(0)
    expect(screen.getByText('L4 refuted in fragment')).toBeInTheDocument()
    expect(screen.getByText('evil⟪U+202E⟫name')).toBeInTheDocument()
    expect(screen.queryByText(/executes/)).not.toBeInTheDocument()
    expect(screen.getByText(/Why this is not a complete claim \(1\)/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'lanes' }))
    expect(screen.getAllByText(/order not determined/).length).toBe(2)
    expect(screen.getByRole('img', { name: 'Lanes of the trace' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'responses' }))
    expect(screen.getByText(/fulfilled at/)).toBeInTheDocument()
    expect(screen.getByText(/rejected at/)).toBeInTheDocument()
    expect(screen.getByText('the client may observe success before the commit')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'branches' }))
    expect(screen.getByText(/opaque condition \(CALL\) is false/)).toBeInTheDocument()
  })

  it('inspects a step: four verdicts and the cited line in stored source', async () => {
    mocked.mockImplementation(async (path: string) => route(path))
    show('/flows?project=p1&snapshot=s1&entry=entry:a&step=S1')
    expect(await screen.findByText('L3 path condition')).toBeInTheDocument()
    expect(screen.getAllByText(/not an execution/).length).toBeGreaterThan(0)
    expect(await screen.findByLabelText('Lines 1–3')).toBeInTheDocument()
    expect(document.querySelector('.source-line.cited')?.textContent).toContain('request()')
  })

  it('hides refuted steps only as a presentation filter', async () => {
    mocked.mockImplementation(async (path: string) => route(path))
    show('/flows?project=p1&snapshot=s1&entry=entry:a&refuted=hide')
    expect(await screen.findByText(/1 refuted step hidden by the filter; the trace keeps them/)).toBeInTheDocument()
    expect(screen.queryByText('L4 refuted in fragment')).not.toBeInTheDocument()
  })

  it('proposes search candidates with reasons and never selects one', async () => {
    mocked.mockImplementation(async (path: string) => route(path))
    show('/flows?project=p1&snapshot=s1')
    fireEvent.change(await screen.findByPlaceholderText('what happens after login'), { target: { value: 'create item' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find candidates' }))
    expect(await screen.findByText(/Mode used:/)).toBeInTheDocument()
    expect(screen.getByText(/search hit #1/)).toBeInTheDocument()
    expect(mocked.mock.calls.some(([path]) => String(path).includes('/flow/trace'))).toBe(false)
    fireEvent.click(screen.getAllByRole('button', { name: 'POST /api/items' })[0])
    expect(await screen.findByText('only-in-trace-A')).toBeInTheDocument()
  })

  it('fails closed on integrity failures and explains stale models', async () => {
    mocked.mockImplementation(async (path: string) => {
      if (path.includes('/flow/trace')) throw new ApiError(409, 'FLOW_INTEGRITY_FAILED', 'x')
      return route(path)
    })
    show('/flows?project=p1&snapshot=s1&entry=entry:a')
    expect(await screen.findByText(/no partial trace is shown/)).toBeInTheDocument()
    expect(screen.queryByText('only-in-trace-A')).not.toBeInTheDocument()
  })

  it('shows no entry points or trace for a stale model', async () => {
    mocked.mockImplementation(async (path: string) => path.endsWith('/flow')
      ? { ...fresh, freshness: 'stale', freshness_reasons: ['STRUCTURAL_AUTHORITY_CHANGED'] } : route(path))
    show('/flows?project=p1&snapshot=s1&entry=entry:a')
    expect(await screen.findByText(/stale \(STRUCTURAL_AUTHORITY_CHANGED\)/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Entry points')).not.toBeInTheDocument()
  })

  it('never paints a resolved trace under a newly selected entry, not even for one commit', async () => {
    type Pending = { path: string; resolve: (value: unknown) => void }
    const pending: Pending[] = []
    mocked.mockImplementation((path: string) => {
      if (!path.includes('/flow/trace')) return Promise.resolve(route(path)) as never
      return new Promise(resolve => { pending.push({ path, resolve }) }) as never
    })
    const frames: string[] = []
    const record = () => { frames.push(document.querySelector('[aria-label="Trace"]')?.textContent ?? '') }
    render(<MemoryRouter initialEntries={['/flows?project=p1&snapshot=s1&entry=entry:a']}><Profiler id="flows" onRender={record}><Routes><Route path="/flows" element={<FlowsPage />} /></Routes></Profiler></MemoryRouter>)
    await screen.findByRole('button', { name: 'GET /api/items' })
    const first = pending.shift() as Pending
    await act(async () => { first.resolve(route(first.path)) })
    expect(screen.getByText('only-in-trace-A')).toBeInTheDocument()
    frames.length = 0
    fireEvent.click(screen.getByRole('button', { name: 'GET /api/items' }))
    expect(frames.length).toBeGreaterThan(0)
    for (const frame of frames) expect(frame).not.toContain('only-in-trace-A')
    // A late response for the old entry never applies.
    await act(async () => { first.resolve(trace('late-A')) })
    expect(screen.queryByText('late-A')).not.toBeInTheDocument()
    const second = pending.shift() as Pending
    await act(async () => { second.resolve(route(second.path)) })
    expect(screen.getByText('only-in-trace-B')).toBeInTheDocument()
  })
})
