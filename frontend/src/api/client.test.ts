import { afterEach, expect, it, vi } from 'vitest'
import { ApiError, request } from './client'
afterEach(() => vi.unstubAllGlobals())
it('retains structured server errors', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'CURRENT_CONTENT_UNAVAILABLE', message: 'Regenerate evidence.' } }), { status: 409 })))
  try { await request('/test'); throw new Error('expected rejection') }
  catch (error) { expect(error).toBeInstanceOf(ApiError); expect(error).toMatchObject({ status: 409, code: 'CURRENT_CONTENT_UNAVAILABLE' }) }
})
