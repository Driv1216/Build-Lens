import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '../api/client'
import { AskPanel } from './AskPanel'

vi.mock('../api/client', () => ({ request: vi.fn() }))
const mocked = vi.mocked(request)
const generation = { enabled: true, dependency_installed: true, key_present: true, configured: true, provider: 'gemini', model_id: 'gemini-3.8-flash', policy_version: 'v1', prompt_version: 'v1', guidance: 'Configure Ask.' }
const embedding = { search_available: true, active_processing_run_id: 'run1', active_run: { run_id: 'embed1' } }
const reranker = { available: true }
const result = {
  request_id: 'request1', status: 'answered', reason: null,
  answer: { text: 'Shown.', statements: [{ text: 'Shown.', support: 'direct', evidence_ids: ['E1'] }] },
  context: { processing_run_id: 'run1', embedding_run_id: 'embed1', retrieval: { requested_mode: 'hybrid', executed_mode: 'hybrid' }, ranking_policy: 'local_cross_encoder', reranker_state: 'completed', budget: { used: 500 } },
  evidence: [{ evidence_id: 'E1', item: { locator: 'src/example.py', span: { start_line: 2, end_line: 3 }, unit_anchor: 'run', stable_key: 'stable', part_index: 0, content: 'source code' } }],
  generation_decisions: [], model_contract: { provider: 'gemini', model_id: 'gemini-3.8-flash' }, generation_policy: { version: 'v1' }, returned_model: 'gemini-3.8-flash', prompt_version: 'v1', prompt_measurement: { input_tokens: 200 }, usage_input_tokens: 200, usage_output_tokens: 20, total_seconds: 0.1,
}
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (value: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function setup() {
  mocked.mockImplementation(path => {
    if (!path) return Promise.resolve(embedding)
    if (path === '/generation') return Promise.resolve(generation)
    if (path === '/reranker') return Promise.resolve(reranker)
    if (path.endsWith('/embeddings')) return Promise.resolve(embedding)
    return Promise.resolve(result)
  })
}
beforeEach(() => mocked.mockReset())

async function typeAndAsk(question = 'What is shown?') {
  fireEvent.change(await screen.findByLabelText('Ask question'), { target: { value: question } })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Ask' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
}

describe('Ask scope and evidence', () => {
  it('shows configuration guidance without sending Ask', async () => {
    setup()
    mocked.mockImplementation(path => Promise.resolve(path === '/generation' ? { ...generation, configured: false } : path === '/reranker' ? reranker : embedding))
    render(<AskPanel projectId="p" snapshotId="s" invalidation={0} />)
    expect(await screen.findByText('Configure Ask.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Ask question'), { target: { value: 'Question' } })
    expect(screen.getByRole('button', { name: 'Ask' })).toBeDisabled()
  })
  it('renders statement citations and expandable escaped source', async () => {
    setup()
    render(<AskPanel projectId="p" snapshotId="s" invalidation={0} />)
    expect(await screen.findByText(/sent to gemini \(gemini-3\.8-flash\)/)).toBeInTheDocument()
    await typeAndAsk()
    expect(await screen.findByText('Shown.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '[E1]' })).toHaveAttribute('href', '#ask-evidence-request1-E1')
    fireEvent.click(screen.getByText('Source evidence (1)'))
    expect(screen.getByText('source code')).toBeInTheDocument()
  })
  it('discards a late success after edits and invalidation', async () => {
    setup()
    const late = deferred<unknown>()
    mocked.mockImplementation(path => { if (!path) return Promise.resolve(embedding); return path.endsWith('/ask') ? late.promise : Promise.resolve(path === '/generation' ? generation : path === '/reranker' ? reranker : embedding) })
    const view = render(<AskPanel projectId="p" snapshotId="s" invalidation={0} />)
    await typeAndAsk()
    fireEvent.change(screen.getByLabelText('Ask question'), { target: { value: 'Edited question' } })
    view.rerender(<AskPanel projectId="p" snapshotId="s" invalidation={1} />)
    await act(async () => late.resolve(result))
    expect(screen.queryByText('Shown.')).not.toBeInTheDocument()
  })
  it('discards a late failure after a policy edit', async () => {
    const late = deferred<unknown>()
    mocked.mockImplementation(path => {
      if (!path) return Promise.resolve(embedding)
      if (path.endsWith('/ask')) return late.promise
      return Promise.resolve(path === '/generation' ? generation : path === '/reranker' ? reranker : embedding)
    })
    render(<AskPanel projectId="p" snapshotId="s" invalidation={0} />)
    await typeAndAsk()
    fireEvent.change(screen.getByLabelText('Ranking'), { target: { value: 'retrieval_order' } })
    await act(async () => late.reject(new Error('stale provider error')))
    expect(screen.queryByText('stale provider error')).not.toBeInTheDocument()
  })
  it('shows server-owned insufficient-evidence text', async () => {
    mocked.mockImplementation(path => {
      if (!path) return Promise.resolve(embedding)
      if (path.endsWith('/ask')) return Promise.resolve({
        ...result, status: 'insufficient_evidence',
        answer: { text: 'The supplied evidence does not support an answer.', statements: [] },
        evidence: [],
      })
      return Promise.resolve(path === '/generation' ? generation : path === '/reranker' ? reranker : embedding)
    })
    render(<AskPanel projectId="p" snapshotId="s" invalidation={0} />)
    await typeAndAsk()
    expect(await screen.findByText('The supplied evidence does not support an answer.')).toBeInTheDocument()
  })
  it('discards stale authority after generation', async () => {
    setup()
    let reads = 0
    mocked.mockImplementation(path => {
      if (!path) return Promise.resolve(embedding)
      if (path.endsWith('/ask')) return Promise.resolve(result)
      if (path.endsWith('/embeddings')) return Promise.resolve(++reads > 1 ? { ...embedding, active_processing_run_id: 'run2' } : embedding)
      return Promise.resolve(path === '/generation' ? generation : reranker)
    })
    render(<AskPanel projectId="p" snapshotId="s" invalidation={0} />)
    await typeAndAsk()
    expect(await screen.findByText(/Evidence authority changed/)).toBeInTheDocument()
    expect(screen.queryByText('Shown.')).not.toBeInTheDocument()
  })
})
