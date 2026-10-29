import { useCallback, useEffect, useRef, useState } from 'react'
import { describe } from './format'

type Settled<T> = { key: string; value: T | null; error: string | null }

/** One abortable request whose result resets whenever the listed inputs change.
 *
 * The result is stored with the inputs it was requested for and returned only while they are
 * still the current inputs, so a response can never render under a newer selection, not even
 * for the one render between an input change and the effect that starts the next request. */
export function useRequest<T>(load: (signal: AbortSignal) => Promise<T>, deps: unknown[]): { value: T | null; error: string | null } {
  const key = JSON.stringify(deps)
  const [state, setState] = useState<Settled<T>>({ key, value: null, error: null })
  useEffect(() => {
    const controller = new AbortController()
    setState({ key, value: null, error: null })
    void load(controller.signal)
      .then(result => { if (!controller.signal.aborted) setState({ key, value: result, error: null }) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setState({ key, value: null, error: describe(reason) }) })
    return () => controller.abort()
    // The caller lists the inputs that define the request; ``key`` serialises them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  return state.key === key ? { value: state.value, error: state.error } : { value: null, error: null }
}

/** On-demand requests where only the latest may deliver: starting another request, calling
 * ``cancel`` (for example when the selection changes) or unmounting aborts the previous one,
 * and an aborted request never applies its result or error. */
export function useLatestRequest(): {
  run: <T>(load: (signal: AbortSignal) => Promise<T>, apply: (value: T) => void, fail: (reason: unknown) => void) => Promise<void>
  cancel: () => void
} {
  const current = useRef<AbortController | null>(null)
  const cancel = useCallback(() => { current.current?.abort(); current.current = null }, [])
  useEffect(() => cancel, [cancel])
  const run = useCallback(async <T,>(load: (signal: AbortSignal) => Promise<T>, apply: (value: T) => void, fail: (reason: unknown) => void) => {
    current.current?.abort()
    const controller = new AbortController()
    current.current = controller
    try {
      const value = await load(controller.signal)
      if (!controller.signal.aborted) apply(value)
    } catch (reason) {
      if (!controller.signal.aborted) fail(reason)
    }
  }, [])
  return { run, cancel }
}
