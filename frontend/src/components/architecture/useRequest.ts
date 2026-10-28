import { useEffect, useState } from 'react'
import { describe } from './format'

/** One abortable request whose result resets whenever the listed inputs change. */
export function useRequest<T>(load: (signal: AbortSignal) => Promise<T>, deps: unknown[]): { value: T | null; error: string | null } {
  const [value, setValue] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setValue(null); setError(null)
    void load(controller.signal)
      .then(result => { if (!controller.signal.aborted) setValue(result) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(describe(reason)) })
    return () => controller.abort()
    // The caller lists the inputs that define the request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return { value, error }
}
