import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '../api/client'
import { ArtifactProcessingPanel } from './ArtifactProcessingPanel'
import { StructuralAnalysisPanel } from './StructuralAnalysisPanel'
import { EvidencePage } from './EvidencePage'
import { SemanticSearchPanel } from './SemanticSearchPanel'

vi.mock('../api/client', () => ({ request: vi.fn() }))
const mocked = vi.mocked(request)
const empty = { items: [], total: 0, limit: 50, offset: 0 }
const none = { latest_attempt: null, active_run: null, active_freshness: 'none', freshness_reasons: [] }
const run = { run_id: 'run1', revision: 1, outcome: 'complete', quarantined: false, superseded: false }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
beforeEach(() => { mocked.mockReset() })

describe('evidence request scopes', () => {
  it('ignores a late status response from the previous snapshot', async () => {
    const late = deferred<unknown>()
    mocked.mockImplementation(path => path.includes('/old/processing') && !path.includes('/runs') ? late.promise : Promise.resolve(path.includes('/runs') ? empty : none))
    const view = render(<ArtifactProcessingPanel projectId="p" snapshotId="old" />)
    view.rerender(<ArtifactProcessingPanel projectId="p" snapshotId="new" />)
    await act(async () => late.resolve({ ...none, latest_attempt: { ...run, revision: 99 } }))
    expect(screen.queryByText(/#99/)).not.toBeInTheDocument()
    expect(screen.getByText('Latest attempt: none')).toBeInTheDocument()
  })
  it('clears selected content and ignores a late chunk response after switching snapshots', async () => {
    const late = deferred<unknown>()
    mocked.mockImplementation(path => {
      if (path.includes('/chunks/chunk1')) return late.promise
      if (path.includes('/new/')) return Promise.resolve(path.endsWith('/processing') ? none : empty)
      if (path.endsWith('/processing')) return Promise.resolve({ ...none, latest_attempt: run, active_run: run, active_freshness: 'fresh' })
      if (path.includes('/chunks?')) return Promise.resolve({ ...empty, total: 1, items: [{ id: 'chunk1', locator: 'old.py', kind: 'python', span: { start_line: 1, end_line: 2 } }] })
      return Promise.resolve(empty)
    })
    const view = render(<ArtifactProcessingPanel projectId="p" snapshotId="old" />)
    fireEvent.click(await screen.findByRole('button', { name: /old.py/ }))
    view.rerender(<ArtifactProcessingPanel projectId="p" snapshotId="new" />)
    await act(async () => late.resolve({ id: 'chunk1', content: 'obsolete source', locator: 'old.py', run_id: 'run1' }))
    expect(screen.queryByText('obsolete source')).not.toBeInTheDocument()
    expect(screen.queryByText('Chunk provenance')).not.toBeInTheDocument()
  })
  it('paginates using server totals and does not restore a superseded page', async () => {
    mocked.mockImplementation(path => Promise.resolve({ items: [{ id: path.includes('offset=50') ? 'second' : 'first' }], total: 51, limit: 50, offset: path.includes('offset=50') ? 50 : 0 }))
    render(<EvidencePage<{ id: string }> path="/items" title="Items" render={item => item.id} />)
    expect(await screen.findByText('first')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText('second')).toBeInTheDocument()
    expect(screen.getByText('51–51 of 51')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
  })
  it('refreshes dependent evidence after structural publication', async () => {
    mocked.mockImplementation(path => Promise.resolve(path.endsWith('/status') ? { latest_attempt: null, active_run: null } : empty))
    const refresh = vi.fn()
    render(<StructuralAnalysisPanel projectId="p" snapshotId="s" onRefresh={refresh} />)
    fireEvent.click(screen.getByRole('button', { name: 'Analyse Python files' }))
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce())
  })
  it('ignores late semantic results after switching snapshots', async () => {
    const late = deferred<unknown>()
    const prepared = {
      model: { available: true, path: '/cache', reason: null },
      contract: { model_id: 'test/model', dimensions: 2 },
      prepared: true,
      search_available: true,
      active_processing_run_id: 'run1',
      freshness_reasons: [],
      active_run: { run_id: 'embedding1', revision: 1, chunk_count: 1, processing_revision: 1, coverage: { embedded_chunks: 1, embedding_windows: 1, gaps: 0 } },
      latest_attempt: null,
    }
    mocked.mockImplementation((path, init) => {
      if (path.includes('/old/semantic-search') && init?.method === 'POST') return late.promise
      return Promise.resolve(path.includes('/new/embeddings') ? { ...prepared, prepared: false, search_available: false, active_run: null } : prepared)
    })
    const view = render(<SemanticSearchPanel projectId="p" snapshotId="old" />)
    fireEvent.change(await screen.findByLabelText('Semantic query'), { target: { value: 'find alpha' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    view.rerender(<SemanticSearchPanel projectId="p" snapshotId="new" />)
    await act(async () => late.resolve({
      contract: prepared.contract,
      embedding_run: prepared.active_run,
      hits: [{ chunk_id: 'old', score: 0.9, locator: 'obsolete.py', span: { start_line: 1, end_line: 2 }, kind: 'source', stable_key: 'old', window_count: 1, heading_path: [], json_path: null, content: 'obsolete source' }],
    }))
    expect(screen.queryByText('obsolete source')).not.toBeInTheDocument()
    expect(screen.getByText(/not prepared for current evidence/)).toBeInTheDocument()
  })
  it('shows actionable offline model guidance', async () => {
    mocked.mockResolvedValue({
      model: { available: false, path: '/cache', reason: 'MODEL_MANIFEST_MISSING' },
      contract: { model_id: 'test/model', dimensions: 384 },
      prepared: false,
      search_available: false,
      active_processing_run_id: null,
      freshness_reasons: [],
      active_run: null,
      latest_attempt: null,
    })
    render(<SemanticSearchPanel projectId="p" snapshotId="s" />)
    expect(await screen.findByText(/buildlens embeddings download-model/)).toBeInTheDocument()
    expect(screen.getByLabelText('Search mode')).toHaveValue('lexical')
    fireEvent.change(screen.getByLabelText('Semantic query'), { target: { value: 'get user' } })
    expect(screen.getByRole('button', { name: 'Search' })).toBeEnabled()
    fireEvent.change(screen.getByLabelText('Search mode'), { target: { value: 'hybrid' } })
    expect(screen.getByRole('button', { name: 'Search' })).toBeDisabled()
    expect(screen.getByText(/Hybrid requires both lexical retrieval/)).toBeInTheDocument()
  })
  it('builds and inspects bounded context independently of top results', async () => {
    const prepared = {
      model: { available: true, path: '/embeddings', reason: null },
      contract: { model_id: 'test/embedding', dimensions: 384 },
      prepared: true,
      search_available: true,
      active_processing_run_id: 'processing1',
      freshness_reasons: [],
      active_run: null,
      latest_attempt: null,
    }
    mocked.mockImplementation((path, init) => {
      if (path === '/reranker') return Promise.resolve({
        available: true,
        path: '/reranker',
        reason: null,
        runtime_verified: false,
        acquisition_command: 'buildlens reranking download-model',
        contract: { model_id: 'test/reranker' },
      })
      if (path.includes('/embeddings/runs')) return Promise.resolve(empty)
      if (path.endsWith('/embeddings')) return Promise.resolve(prepared)
      if (path.endsWith('/context') && init?.method === 'POST') return Promise.resolve({
        item_count: 1,
        ranking_policy: 'local_cross_encoder',
        reranker_state: 'completed',
        retrieval: { candidate_count: 3, candidate_depth: 100, candidates_truncated: false },
        budget: { used: 5300, limit: 8000, remaining: 2700, measurement: { policy: 'canonical-json-evidence.v1' } },
        items: [{
          chunk_id: 'chunk1',
          locator: 'src/example.py',
          span: { start_line: 4, end_line: 8 },
          context_cost: 1200,
          selected_as_seed: true,
          retrieval: { rank: 2 },
          rerank_rank: 1,
          rerank_score: -0.5,
          rerank_window_count: 1,
          expanded_from: [],
          stable_key: 'stable',
          part_index: 0,
          unit_anchor: 'function:run',
          content: 'def run(): pass',
        }],
        decisions: [{ disposition: 'excluded', chunk_id: 'chunk2', locator: 'src/other.py', stage: 'packing', reason: 'remaining_budget', retrieval_rank: 3, rerank_rank: 2 }],
      })
      return Promise.resolve(prepared)
    })
    render(<SemanticSearchPanel projectId="p" snapshotId="s" />)
    fireEvent.change(await screen.findByLabelText('Semantic query'), { target: { value: 'run function' } })
    fireEvent.click(screen.getByRole('button', { name: 'Build context' }))
    expect(await screen.findByText(/5,300 of 8,000 UTF-8 bytes/)).toBeInTheDocument()
    expect(screen.getByText('def run(): pass')).toBeInTheDocument()
    expect(screen.getByText(/rerank #1 · raw logit -0.500000/)).toBeInTheDocument()
    expect(screen.getByText(/packing: remaining_budget/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Top results'), { target: { value: '5' } })
    expect(screen.getByText('def run(): pass')).toBeInTheDocument()
  })
})
