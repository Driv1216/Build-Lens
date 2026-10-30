import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { getArchitecture, type ArchitectureStatus, type ClassSet } from '../../api/architecture'
import { listProjects, type Project } from '../../api/projects'
import { latestSnapshot } from '../../api/snapshots'
import { HierarchyTree } from './HierarchyTree'
import { Inspector } from './Inspector'
import { ApiView } from './ApiView'
import { DatabaseView, FrontendView, IntegrationsView, SubsystemsView } from './StructureViews'
import { CyclesView, DiagnosticsView, MatrixView, NeighbourhoodView, OverviewView } from './Views'
import { describe, label } from './format'

const VIEWS = ['overview', 'modules', 'api', 'integrations', 'database', 'frontend', 'subsystems', 'cycles', 'diagnostics'] as const
type View = (typeof VIEWS)[number]
const CLASS_SETS: { value: ClassSet; text: string }[] = [
  { value: 'S0', text: 'S0 · unconditional only' },
  { value: 'S1', text: 'S1 · + presumed unconditional' },
  { value: 'S2', text: 'S2 · + conditional and elidable (default)' },
  { value: 'S3', text: 'S3 · + type level' },
  { value: 'S4', text: 'S4 · + deferred' },
]

/** Stage 14 architecture workspace: verified, evidence-backed, read-only. */
export function ArchitecturePage() {
  const [params, setParams] = useSearchParams()
  const projectId = params.get('project') ?? ''
  const snapshotParam = params.get('snapshot') ?? ''
  const view = (VIEWS as readonly string[]).includes(params.get('view') ?? '') ? (params.get('view') as View) : 'overview'
  const node = params.get('node') ?? ''
  const container = params.get('container') ?? 'workspace:.'
  const classSet = (params.get('set') as ClassSet | null) ?? 'S2'
  const [projects, setProjects] = useState<Project[]>([])
  const [snapshotId, setSnapshotId] = useState(snapshotParam)
  const [status, setStatus] = useState<ArchitectureStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

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
    setStatus(null); setError(null)
    if (!projectId || !snapshotId) return
    const controller = new AbortController()
    void getArchitecture(projectId, snapshotId, controller.signal)
      .then(value => { if (!controller.signal.aborted) setStatus(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(describe(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId])

  const revision = status?.active_run?.run_id ?? ''
  const ready = status?.freshness === 'fresh'
  const scopeLabel = useMemo(() => projects.find(item => item.id === projectId)?.name ?? '', [projects, projectId])
  const scope = { projectId, snapshotId, revision }
  const select = (key: string) => update({ node: key })

  return <main className="explore-shell">
    <p className="eyebrow">Phase 3 · Stage 14 · Architecture intelligence</p>
    <h1 className="lab-title">Architecture</h1>
    <p className="subtitle">The module dependency graph and API connections projected from the semantic catalog and sanitised literal evidence. Every element is evidence-linked and re-verified on each request; derived, inferred and unknown parts are labelled.</p>
    <div className="lab-scope card">
      <label>Project
        <select aria-label="Architecture project" value={projectId} onChange={event => update({ project: event.target.value, snapshot: '', node: '', container: '' })}>
          {projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>
      <label>Class set
        <select aria-label="Import class set" value={classSet} onChange={event => update({ set: event.target.value })}>
          {CLASS_SETS.map(item => <option key={item.value} value={item.value}>{item.text}</option>)}
        </select>
      </label>
      <p className="note">{scopeLabel} · snapshot <code>{snapshotId || 'none'}</code> · <Link to="/">Back to projects</Link> · <Link to={`/explore?project=${projectId}&snapshot=${snapshotId}`}>Explore</Link></p>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    {status && !ready && <p className="error" role="alert">{status.freshness === 'none'
      ? `No architecture model for this snapshot. ${status.catalog_ready ? 'Build it from the snapshot panel.' : 'Build the semantic catalog first, then the architecture model.'}`
      : `The architecture model is stale (${status.freshness_reasons.join(', ')}). Rebuild it from the snapshot panel.`}</p>}
    {ready && <>
      <nav className="tabs arch-views" role="tablist">{VIEWS.map(item => <button key={item} role="tab" aria-selected={view === item} className={view === item ? 'active' : ''} onClick={() => update({ view: item })}>{item}</button>)}</nav>
      <div className="explore-grid">
        <HierarchyTree {...scope} selected={node} onSelect={select} />
        <section className="explore-column arch-main" aria-label={`${view} view`}>
          {view === 'overview' && <OverviewView {...scope} />}
          {view === 'modules' && <>
            <p className="note breadcrumbs">Matrix of <code>{label(container) || '(repository root)'}</code> {container !== 'workspace:.' && <button className="link-button" onClick={() => update({ container: '' })}>back to root</button>}</p>
            <MatrixView {...scope} parent={container} classSet={classSet} onSelect={select} onOpen={key => update({ container: key })} />
            {node.startsWith('module:') && <NeighbourhoodView {...scope} node={node} classSet={classSet} onSelect={select} />}
          </>}
          {view === 'api' && <ApiView {...scope} onSelect={select} />}
          {view === 'integrations' && <IntegrationsView {...scope} onSelect={select} />}
          {view === 'database' && <DatabaseView {...scope} onSelect={select} />}
          {view === 'frontend' && <FrontendView {...scope} onSelect={select} />}
          {view === 'subsystems' && <SubsystemsView {...scope} onSelect={select} />}
          {view === 'cycles' && <CyclesView {...scope} onSelect={select} />}
          {view === 'diagnostics' && <DiagnosticsView {...scope} onSelect={select} />}
        </section>
        <Inspector {...scope} node={node} classSet={classSet} onSelect={select} />
      </div>
    </>}
  </main>
}
