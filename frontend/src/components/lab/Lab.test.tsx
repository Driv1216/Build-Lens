import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '../../api/client'
import type { LabResult, LabStatus } from '../../api/lab'
import { ExperimentBuilder } from './ExperimentBuilder'
import { LabOverview } from './LabOverview'
import { LoaderLab } from './LoaderLab'
import { ProbeWorkbench } from './ProbeWorkbench'
import { RunsBrowser } from './RunsBrowser'

vi.mock('../../api/client', async (original) => ({ ...(await original<typeof import('../../api/client')>()), request: vi.fn() }))
const mocked = vi.mocked(request)

const status: LabStatus = {
  contract: 'stage12.lab.v1',
  python: '3.14.6',
  extras: { 'lab-langchain': { 'langchain-core': '1.7.0' } },
  extras_installed: { 'lab-langchain': true },
  isolated_environments: { 'langchain-postgres': false },
  lab_database_configured: false,
  generation: { enabled: false, state: 'offline-only (no external provider calls)' },
  models: {},
  pipelines: [
    { id: 'langchain.vector', family: 'langchain', label: 'LangChain vector', outcome: 'LC-B', corpus_modes: ['native_chunks'], native_chunk_identity: true, available: true, reasons: [], params: { k: { kind: 'int', default: 32, minimum: 1, maximum: 100, choices: [], help: '' } } },
    { id: 'langchain.pg_custom', family: 'langchain', label: 'Custom pg store', outcome: 'LS-1', corpus_modes: ['native_chunks'], native_chunk_identity: true, available: false, reasons: ['BUILDLENS_LAB_DATABASE_URL is not configured'], params: {} },
  ],
}

function item(ref: string, rank: number) {
  return { ref, rank, text_sha256: `h${ref}`, text_bytes: 10, locator: 'a.py', artifact_fingerprint: 'f', span: { start_offset: 0, end_offset: 10 }, anchored: true, chunk_id: ref, score: 0.5, score_kind: 'cosine', detail: { anchor_method: 'native' } }
}

function result(pipeline: string, question: string): LabResult {
  return {
    pipeline,
    config_digest: pipeline,
    question,
    retrieved: [item('c1', 1), item('c2', 2)],
    ranked: [item('c1', 1), item('c2', 2)],
    context: [item('c1', 1)],
    packing: {},
    steps: [{ seq: 1, kind: 'retrieve', name: 'retriever', framework_event: 'on_retriever_end', duration_ms: 2, attributes: {}, item_refs: [], parent: null }],
    answer: { model: 'offline', buildlens_validation: { valid: true, problems: [] } },
    info: {},
    timings: { total_ms: 5, worker_ms: 3, bridge_ms: 1 },
    texts: { hc1: 'def load_user(): pass' },
    verification: { rebound_chunks: 2 },
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => (resolve = r))
  return { promise, resolve }
}

function withScope(element: React.ReactNode) {
  return render(
    <MemoryRouter>
      <Routes>
        <Route element={<Outlet context={{ projectId: 'p', snapshotId: 's' }} />}>
          <Route path="/" element={element} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mocked.mockReset()
})

