import { useEffect, useState, type ReactNode } from 'react'
import { request } from '../api/client'

export type Page<T> = { items: T[]; total: number; limit: number; offset: number }

/** A query is mounted under its complete project/snapshot/run scope. */
export function EvidencePage<T extends { id?: string; run_id?: string; analysis_id?: string }>({ path, title, render }: { path: string; title: string; render: (item: T) => ReactNode }) {
  return <ScopedPage<T> key={path} path={path} title={title} render={render} />
}
function ScopedPage<T extends { id?: string; run_id?: string; analysis_id?: string }>({ path, title, render }: { path: string; title: string; render: (item: T) => ReactNode }) {
  const [offset, setOffset] = useState(0)
  const [page, setPage] = useState<Page<T> | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setPage(null); setError(null)
    const separator = path.includes('?') ? '&' : '?'
    void request<Page<T>>(`${path}${separator}limit=50&offset=${offset}`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setPage(value)
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Could not load evidence.')
    })
    return () => controller.abort()
  }, [path, offset])
  return <details open><summary>{title}{page ? ` (${page.total})` : ''}</summary>
    {error && <p role="alert" className="error">{error}</p>}
    {!page && !error && <p role="status">Loading {title.toLowerCase()}…</p>}
    {page && <><ul>{page.items.map((item, index) => <li key={item.id ?? item.run_id ?? item.analysis_id ?? index}>{render(item)}</li>)}</ul>
      <nav className="pager" aria-label={`${title} pages`}><button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - page.limit))}>Previous</button><span>{page.total ? offset + 1 : 0}–{Math.min(offset + page.items.length, page.total)} of {page.total}</span><button disabled={offset + page.limit >= page.total} onClick={() => setOffset(offset + page.limit)}>Next</button></nav></>}
  </details>
}
