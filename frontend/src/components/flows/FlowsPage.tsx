import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import { getFlow, getPinned, getTrace, listEntries, pinTrace, type EntryPage, type FlowStatus, type PinnedTrace } from '../../api/flow'
import { listProjects, type Project } from '../../api/projects'
import { latestSnapshot } from '../../api/snapshots'
import { TierBadge } from '../architecture/parts'
import { useLatestRequest, useRequest } from '../architecture/useRequest'
import { describeFlow, explained, shown, STATUS_TEXT } from './format'
import { ExplanationPanel } from './ExplanationPanel'
import { StepInspector } from './StepInspector'
import { BranchPanel, LaneDiagram, ResponsePanel, TraceOutline } from './TraceViews'

const VIEWS = ['outline', 'lanes', 'responses', 'branches', 'explanation'] as const
type View = (typeof VIEWS)[number]
const KINDS = ['http_endpoint', 'dom_event', 'effect', 'cli_command', 'asyncio_run', 'module'] as const
type Scope = { projectId: string; snapshotId: string; revision: string }

/** Stage 15 flow traces: verified, bounded and evidence-linked; structure is never execution. */
export function FlowsPage() {
  const [params, setParams] = useSearchParams()
  const projectId = params.get('project') ?? ''
  const snapshotParam = params.get('snapshot') ?? ''
  const entry = params.get('entry') ?? ''
  const direction = params.get('direction') === 'backward' ? 'backward' : 'forward'
  const depth = Math.min(64, Math.max(1, Number(params.get('depth') ?? '24') || 24))
  const view = (VIEWS as readonly string[]).includes(params.get('view') ?? '') ? (params.get('view') as View) : 'outline'
  const step = params.get('step') ?? ''
  const hideRefuted = params.get('refuted') === 'hide'
  const [projects, setProjects] = useState<Project[]>([])
  const [snapshotId, setSnapshotId] = useState(snapshotParam)
  const [status, setStatus] = useState<FlowStatus | null>(null)
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
    void getFlow(projectId, snapshotId, controller.signal)
      .then(value => { if (!controller.signal.aborted) setStatus(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(describeFlow(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId])

  const revision = status?.active_run?.run_id ?? ''
  const ready = status?.freshness === 'fresh'
  const scopeLabel = useMemo(() => projects.find(item => item.id === projectId)?.name ?? '', [projects, projectId])
  const scope: Scope = { projectId, snapshotId, revision }

  return <main className="explore-shell">
    <p className="eyebrow">Phase 3 · Stage 15 · Flow tracing</p>
    <h1 className="lab-title">Flow traces</h1>
    <p className="subtitle">Bounded walks over the verified flow model. Each step keeps four verdicts apart: how the call resolves (L1), static reachability (L2), the structural path condition (L3, not an execution) and feasibility in a sound fragment (L4). Every element is re-verified on each request.</p>
    <div className="lab-scope card">
      <label>Project
        <select aria-label="Flow project" value={projectId} onChange={event => update({ project: event.target.value, snapshot: '', entry: '', step: '' })}>
          {projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>
      <p className="note">{scopeLabel} · snapshot <code>{snapshotId || 'none'}</code> · <Link to="/">Back to projects</Link> · <Link to={`/architecture?project=${projectId}&snapshot=${snapshotId}`}>Architecture</Link> · <Link to={`/impact?project=${projectId}&snapshot=${snapshotId}&entity=${entry}`}>Impact</Link></p>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    {status && !ready && <p className="error" role="alert">{status.freshness === 'none'
      ? `No flow model for this snapshot. ${status.inputs_ready ? 'Build it from the snapshot panel.' : 'Extract flow evidence and build the semantic catalog and architecture model first.'}`
      : `The flow model is stale (${status.freshness_reasons.join(', ')}). Rebuild it from the snapshot panel.`}</p>}
    {ready && <div className="explore-grid">
      <EntryPanel key={`${projectId}/${snapshotId}/${revision}`} {...scope} selected={entry} onSelect={key => update({ entry: key, step: '' })} />
      <section className="explore-column arch-main" aria-label="Trace">
        {!entry && <p className="note">Choose an entry point. Search proposes candidates; nothing is selected for you.</p>}
        {entry && <TraceArea {...scope} entry={entry} direction={direction} depth={depth} view={view} step={step} hideRefuted={hideRefuted} update={update} />}
      </section>
    </div>}
  </main>
}

function EntryPanel({ projectId, snapshotId, revision, selected, onSelect }: Scope & { selected: string; onSelect: (key: string) => void }) {
  const [kind, setKind] = useState<string>('http_endpoint')
  const [text, setText] = useState('')
  const [mode, setMode] = useState('lexical')
  const [explicit, setExplicit] = useState('')
  const [found, setFound] = useState<EntryPage | null>(null)
  const [searchError, setSearchError] = useState<string | null>(null)
  const search = useLatestRequest()
  const listed = useRequest(signal => explained(listEntries(projectId, snapshotId, { kind }, signal)), [projectId, snapshotId, revision, kind])
  function submit(event: FormEvent) {
    event.preventDefault()
    if (!text.trim()) return
    setFound(null); setSearchError(null)
    void search.run(signal => listEntries(projectId, snapshotId, { q: text.trim(), mode }, signal), setFound, reason => setSearchError(describeFlow(reason)))
  }
  return <aside className="explore-column" aria-label="Entry points">
    <h2>Entry points</h2>
    <form onSubmit={submit} className="entry-search">
      <label>Describe a flow <input value={text} onChange={event => setText(event.target.value)} placeholder="what happens after login" maxLength={500} /></label>
      <label>Search mode <select value={mode} onChange={event => setMode(event.target.value)}><option value="lexical">lexical</option><option value="hybrid">hybrid (needs embeddings)</option></select></label>
      <button type="submit">Find candidates</button>
    </form>
    {searchError && <p className="error" role="alert">{searchError}</p>}
    {found && <div className="entry-candidates">
      <p className="note">Mode used: <strong>{found.mode}</strong> · {found.total} candidate{found.total === 1 ? '' : 's'} · you choose; nothing is selected automatically.</p>
      <ol>{found.items.map(item => <li key={item.key}>
        <button className="link-button" onClick={() => onSelect(item.key)}>{shown(item.label)}</button> <span className="kind">{item.kind}</span>
        {item.reason && <span className="note"> · search hit #{item.reason.hit_rank} in <code>{shown(item.reason.chunk_locator)}</code>, {item.reason.distance} hop{item.reason.distance === 1 ? '' : 's'} from the entry</span>}
      </li>)}</ol>
    </div>}
    <label>Kind <select aria-label="Entry kind" value={kind} onChange={event => setKind(event.target.value)}>{KINDS.map(item => <option key={item} value={item}>{item.replace('_', ' ')}</option>)}</select></label>
    {listed.error && <p className="error" role="alert">{listed.error}</p>}
    {listed.value && <ul className="entry-list">{listed.value.items.map(item => <li key={item.key} className={selected === item.key ? 'selected' : ''}>
      <button className="link-button" onClick={() => onSelect(item.key)}>{shown(item.label)}</button>
      {item.tier && <> <TierBadge tier={item.tier} /></>} <span className="note">{shown(item.locator)}</span>
    </li>)}</ul>}
    <form onSubmit={event => { event.preventDefault(); if (explicit.trim()) onSelect(explicit.trim()) }}>
      <label>Or a node key <input value={explicit} onChange={event => setExplicit(event.target.value)} placeholder="scope:app/api.py#m.2" /></label>
      <button type="submit">Trace from key</button>
    </form>
  </aside>
}

function TraceArea({ projectId, snapshotId, revision, entry, direction, depth, view, step, hideRefuted, update }: Scope & {
  entry: string; direction: 'forward' | 'backward'; depth: number; view: View; step: string; hideRefuted: boolean
  update: (next: Record<string, string>) => void
}) {
  const trace = useRequest(signal => explained(getTrace(projectId, snapshotId, entry, direction, depth, signal)), [projectId, snapshotId, revision, entry, direction, depth])
  const value = trace.value
  return <>
    <div className="trace-controls">
      <label>Direction <select aria-label="Trace direction" value={direction} onChange={event => update({ direction: event.target.value, step: '' })}><option value="forward">forward</option><option value="backward">backward</option></select></label>
      <label>Depth bound <input type="number" min={1} max={64} value={depth} onChange={event => update({ depth: event.target.value, step: '' })} /></label>
      <label><input type="checkbox" checked={hideRefuted} onChange={event => update({ refuted: event.target.checked ? 'hide' : '' })} /> Hide refuted steps</label>
    </div>
    {trace.error && <p className="error" role="alert">{trace.error}</p>}
    {!value && !trace.error && <p className="note" role="status">Computing and verifying the trace…</p>}
    {value && <>
      <div className="trace-summary">
        <p><strong>{STATUS_TEXT[value.status] ?? value.status}</strong> · {value.hops.length} hops · {value.steps.length} steps · weakest tier <TierBadge tier={value.path_tier} /> · {value.verification}</p>
        {value.caveats.map(caveat => <p key={caveat} className="badge warn">{caveat === 'HTTP_LINK_INDEPENDENT_EVALUATION_PENDING' ? 'Every HTTP hop is inferred: the Stage 14 API matching has not been independently evaluated.' : shown(caveat)}</p>)}
        {value.gaps.length > 0 && <details className="gaps"><summary>Why this is not a complete claim ({value.gaps.length})</summary>
          <ul>{value.gaps.slice(0, 200).map((gap, index) => <li key={index}>{gap.kind ?? 'gap'}{gap.reasons?.length ? `: ${gap.reasons.map(shown).join(', ')}` : ''}{gap.step && <> · <button className="link-button" onClick={() => update({ step: gap.step ?? '' })}>{gap.step}</button></>}</li>)}</ul>
        </details>}
        {value.absence.map(claim => <p key={claim.hop} className="note">Callers of {claim.hop}: {claim.verdict === 'no_callers_in_model' ? 'none in the model (coverage complete, no dynamic callsites)' : `not determinable (${claim.reasons.join(', ')})`}</p>)}
        <PinControl projectId={projectId} snapshotId={snapshotId} entry={entry} direction={direction} depth={depth} digest={value.trace_digest} />
      </div>
      <nav className="tabs" role="tablist">{VIEWS.map(item => <button key={item} role="tab" aria-selected={view === item} className={view === item ? 'active' : ''} onClick={() => update({ view: item })}>{item}</button>)}</nav>
      <div className="trace-body">
        <div>
          {view === 'outline' && <TraceOutline trace={value} selected={step} onSelect={id => update({ step: id })} hideRefuted={hideRefuted} />}
          {view === 'lanes' && <LaneDiagram trace={value} selected={step} onSelect={id => update({ step: id })} />}
          {view === 'responses' && <ResponsePanel trace={value} selected={step} onSelect={id => update({ step: id })} />}
          {view === 'branches' && <BranchPanel trace={value} selected={step} onSelect={id => update({ step: id })} />}
          {view === 'explanation' && <PinnedExplanation projectId={projectId} snapshotId={snapshotId} digest={value.trace_digest} onSelect={id => update({ step: id })} />}
        </div>
        <StepInspector key={`${value.trace_digest}/${step}`} projectId={projectId} snapshotId={snapshotId} trace={value} step={step} />
      </div>
    </>}
  </>
}

function PinnedExplanation({ projectId, snapshotId, digest, onSelect }: { projectId: string; snapshotId: string; digest: string; onSelect: (step: string) => void }) {
  const pinned = useRequest(signal => getPinned(projectId, snapshotId, digest, signal).then(value => value.status === 'current').catch(() => false), [projectId, snapshotId, digest])
  if (pinned.value === null) return <p className="note" role="status">Checking whether this trace is pinned…</p>
  return <ExplanationPanel projectId={projectId} snapshotId={snapshotId} digest={digest} pinned={pinned.value} onSelect={onSelect} />
}

function PinControl({ projectId, snapshotId, entry, direction, depth, digest }: { projectId: string; snapshotId: string; entry: string; direction: 'forward' | 'backward'; depth: number; digest: string }) {
  const [pinned, setPinned] = useState<{ digest: string; check: PinnedTrace | null } | null>(null)
  const [pinError, setPinError] = useState<string | null>(null)
  const latest = useLatestRequest()
  const { cancel } = latest
  useEffect(() => { cancel(); setPinned(null); setPinError(null) }, [digest, cancel])
  const current = pinned?.digest === digest ? pinned : null
  return <p className="note pin-control">
    Trace digest <code>{digest.slice(0, 16)}…</code>{' '}
    {!current && <button onClick={() => { setPinError(null); void latest.run(signal => pinTrace(projectId, snapshotId, entry, direction, depth, signal), value => setPinned({ digest: value.trace_digest, check: null }), reason => setPinError(describeFlow(reason))) }}>Pin trace</button>}
    {current && <> · pinned <button onClick={() => void latest.run(signal => getPinned(projectId, snapshotId, digest, signal), check => setPinned({ digest, check }), reason => setPinError(describeFlow(reason)))}>Re-verify</button></>}
    {current?.check && <span className={`badge${current.check.status === 'current' ? '' : ' warn'}`}> {current.check.status}{current.check.freshness_reasons.length ? ` (${current.check.freshness_reasons.join(', ')})` : ''}</span>}
    {pinError && <span className="error" role="alert"> {pinError}</span>}
  </p>
}