describe('Stage 12 laboratory UI', () => {
  it('lists pipelines and explains why unavailable ones cannot run', async () => {
    mocked.mockImplementation(async () => status as never)
    withScope(<LabOverview />)
    expect(await screen.findByText('BUILDLENS_LAB_DATABASE_URL is not configured')).toBeInTheDocument()
    expect(screen.getByText(/offline-only/)).toBeInTheDocument()
  })

  it('disables unavailable pipelines and validates configs on the server', async () => {
    mocked.mockImplementation(async (path: string) => {
      if (path === '/lab/status') return status as never
      if (path === '/lab/validate') return { name: 'my-experiment', digest: 'abc123' } as never
      throw new Error(path)
    })
    withScope(<ExperimentBuilder />)
    const option = await screen.findByRole('option', { name: /LS-1 · langchain.pg_custom/ })
    expect(option).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }))
    expect(await screen.findByText(/abc123/)).toBeInTheDocument()
    const [, init] = mocked.mock.calls.find(([path]) => path === '/lab/validate')!
    expect(JSON.parse(String(init?.body)).config.pipeline).toBe('langchain.vector')
  })

  it('renders probe traces side by side and ignores stale responses', async () => {
    const late = deferred<unknown>()
    let calls = 0
    mocked.mockImplementation(async (path: string) => {
      if (path === '/lab/experiments') return { experiments: [{ name: 'one', pipeline: 'native.context', valid: true, digest: 'd1' }, { name: 'two', pipeline: 'langchain.vector', valid: true, digest: 'd2' }] } as never
      if (path.endsWith('/probe')) {
        calls += 1
        if (calls === 1) return late.promise as never
        return { question: 'q2', results: [{ ok: true, result: result('native.context', 'q2') }, { ok: true, result: result('langchain.vector', 'q2') }], overlap: { '0-1': { '1': 1 } } } as never
      }
      throw new Error(path)
    })
    withScope(<ProbeWorkbench />)
    fireEvent.click(await screen.findByLabelText('one'))
    fireEvent.click(screen.getByLabelText('two'))
    fireEvent.change(screen.getByLabelText('Probe question'), { target: { value: 'q1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Probe' }))
    fireEvent.change(screen.getByLabelText('Probe question'), { target: { value: 'q2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Probe' }))
    expect(await screen.findByRole('heading', { name: 'langchain.vector' })).toBeInTheDocument()
    const probeCall = mocked.mock.calls.filter(([path]) => String(path).endsWith('/probe')).at(-1)!
    const sent = JSON.parse(String(probeCall[1]?.body)).configs as Record<string, unknown>[]
    expect(sent.every((config) => !('valid' in config) && !('digest' in config))).toBe(true)
    expect(screen.getAllByRole('img', { name: 'Pipeline step waterfall' })).toHaveLength(2)
    expect(screen.getByRole('img', { name: 'Rank alignment' })).toBeInTheDocument()
    await act(async () => late.resolve({ question: 'q1', results: [{ ok: false, pipeline: 'x', error: { code: 'STALE', message: '' } }], overlap: {} }))
    expect(screen.queryByText(/STALE/)).not.toBeInTheDocument()
  })

  it('labels scoring tiers and shows the matched subset', async () => {
    mocked.mockImplementation(async (path: string) => {
      if (path === '/lab/runs') return { runs: [{ run_id: 'r1', files: ['manifest.json', 'scores.json'] }] } as never
      if (path === '/lab/runs/r1/files/scores.json')
        return {
          version: 'stage12.lab-scores.v1', run_id: 'r1', corpus_digest: 'c', dataset: { id: 'd', version: '4', review_status: 'approved' },
          tiers: { approved: 'a', span: 's', derived: 'd' },
          summary: { means: { native: { 'span:context_coverage': { mean: 0.5, n: 24 } } }, latency: { native: { n: 1, p50_ms: 10, p95_ms: 10, mean_ms: 10 } }, matched: { cases: ['x'], means: { native: { 'span:context_coverage': { mean: 0.25, n: 19 } } } } },
          comparisons: [], builds: {}, counts: { success: 1 }, status: 'completed',
        } as never
      if (path === '/lab/runs/r1/cases') return { cases: [] } as never
      throw new Error(path)
    })
    withScope(<RunsBrowser />)
    await screen.findByRole('option', { name: 'r1' })
    fireEvent.change(screen.getByLabelText('Run'), { target: { value: 'r1' } })
    expect(await screen.findByText('Derived · not human-approved')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('0.2500')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Matched subset'))
    expect(await screen.findByText('0.5000')).toBeInTheDocument()
  })

  it('surfaces loader safety findings', async () => {
    mocked.mockImplementation(async () => ({
      summary: { 'llamaindex.reader_default': { files_loaded: 3, symlink_escape_followed: true, secret_bait_files_loaded: ['notes/token.md'], loaded_but_buildlens_excluded_or_rejected: ['.env'], absolute_paths_exposed: true, errors: [] } },
      rows: [{ path: 'linked/escape.txt', buildlens: { outcome: 'excluded', reasons: ['symlink_not_followed'] }, 'llamaindex.reader_default': { loaded: true, contains_canary: true } }],
    }) as never)
    withScope(<LoaderLab />)
    fireEvent.click(screen.getByRole('button', { name: /Generate sandbox/ }))
    expect(await screen.findByText('symlink escape followed: true')).toBeInTheDocument()
    expect(screen.getByText('loaded · CANARY')).toBeInTheDocument()
  })
})
