import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request } from '../../api/client'
import { ArchitecturePage } from './ArchitecturePage'
import { ArchitecturePanel } from './ArchitecturePanel'

vi.mock('../../api/client', async (original) => ({ ...(await original<typeof import('../../api/client')>()), request: vi.fn() }))
const mocked = vi.mocked(request)
beforeEach(() => { mocked.mockReset() })

const run = { run_id: 'r1', snapshot_id: 's1', revision: 1, started_at: '', completed_at: '', outcome: 'partial', catalog_run_id: 'c1', graph_digest: 'g', architecture_digest: 'a', portable_digest: 'p', coverage: {}, diagnostics: [], superseded: false, quarantined: false }
const fresh = { latest_attempt: run, active_run: run, freshness: 'fresh', freshness_reasons: [], catalog_ready: true }
const node = (key: string, extra: Record<string, unknown> = {}) => ({
  key, kind: key.split(':')[0], parent_key: null, name: key.replace(/^[a-z_]+:/, '').split('/').pop(), locator: null, language: 'Python', catalog_entity_key: 'file:1',
  coverage_state: 'analysed', attributes: {}, metrics: { fan_in: 1, fan_out: 2 }, evidence: [{ source: 'architecture', digest: 'e'.repeat(64) }], ...extra,
})
const edge = { key: 'k1', kind: 'imports', source_key: 'module:app/a.py', target_key: 'module:app/b.py', target_text: null, tier: 'derived', resolution_status: 'resolved', import_class: 'presumed_unconditional', count: 2, attributes: {}, conditions: ['PYTHON_GUARD_CONTEXT_UNKNOWN'], evidence: [] }
const scoped = { run_id: 'r1', freshness_reasons: [], verification: 'verified' }

function route(path: string): unknown {
  if (path === '/projects') return [{ id: 'p1', name: 'Demo', root_path: '/tmp/demo', created_at: '' }]
  if (path.endsWith('/architecture')) return fresh
  if (path.includes('/architecture/overview')) return { ...scoped, workspaces: [node('workspace:.', { kind: 'workspace', attributes: { manifests: ['pyproject.toml'] } })], node_counts: { module: 3 }, edge_counts: {}, component_counts: { 'module:S1': 1 }, diagnostic_counts: { IMPORT_CYCLE: 1 }, coverage: { alias: { statements: 2, prefixes: { '@/': 2 } }, statements: { by_resolution: { resolved: 4 }, by_class: { presumed_unconditional: 4 } } } }
  if (path.includes('/architecture/tree')) return { ...scoped, parent: null, children: [node('container:app', { kind: 'container' })], diagnostic_counts: { 'container:app': 1 } }
  if (path.includes('/architecture/node')) return { ...scoped, node: node('module:app/a.py', { kind: 'module', name: 'a‮.py' }), chain: [], children: [], outgoing: [edge], incoming: [], outgoing_total: 1, incoming_total: 0, components: [], diagnostics: [] }
  if (path.includes('/architecture/cycles')) return { ...scoped, items: [{ key: 'component:module:x', level: 'module', class_set: 'S1', scope_key: null, members: ['module:app/a.py', 'module:app/b.py'], witness: ['k1'], attributes: {}, evidence: [] }], edges: { k1: edge }, total: 1 }
  if (path.includes('/architecture/path')) return { ...scoped, source: 'module:app/b.py', target: 'module:app/a.py', class_set: 'S2', status: 'not_found_in_observed_graph', path: [], gaps: { complete: false, unknown_exits: [], coverage_gaps: [], run_gaps: ['OPAQUE_DYNAMIC_IMPORTS_NOT_OBSERVABLE'] } }
  if (path.includes('/architecture/api')) return {
    ...scoped,
    endpoints: [node('endpoint:symbol:app/api.py#get_user', { kind: 'api_endpoint', attributes: { function: 'get_user', methods: ['GET'], paths: [{ path: '/api/users/{user_id}', status: 'derived', reasons: [], conditions: [] }], reasons: [] } })],
    endpoints_total: 1,
    clients: [node('client:web/api.ts:3', { kind: 'client_call', locator: 'web/api.ts', attributes: { line: 4, method: 'GET', wrapper: 'web/client.ts#request', branches: [
      { display: '{…}/users/{…}', destination: 'opaque_origin', outcome: 'opaque_origin', candidates: [], conditions: [], reasons: ['BASE_UNKNOWN'], host: null, port: null, scheme: null },
      { display: '/api/users/{…}', destination: 'local_loopback', outcome: 'conditional_candidate', candidates: ['endpoint:symbol:app/api.py#get_user'], conditions: ['BASE_FROM_FALLBACK_LITERAL', 'LOOPBACK_ORIGIN_ASSUMED_LOCAL_BACKEND'], reasons: [], strength: 'segment_aligned', host: '127.0.0.1', port: '8000', scheme: 'http' },
    ] } })],
    clients_total: 1,
    destinations: [node('destination:https://api.example.com', { kind: 'external_destination', attributes: { host: 'api.example.com', class: 'public_host_unbound', ownership: 'unknown' } })],
    routers: [], connections: [], outcome_counts: { conditional_candidate: 1, opaque_origin: 1 },
    coverage: { endpoints: { total: 1, with_full_path: 1, reasons: {} }, unrecognized_clients: 0, url_usage: {} }, limit: 100, offset: 0,
  }
  throw new Error(`unexpected ${path}`)
}

