import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { expect, it, vi } from 'vitest'
import { request } from '../../api/client'
import { ImpactPage } from './ImpactPage'

vi.mock('../../api/client', async original => ({
  ...(await original<typeof import('../../api/client')>()),
  request: vi.fn(),
}))

it('submits one pinned rule version and the selected source paths for both snapshots', async () => {
  const mocked = vi.mocked(request)
  mocked.mockImplementation(async path => {
    if (path === '/projects') return [{ id: 'owner-project', name: 'Test project' }]
    if (path === '/projects/owner-project/impact/analyses') return {
      status: 'evidence_incomplete', result_digest: 'result',
      changes: [], findings: [], tests: [], verification_gaps: [], frontier: [], gaps: [],
      architecture_rules: { version: 3, digest: 'rules', old_graph: 'old', new_graph: 'new' },
    }
    throw new Error(`Unexpected request ${path}`)
  })
  await act(async () => {
    render(<MemoryRouter initialEntries={['/impact?project=owner-project&snapshot=before']}><ImpactPage /></MemoryRouter>)
  })
  fireEvent.change(screen.getByLabelText('Input type'), { target: { value: 'snapshots' } })
  fireEvent.change(screen.getByLabelText('After snapshot ID'), { target: { value: 'after' } })
  fireEvent.change(screen.getByLabelText('Architecture rule version'), { target: { value: '3' } })
  const analyze = screen.getByRole('button', { name: 'Analyze' })
  expect(analyze).toBeDisabled()
  const form = analyze.closest('form')!
  fireEvent.change(within(form).getByLabelText('Source paths'), { target: { value: 'app/api.py' } })
  fireEvent.click(analyze)
  expect(await screen.findByText(/Architecture rule version 3/)).toBeInTheDocument()
  expect(mocked).toHaveBeenCalledWith(
    '/projects/owner-project/impact/analyses',
    expect.objectContaining({
      body: JSON.stringify({
        mode: 'snapshots', before: { snapshot_id: 'before' }, after: { snapshot_id: 'after' },
        locators: ['app/api.py'], architecture_rule_version: 3, save: false,
      }),
    }),
  )
})

it('submits an evidenced API field hypothesis with a confirmed contract family', async () => {
  const mocked = vi.mocked(request)
  mocked.mockImplementation(async path => {
    if (path === '/projects') return [{ id: 'owner-project', name: 'Test project' }]
    if (path === '/projects/owner-project/impact/analyses') return {
      status: 'evidence_incomplete', result_digest: 'result', changes: [], findings: [], tests: [], verification_gaps: [], frontier: [], gaps: [],
    }
    throw new Error(`Unexpected request ${path}`)
  })
  await act(async () => {
    render(<MemoryRouter initialEntries={['/impact?project=owner-project&snapshot=before']}><ImpactPage /></MemoryRouter>)
  })
  fireEvent.change(screen.getByLabelText('Input type'), { target: { value: 'hypothesis' } })
  fireEvent.change(screen.getByLabelText('Operation'), { target: { value: 'remove_api_field' } })
  fireEvent.change(screen.getByLabelText('Target'), { target: { value: 'items' } })
  fireEvent.change(screen.getByLabelText('Parameter, field or environment key'), { target: { value: 'ready' } })
  const form = screen.getByRole('button', { name: 'Analyze' }).closest('form')!
  fireEvent.change(within(form).getByLabelText('Source paths'), { target: { value: 'api.py' } })
  fireEvent.click(screen.getByLabelText('Confirm this hypothesis'))
  fireEvent.click(screen.getByRole('button', { name: 'Analyze' }))
  await screen.findByText('Impact report')
  expect(mocked).toHaveBeenCalledWith('/projects/owner-project/impact/analyses', expect.objectContaining({
    body: JSON.stringify({ mode: 'hypothesis', before: { snapshot_id: 'before' }, locators: ['api.py'],
      operations: [{ kind: 'remove_api_field', target: 'items', member: 'ready', family: 'response', value: null }], confirm: true, save: false }),
  }))
})

it('shows historical integrity separately from freshness and contract compatibility', async () => {
  const id = 'a'.repeat(64)
  vi.mocked(request).mockImplementation(async path => {
    if (path === '/projects') return [{ id: 'owner-project', name: 'Test project' }]
    if (path.endsWith('?verify=true')) return {
      status: 'qualified', reasons: ['IMPACT_CONTRACT_CHANGED'], integrity: 'verified', freshness: 'stale',
      compatibility: 'changed', evidence_verification: 'complete', analysis_completeness: 'evidence_incomplete',
    }
    if (path.endsWith(id)) return {
      status: 'evidence_incomplete', result_digest: id, changes: [], findings: [], tests: [], verification_gaps: [], frontier: [], gaps: [],
    }
    throw new Error(`Unexpected request ${path}`)
  })
  await act(async () => {
    render(<MemoryRouter initialEntries={['/impact?project=owner-project&snapshot=before']}><ImpactPage /></MemoryRouter>)
  })
  fireEvent.change(screen.getByLabelText('Result digest'), { target: { value: id } })
  fireEvent.click(screen.getByRole('button', { name: 'Open report' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Verify saved evidence' }))
  expect(await screen.findByText('Evidence integrity · verified')).toBeInTheDocument()
  expect(screen.getByText('Current authority · stale')).toBeInTheDocument()
  expect(screen.getByText('Analytical contract · changed')).toBeInTheDocument()
  expect(screen.getByText('Evidence verification · complete')).toBeInTheDocument()
})
