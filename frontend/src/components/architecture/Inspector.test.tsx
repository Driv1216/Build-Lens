import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '../../api/client'
import type { ClassSet } from '../../api/architecture'
import { Inspector } from './Inspector'

vi.mock('../../api/client', async (original) => ({ ...(await original<typeof import('../../api/client')>()), request: vi.fn() }))
const mocked = vi.mocked(request)

// Every request is held until the test settles it, in whatever order the test chooses.
type Pending = { path: string; signal: AbortSignal | undefined; resolve: (value: unknown) => void }
let pending: Pending[] = []
let reactErrors: unknown[][] = []
beforeEach(() => {
  pending = []
  reactErrors = []
  // React reports duplicate keys and similar identity faults through console.error; the
  // panels below rely on their keys to remount per selection, so any such report fails.
  vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => { reactErrors.push(args) })
  mocked.mockReset()
  mocked.mockImplementation((path: string, init?: RequestInit) => new Promise(resolve => {
    pending.push({ path, signal: init?.signal ?? undefined, resolve })
  }) as never)
})

afterEach(() => {
  expect(reactErrors).toEqual([])
  vi.restoreAllMocks()
})

function take(part: string): Pending {
  const index = pending.findIndex(item => item.path.includes(part))
  if (index < 0) throw new Error(`no pending request for ${part}: ${pending.map(item => item.path).join(', ')}`)
  return pending.splice(index, 1)[0]
}

async function settle(item: Pending, value: unknown) {
  await act(async () => { item.resolve(value) })
}

const scoped = { run_id: 'r1', freshness_reasons: [], verification: 'verified' }
const evidence = (digest: string) => ({ source: 'architecture', digest })
function detail(key: string, name: string, links = [evidence('a'.repeat(64))]) {
  return {
    ...scoped,
    node: { key, kind: 'module', parent_key: null, name, locator: name, language: 'Python', catalog_entity_key: null, coverage_state: 'analysed', attributes: {}, metrics: {}, evidence: links },
    chain: [], children: [], outgoing: [], incoming: [], outgoing_total: 0, incoming_total: 0, components: [], diagnostics: [],
  }
}
function dependencies(module: string) {
  return { ...scoped, origin: 'x', origins: ['x'], direction: 'out', class_set: 'S2', depth: 32, search: 'complete', frontier: 0,
    reached: [{ node: `module:${module}`, depth: 1, path: [] }], edges: {}, nodes: {}, gaps: { complete: true, unknown_exits: [], coverage_gaps: [], run_gaps: [] }, by_container: {} }
}

function inspector(node: string, classSet: ClassSet = 'S2', revision = '1', snapshotId = 's1') {
  return <MemoryRouter><Inspector projectId="p1" snapshotId={snapshotId} revision={revision} node={node} classSet={classSet} onSelect={() => undefined} /></MemoryRouter>
}

describe('Inspector stale-response guards', () => {
  it('never shows a late node response under a newer selection', async () => {
    const view = render(inspector('module:a.py'))
    const first = take('/node')
    view.rerender(inspector('module:b.py'))
    expect(first.signal?.aborted).toBe(true)
    await settle(first, detail('module:a.py', 'a.py'))
    expect(screen.queryByText('a.py')).toBeNull()
    expect(screen.getByRole('status')).toHaveTextContent('Verifying')
    await settle(take('/node'), detail('module:b.py', 'b.py'))
    expect(screen.getByRole('heading', { name: 'b.py' })).toBeInTheDocument()
    // A run (revision) or snapshot change drops the shown detail at once, before any refetch.
    view.rerender(inspector('module:b.py', 'S2', '2'))
    expect(screen.queryByRole('heading', { name: 'b.py' })).toBeNull()
    view.rerender(inspector('module:b.py', 'S2', '2', 's2'))
    expect(take('/node').signal?.aborted).toBe(true)
  })

  it('drops dependency and path results requested for an earlier node or class set', async () => {
    const view = render(inspector('module:a.py'))
    await settle(take('/node'), detail('module:a.py', 'a.py'))
    fireEvent.click(screen.getByRole('button', { name: 'Depends on' }))
    const outgoing = take('/dependencies')
    view.rerender(inspector('module:a.py', 'S4'))  // same node, other class set
    expect(outgoing.signal?.aborted).toBe(true)
    await settle(outgoing, dependencies('stale.py'))
    expect(screen.queryByText('stale.py')).toBeNull()
    // Two clicks in a row: only the second may deliver, whatever the arrival order.
    fireEvent.click(screen.getByRole('button', { name: 'Depends on' }))
    const first = take('/dependencies')
    fireEvent.click(screen.getByRole('button', { name: 'Depended on by' }))
    const second = take('/dependencies')
    expect(first.signal?.aborted).toBe(true)
    await settle(second, dependencies('inbound.py'))
    await settle(first, dependencies('outbound.py'))
    expect(screen.getByText('inbound.py')).toBeInTheDocument()
    expect(screen.queryByText('outbound.py')).toBeNull()
    // A path search in flight when the node changes never appears under the new node.
    fireEvent.change(screen.getByLabelText(/Path to module/), { target: { value: 'app/db.py' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find path' }))
    const path = take('/path')
    view.rerender(inspector('module:b.py', 'S4'))
    expect(path.signal?.aborted).toBe(true)
    await settle(take('/node'), detail('module:b.py', 'b.py'))
    await settle(path, { ...scoped, source: 'module:a.py', target: 'module:app/db.py', class_set: 'S4', status: 'found', path: [], gaps: { complete: true, unknown_exits: [], coverage_gaps: [], run_gaps: [] } })
    expect(screen.getByRole('heading', { name: 'b.py' })).toBeInTheDocument()
    expect(screen.queryByText('found')).toBeNull()
  })

  it('shows only the evidence item clicked last', async () => {
    render(inspector('module:a.py'))
    await settle(take('/node'), detail('module:a.py', 'a.py', [evidence('1'.repeat(64)), evidence('2'.repeat(64))]))
    fireEvent.click(screen.getByRole('button', { name: '1'.repeat(12) }))
    const first = take(`/evidence/${'1'.repeat(64)}`)
    fireEvent.click(screen.getByRole('button', { name: '2'.repeat(12) }))
    const second = take(`/evidence/${'2'.repeat(64)}`)
    expect(first.signal?.aborted).toBe(true)
    const item = (digest: string, text: string) => ({ run_id: 'r1', digest, kind: 'import_statements', source_refs: [], derived: {}, renderer_version: 'v', rendered_text: text, verification: 'verified', label: '' })
    await settle(second, item('2'.repeat(64), 'second fact'))
    await settle(first, item('1'.repeat(64), 'first fact'))
    expect(screen.getByText('second fact')).toBeInTheDocument()
    expect(screen.queryByText('first fact')).toBeNull()
  })
})
