import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, request } from '../../api/client'
import { SemanticCatalogPanel } from '../SemanticCatalogPanel'
import { ExplorePage } from './ExplorePage'

vi.mock('../../api/client', async (original) => ({ ...(await original<typeof import('../../api/client')>()), request: vi.fn() }))
const mocked = vi.mocked(request)
beforeEach(() => { mocked.mockReset() })

const entity = (overrides: Record<string, unknown>) => ({
  entity_key: 'symbol:1', kind: 'symbol', language: 'Python', locator: 'app/api.py', name: 'Store', qualified_name: 'Store',
  symbol_kind: 'class', start_line: 1, end_line: 6, subject_entity_key: null, stored_coverage: 'complete', coverage_reason: null,
  context_coverage: 'outline', context_reason: null, package_digest: 'a'.repeat(64), package_bytes: 120, limitations: [], ...overrides,
})
const page = { run_id: 'r1', freshness_reasons: [], total: 2, limit: 200, offset: 0, items: [
  entity({ entity_key: 'file:1', kind: 'file', name: 'app/api.py', qualified_name: null, symbol_kind: null }),
  entity({}),
] }
const detail = {
  run_id: 'r1', freshness_reasons: [], entity: entity({}), gaps: { count: 1, characters: 1 }, package_chunks: ['k1'],
  omitted: [{ entity_key: 'symbol:2', start_line: 2, end_line: 3 }],
  evidence: [
    { chunk_id: 'c1', stable_key: 'k1', content_hash: 'h', start_offset: 0, end_offset: 13, start_line: 1, end_line: 2, symbol_identity_id: '1', content: 'class Store:\n', verified: true },
    { chunk_id: 'c2', stable_key: 'k2', content_hash: 'h', start_offset: 14, end_offset: 40, start_line: 2, end_line: 3, symbol_identity_id: '2', content: '    def get(self):\n        return 1\n', verified: true },
  ],
  facts: [
    { fact_digest: 'f1', fact_kind: 'signature', subject_entity_key: 'symbol:1', basis: 'parsed', rendered_text: 'class Store at app/api.py lines 1–6: class Store', renderer_version: 'fact-renderer.v1', source_refs: [{ table: 'symbols', id: '00000000-1', contract: null, locator: 'app/api.py' }], derived: {}, verification: 'verified' },
    { fact_digest: 'f2', fact_kind: 'recognizer_match', subject_entity_key: 'symbol:1', basis: 'pattern_only', rendered_text: 'sqlalchemy-declarative.v1 (pattern only): Store', renderer_version: 'fact-renderer.v1', source_refs: [], derived: { met: ['tablename_assignment'], unmet: ['orm_import'] }, verification: 'FACT_DIGEST_MISMATCH' },
  ],
  neighbor_facts: [], outgoing: [{ kind: 'inherits', source_entity_key: 'symbol:1', target_entity_key: null, target_text: 'Base', resolution_status: 'external', fact_digest: 'f3' }],
  incoming: [{ kind: 'contains', source_entity_key: 'file:1', target_entity_key: 'symbol:1', target_text: null, resolution_status: null, fact_digest: 'f4' }],
  integrity_errors: ['FACT_DIGEST_MISMATCH:f2'], related: { 'file:1': 'file app/api.py', 'symbol:1': 'class Store · app/api.py' },
}

function show(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/explore" element={<ExplorePage />} /></Routes></MemoryRouter>)
}

describe('Explore', () => {
  it('lists entities, shows verified source with outline shading and labelled facts', async () => {
    mocked.mockImplementation(path => Promise.resolve(path === '/projects' ? [{ id: 'p', name: 'Demo' }] : path.includes('/entities/') ? detail : page))
    show('/explore?project=p&snapshot=s')
    fireEvent.click(await screen.findByRole('button', { name: /Store/ }))
    expect(await screen.findByText('Pattern only')).toBeInTheDocument()
    expect(screen.getByText('Parsed')).toBeInTheDocument()
    expect(screen.getByText(/Met: tablename_assignment · Unmet: orm_import/)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('FACT_DIGEST_MISMATCH')
    expect(screen.getByText(/1 whitespace characters not stored/)).toBeInTheDocument()
    expect(screen.getByLabelText('Lines 2–3')).toHaveClass('omitted')
    expect(screen.getByLabelText('Lines 1–2')).not.toHaveClass('omitted')
    expect(mocked).toHaveBeenCalledWith(expect.stringContaining('/entities/symbol%3A1'), expect.anything())
    fireEvent.click(screen.getByRole('tab', { name: 'Relations' }))
    expect(screen.getByText('Base')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'file app/api.py' }))
    await waitFor(() => expect(mocked).toHaveBeenCalledWith(expect.stringContaining('/entities/file%3A1'), expect.anything()))
  })
  it('explains a stale catalog instead of showing entities', async () => {
    mocked.mockImplementation(path => path === '/projects' ? Promise.resolve([{ id: 'p', name: 'Demo' }]) : Promise.reject(new ApiError(409, 'SEMANTIC_CATALOG_STALE', 'stale')))
    show('/explore?project=p&snapshot=s')
    expect(await screen.findByRole('alert')).toHaveTextContent('The catalog is stale')
  })
  it('passes filters to the entity query', async () => {
    mocked.mockImplementation(path => Promise.resolve(path === '/projects' ? [{ id: 'p', name: 'Demo' }] : page))
    show('/explore?project=p&snapshot=s')
    await screen.findByRole('button', { name: /Store/ })
    fireEvent.change(screen.getByLabelText('Filter kind'), { target: { value: 'component' } })
    await waitFor(() => expect(mocked).toHaveBeenCalledWith(expect.stringContaining('kind=component'), expect.anything()))
  })
})

describe('SemanticCatalogPanel', () => {
  it('builds the catalog and links to Explore once fresh', async () => {
    const fresh = { latest_attempt: { revision: 1, outcome: 'complete', superseded: false }, active_run: { revision: 1, coverage: { entity_kinds: { file: 2 } } }, freshness: 'fresh', freshness_reasons: [] }
    mocked.mockImplementation((_path, init) => Promise.resolve(init?.method === 'POST' ? fresh : { latest_attempt: null, active_run: null, freshness: 'none', freshness_reasons: [] }))
    render(<MemoryRouter><SemanticCatalogPanel projectId="p" snapshotId="s" /></MemoryRouter>)
    expect(await screen.findByText('Active catalog: none')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Build catalog' }))
    expect(await screen.findByRole('link', { name: /Explore the catalog/ })).toHaveAttribute('href', '/explore?project=p&snapshot=s')
    expect(screen.getByText('file: 2')).toBeInTheDocument()
  })
})
