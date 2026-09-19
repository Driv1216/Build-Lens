import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('BuildLens projects screen', () => {
  it('loads projects from the API and creates a new project', async () => {
    const existing = {
      id: '11111111-1111-1111-1111-111111111111',
      name: 'Existing',
      root_path: '/work/existing',
      created_at: '2026-09-10T00:00:00Z',
      updated_at: '2026-09-10T00:00:00Z',
    }
    const created = { ...existing, id: '22222222-2222-2222-2222-222222222222', name: 'New project', root_path: '/work/new' }
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([existing]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(created), { status: 201 }))
    vi.stubGlobal('fetch', fetchMock)

    render(<App />)
    expect(await screen.findByText('Existing')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'New project' } })
    fireEvent.change(screen.getByLabelText('Root path'), { target: { value: '/work/new' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add project' }))

    expect(await screen.findByText('New project')).toBeInTheDocument()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(fetchMock.mock.calls[1][0]).toContain('/projects')
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'POST' })
  })

  it('shows a usable error when the project API is unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Network unavailable')))

    render(<App />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Network unavailable')
  })
})
