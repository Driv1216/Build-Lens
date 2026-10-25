import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { listProjects, type Project } from '../../api/projects'
import { latestSnapshot } from '../../api/snapshots'
import { getEntity, listEntities, type EntityDetail, type EntityFilters, type EntityPage } from '../../api/semantic'
import { EntityList } from './EntityList'
import { EntityTabs } from './EntityTabs'
import { SourceView } from './SourceView'

/** Read-only view over the deterministic catalog (Stage 13 Milestone B). */
export function ExplorePage() {
  const [params, setParams] = useSearchParams()
  const projectId = params.get('project') ?? ''
  const snapshotParam = params.get('snapshot') ?? ''
  const entityKey = params.get('entity') ?? ''
  const [projects, setProjects] = useState<Project[]>([])
  const [snapshotId, setSnapshotId] = useState(snapshotParam)
  const [filters, setFilters] = useState<EntityFilters>({})
  const [page, setPage] = useState<EntityPage | null>(null)
  const [detail, setDetail] = useState<EntityDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const detailRequest = useRef<AbortController | null>(null)

  function update(next: Record<string, string>) {
    const value = new URLSearchParams(params)
    for (const [key, item] of Object.entries(next)) {
      if (item) value.set(key, item)
      else value.delete(key)
    }
    setParams(value)
  }

  useEffect(() => {
    const controller = new AbortController()
    void listProjects().then(items => {
      if (controller.signal.aborted) return
      setProjects(items)
      if (!projectId && items[0]) update({ project: items[0].id })
    }).catch(() => { if (!controller.signal.aborted) setError('Projects are unavailable.') })
    return () => controller.abort()
    // Only once: later project changes come from the selector.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!projectId) return
    if (snapshotParam) { setSnapshotId(snapshotParam); return }
    const controller = new AbortController()
    setSnapshotId('')
    void latestSnapshot(projectId, controller.signal)
      .then(value => { if (!controller.signal.aborted) setSnapshotId(value.id) })
      .catch(() => { if (!controller.signal.aborted) setSnapshotId('') })
    return () => controller.abort()
  }, [projectId, snapshotParam])

  useEffect(() => {
    if (!projectId || !snapshotId) return
    const controller = new AbortController()
    setPage(null); setError(null)
    void listEntities(projectId, snapshotId, filters, 0, controller.signal)
      .then(value => { if (!controller.signal.aborted) setPage(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(describe(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId, filters])

  useEffect(() => {
    detailRequest.current?.abort()
    setDetail(null)
    if (!projectId || !snapshotId || !entityKey) return
    const controller = new AbortController(); detailRequest.current = controller
    void getEntity(projectId, snapshotId, entityKey, controller.signal)
      .then(value => { if (!controller.signal.aborted) setDetail(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(describe(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId, entityKey])

  async function more() {
    if (!page || !projectId || !snapshotId) return
    const next = await listEntities(projectId, snapshotId, filters, page.offset + page.items.length)
    setPage(current => current && current.run_id === next.run_id ? { ...next, items: [...current.items, ...next.items] } : current)
  }

  const scopeLabel = useMemo(() => projects.find(item => item.id === projectId)?.name ?? '', [projects, projectId])
  return <main className="explore-shell">
    <p className="eyebrow">Phase 2 · Stage 13 · Semantic catalog</p>
    <h1 className="lab-title">Explore</h1>
    <p className="subtitle">Deterministic entities, parsed facts and labelled pattern matches, each cited to the structural rows it was rendered from. No AI interpretation is shown or stored here.</p>
    <div className="lab-scope card">
      <label>Project
        <select aria-label="Explore project" value={projectId} onChange={event => update({ project: event.target.value, snapshot: '', entity: '' })}>
          {projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>
      <p className="note">{scopeLabel} · snapshot <code>{snapshotId || 'none'}</code> · <Link to="/">Back to projects</Link></p>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="explore-grid">
      <EntityList page={page} filters={filters} selected={entityKey} onFilters={setFilters} onSelect={key => update({ entity: key })} onMore={() => void more()} />
      <SourceView detail={detail} />
      <EntityTabs detail={detail} onSelect={key => update({ entity: key })} />
    </div>
  </main>
}

function describe(reason: unknown): string {
  if (reason instanceof ApiError && reason.code === 'SEMANTIC_CATALOG_REQUIRED') return 'No catalog yet: build it from the snapshot panel.'
  if (reason instanceof ApiError && reason.code === 'SEMANTIC_CATALOG_STALE') return 'The catalog is stale: rebuild it from the snapshot panel.'
  return reason instanceof Error ? reason.message : String(reason)
}