function show(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/architecture" element={<ArchitecturePage />} /></Routes></MemoryRouter>)
}

describe('ArchitecturePage', () => {
  it('shows verified overview counts, alias gaps and the containment tree', async () => {
    mocked.mockImplementation(async (path: string) => route(path))
    show('/architecture?project=p1&snapshot=s1')
    expect(await screen.findByText(/re-verified against its persisted rows/)).toBeInTheDocument()
    expect(screen.getByText(/tsconfig is never read/)).toBeInTheDocument()
    expect(await screen.findByText(/not a subsystem model/)).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Expand app' })).toBeInTheDocument()
    expect(screen.getByText(/1 diagnostic$/)).toBeInTheDocument()
  })

  it('labels cycles as static relations and renders bidi controls visibly in the inspector', async () => {
    mocked.mockImplementation(async (path: string) => route(path))
    show('/architecture?project=p1&snapshot=s1&view=cycles&node=module:app/a.py')
    expect(await screen.findByText(/does not claim a runtime circular-import failure/)).toBeInTheDocument()
    expect(await screen.findByText('a⟪U+202E⟫.py')).toBeInTheDocument()
    expect(screen.getAllByText('presumed unconditional').length).toBeGreaterThan(0)
    expect(screen.getByText(/PYTHON_GUARD_CONTEXT_UNKNOWN/)).toBeInTheDocument()
  })

  it('never reports a plain absence when gaps remain', async () => {
    mocked.mockImplementation(async (path: string) => route(path))
    show('/architecture?project=p1&snapshot=s1&node=module:app/a.py')
    fireEvent.change(await screen.findByPlaceholderText('app/db.py'), { target: { value: 'app/b.py' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find path' }))
    expect(await screen.findByText('not found in observed graph')).toBeInTheDocument()
    expect(screen.getByText(/OPAQUE_DYNAMIC_IMPORTS_NOT_OBSERVABLE/)).toBeInTheDocument()
  })

  it('shows API connections only as conditional candidates with their assumptions', async () => {
    mocked.mockImplementation(async (path: string) => route(path))
    show('/architecture?project=p1&snapshot=s1&view=api')
    expect(await screen.findByText(/none is a confirmed call/)).toBeInTheDocument()
    expect(screen.getByText('conditional candidate')).toBeInTheDocument()
    expect(screen.getByText('base unknown')).toBeInTheDocument()
    expect(screen.getByText('LOOPBACK_ORIGIN_ASSUMED_LOCAL_BACKEND')).toBeInTheDocument()
    expect(screen.getByText(/ownership is unknown/)).toBeInTheDocument()
    expect(screen.getByText('/api/users/{user_id}')).toBeInTheDocument()
    expect(screen.queryByText(/confirmed match/)).not.toBeInTheDocument()
  })

  it('fails closed with an explanation instead of partial content', async () => {
    mocked.mockImplementation(async (path: string) => {
      if (path.includes('/architecture/overview')) throw new ApiError(409, 'ARCHITECTURE_EVIDENCE_INVALID', 'x')
      if (path.includes('/architecture/tree')) throw new ApiError(409, 'ARCHITECTURE_INTEGRITY_FAILED', 'x')
      return route(path)
    })
    show('/architecture?project=p1&snapshot=s1')
    expect(await screen.findByText(/no partial answer is shown/)).toBeInTheDocument()
    expect(await screen.findByText(/failed integrity verification/)).toBeInTheDocument()
  })

  it('explains stale models without rendering views', async () => {
    mocked.mockImplementation(async (path: string) => path.endsWith('/architecture')
      ? { ...fresh, freshness: 'stale', freshness_reasons: ['CATALOG_AUTHORITY_CHANGED'] } : route(path))
    show('/architecture?project=p1&snapshot=s1')
    expect(await screen.findByText(/stale \(CATALOG_AUTHORITY_CHANGED\)/)).toBeInTheDocument()
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
  })
})

describe('ArchitecturePanel', () => {
  it('builds through a JSON mutation and links to the workspace', async () => {
    mocked.mockImplementation(async (_path: string, init?: RequestInit) => init?.method === 'POST' ? fresh : { ...fresh, active_run: null, freshness: 'none' })
    render(<MemoryRouter><ArchitecturePanel projectId="p1" snapshotId="s1" /></MemoryRouter>)
    fireEvent.click(await screen.findByRole('button', { name: 'Build architecture' }))
    await waitFor(() => expect(mocked).toHaveBeenCalledWith('/projects/p1/snapshots/s1/architecture', expect.objectContaining({ method: 'POST', body: '{}' })))
    expect(await screen.findByText('Open the architecture workspace →')).toBeInTheDocument()
  })
})
