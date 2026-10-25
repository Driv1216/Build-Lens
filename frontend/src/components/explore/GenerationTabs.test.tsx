import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '../../api/client'
import type { Entry } from '../../api/semantic'
import { GenerateTab, InterpretationTab } from './GenerationTabs'

vi.mock('../../api/client', async (original) => ({ ...(await original<typeof import('../../api/client')>()), request: vi.fn() }))
const mocked = vi.mocked(request)
beforeEach(() => { mocked.mockReset() })

const citation = { evidence_id: 'E1', kind: 'chunk' as const, basis: 'code', locator: 'app/api.py', start_line: 4, end_line: 4, verification: 'verified', quote: 'value = fetch_user(user_id)' }
const entry: Entry = {
  entity_key: 'symbol:1', state: 'current', stale_reasons: [], mode: 'reused', reused_from_profile: 'abcdef1234567890',
  interpretation: {
    interpretation_id: 'i1', provider: 'local', requested_model: 'stub', returned_model: 'stub-1', generator_profile_digest: 'p'.repeat(64),
    status: 'accepted_with_conflicts', created_at: '2026-10-25T10:00:00Z', context_coverage: 'full', unknowns: ['Callers are not shown.'], integrity_errors: ['claim2:CITATION_MISMATCH'],
    claims: [
      { ordinal: 0, category: 'behavior', text: '<b>load</b> calls `fetch_user`.', status: 'cited', basis_label: 'cited', assertion: { kind: 'calls', target: 'fetch_user' }, assertion_outcome: 'matches_fact', scope_warning: true, citations: [citation] },
      { ordinal: 1, category: 'purpose', text: '`load` is async.', status: 'conflicts_with_fact', basis_label: 'from_declarations_only', assertion: { kind: 'is_async' }, assertion_outcome: 'conflicts_with_fact', scope_warning: false, citations: [citation] },
      { ordinal: 2, category: 'purpose', text: null, status: 'cited', basis_label: 'per_documentation', assertion: null, assertion_outcome: null, scope_warning: false, citations: [{ ...citation, verification: 'CITATION_MISMATCH', quote: null }] },
    ],
  },
}

describe('InterpretationTab', () => {
  it('labels provenance, chips, conflicts, unknowns and withholds unverifiable claims as plain text', () => {
    render(<InterpretationTab entry={entry} />)
    expect(screen.getByRole('note')).toHaveTextContent('AI interpretation · local/stub-1')
    expect(screen.getByRole('note')).toHaveTextContent('reused from profile abcdef123456')
    expect(screen.getByText('<b>load</b> calls `fetch_user`.')).toBeInTheDocument()
    expect(screen.getByText('matches parsed fact')).toBeInTheDocument()
    expect(screen.getByText('scope warning')).toBeInTheDocument()
    expect(screen.getByText('Conflicts with parsed facts')).toBeInTheDocument()
    expect(screen.getByText('Callers are not shown.')).toBeInTheDocument()
    expect(screen.getByText(/Withheld: a citation failed/)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('CITATION_MISMATCH')
  })
  it('explains an absent entry', () => {
    render(<InterpretationTab entry={{ ...entry, state: 'absent', interpretation: null }} />)
    expect(screen.getByText(/No AI interpretation/)).toBeInTheDocument()
  })
})

describe('GenerateTab', () => {
  it('plans first, shows every locator, and sends only after explicit confirmation', async () => {
    const plan = {
      plan_id: 'plan1', plan_digest: 'd'.repeat(64), issued_at: '2026-10-25T10:00:00Z', expires_at: '2026-10-25T10:30:00Z', mode: 'generate',
      provider_config: {}, sharing_class: 'loopback', retention_note: 'Loopback endpoint on this machine.', caps: { max_outbound_requests: 2, deadline_seconds: 300 }, totals: {},
      contributing_locators: [{ locator: 'app/api.py', role: 'own_source', bytes: 400 }, { locator: 'app/db.py', role: 'neighbor_facts', bytes: 58 }],
      items: [{ ordinal: 0, entity_key: 'symbol:1', disposition: 'generate', reason: null, context_coverage: 'full', request_digest: 'r'.repeat(64), request_bytes: 3800, contributing_locators: [], reuse_interpretation_id: null, reuse_profile_digest: null, reuse_provider: null, reuse_returned_model: null }],
      run_id: null,
    }
    mocked.mockImplementation((path, init) => {
      if (path === '/semantic-generation') return Promise.resolve({ enabled: true, provider: 'local', model: 'stub', sharing_class: 'loopback', retention_note: 'Loopback endpoint on this machine.', generator_profile_digest: 'p', status: {}, caps: {} })
      if (path.endsWith('/generation/plan')) return Promise.resolve(plan)
      if (path.endsWith('/generation') && init?.method === 'POST') return Promise.resolve({ run_id: 'r1', state: 'complete', attempts_started: 1, totals: {}, diagnostics: [], attempts: [{ attempt_id: 'a', entity_key: 'symbol:1', attempt_no: 1, outcome: 'accepted', returned_model: 'stub-1', rejection_codes: [], late_receipts: 0 }] })
      return Promise.reject(new Error(path))
    })
    const generated = vi.fn()
    render(<GenerateTab projectId="p" snapshotId="s" entityKey="symbol:1" onGenerated={generated} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create plan' }))
    expect(await screen.findByText(/app\/db.py \(neighbor facts, 58 B\)/)).toBeInTheDocument()
    const run = screen.getByRole('button', { name: 'Authorise and run' })
    expect(run).toBeDisabled()
    expect(mocked).not.toHaveBeenCalledWith(expect.stringMatching(/\/generation$/), expect.anything())
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(run)
    expect(await screen.findByRole('status')).toHaveTextContent('Run complete · 1 request(s) · accepted')
    await waitFor(() => expect(generated).toHaveBeenCalledOnce())
    const body = JSON.parse(String(mocked.mock.calls.find(([path]) => path.endsWith('/generation'))?.[1]?.body))
    expect(body).toEqual({ plan_id: 'plan1', plan_digest: 'd'.repeat(64), confirm: true })
  })
})
