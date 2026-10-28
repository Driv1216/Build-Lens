import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import {
  getDependencies, getEvidence, getNode, getPath,
  type ClassSet, type Dependencies, type Evidence, type EvidenceLink, type NodeDetail, type PathResult,
} from '../../api/architecture'
import { visible } from '../explore/visible'
import { ClassChip, EdgeLine, Gaps, TierBadge } from './parts'
import { describe, label } from './format'

type Props = { projectId: string; snapshotId: string; revision: string; node: string; classSet: ClassSet; onSelect: (key: string) => void }

export function Inspector({ projectId, snapshotId, revision, node, classSet, onSelect }: Props) {
  const [detail, setDetail] = useState<NodeDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    setDetail(null); setError(null)
    if (!node) return
    const controller = new AbortController()
    void getNode(projectId, snapshotId, node, controller.signal)
      .then(value => { if (!controller.signal.aborted) setDetail(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(describe(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId, revision, node])
  if (!node) return <section className="explore-column" aria-label="Inspector"><p className="note">Select a folder, package or module.</p></section>
  return <section className="explore-column" aria-label="Inspector">
    {error && <p className="error" role="alert">{error}</p>}
    {!detail && !error && <p className="note" role="status">Verifying…</p>}
    {detail && <>
      <p className="note breadcrumbs">{[...detail.chain].reverse().map(item => <button key={item.key} className="link-button" onClick={() => onSelect(item.key)}>{item.name}</button>)}</p>
      <h2>{visible(detail.node.name)}</h2>
      <p><span className="kind">{detail.node.kind}</span> {detail.node.language && <span className="note">{detail.node.language}</span>} · coverage <strong>{detail.node.coverage_state.replace('_', ' ')}</strong></p>
      {detail.node.catalog_entity_key && <Link to={`/explore?project=${projectId}&snapshot=${snapshotId}&entity=${encodeURIComponent(detail.node.catalog_entity_key)}`}>Open in Explore →</Link>}
      <dl className="snapshot-details">{Object.entries(detail.node.metrics).map(([key, value]) => <div key={key}><dt>{key.replace('_', ' ')}</dt><dd>{value ?? '—'}</dd></div>)}</dl>
      {detail.node.kind === 'module' && typeof detail.node.attributes.literals === 'string' && <p className="note">Literal pass: {detail.node.attributes.literals.replace('_', ' ')}</p>}
      {detail.node.kind !== 'module' && Object.keys(detail.node.attributes).length > 0 && <details className="evidence">
        <summary>Attributes</summary>
        <pre className="attributes">{visible(JSON.stringify(detail.node.attributes, null, 1))}</pre>
      </details>}
      <EvidenceList projectId={projectId} snapshotId={snapshotId} links={detail.node.evidence} />
      <h3>Outgoing ({detail.outgoing_total})</h3>
      <ul className="edge-list">{detail.outgoing.map(edge => <EdgeLine key={edge.key} edge={edge} onSelect={onSelect} />)}</ul>
      <h3>Incoming ({detail.incoming_total})</h3>
      <ul className="edge-list">{detail.incoming.map(edge => <EdgeLine key={edge.key} edge={edge} onSelect={onSelect} direction="in" />)}</ul>
      {detail.components.length > 0 && <><h3>Cycles</h3><ul className="edge-list">{detail.components.map(item => <li key={item.key}>{item.level === 'module' ? 'import cycle' : 'aggregate cycle'} · {item.members.length} members · class set {item.class_set}</li>)}</ul></>}
      {detail.diagnostics.length > 0 && <><h3>Diagnostics</h3><ul className="edge-list">{detail.diagnostics.map(item => <li key={item.ordinal}><TierBadge tier={item.tier} /> {item.code}</li>)}</ul></>}
      {['module', 'container', 'workspace'].includes(detail.node.kind) && <Traversal projectId={projectId} snapshotId={snapshotId} node={detail.node.key} classSet={classSet} module={detail.node.kind === 'module'} onSelect={onSelect} />}
    </>}
  </section>
}

function EvidenceList({ projectId, snapshotId, links }: { projectId: string; snapshotId: string; links: EvidenceLink[] }) {
  const [open, setOpen] = useState<Evidence | null>(null)
  const [error, setError] = useState<string | null>(null)
  async function show(digest: string) {
    setError(null)
    try { setOpen(await getEvidence(projectId, snapshotId, digest)) } catch (reason) { setError(describe(reason)) }
  }
  return <details className="evidence">
    <summary>Evidence ({links.length})</summary>
    <ul className="edge-list">{links.map(link => <li key={link.digest}>
      {link.source === 'architecture'
        ? <button className="link-button" onClick={() => void show(link.digest)}>{link.digest.slice(0, 12)}</button>
        : <span className="note">catalog fact {link.digest.slice(0, 12)}</span>}
    </li>)}</ul>
    {error && <p className="error" role="alert">{error}</p>}
    {open && <div className="fact">
      <p><span className="badge">{open.verification}</span> <span className="note">{open.label}</span></p>
      {open.rendered_text ? <p>{visible(open.rendered_text)}</p> : <p className="note">Text withheld: verification failed.</p>}
      <ul className="note">{open.source_refs.map((ref, index) => <li key={index}>{String(ref.table)} · {visible(String(ref.locator))}</li>)}</ul>
    </div>}
  </details>
}

function Traversal({ projectId, snapshotId, node, classSet, module, onSelect }: { projectId: string; snapshotId: string; node: string; classSet: ClassSet; module: boolean; onSelect: (key: string) => void }) {
  const [deps, setDeps] = useState<Dependencies | null>(null)
  const [target, setTarget] = useState('')
  const [path, setPath] = useState<PathResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { setDeps(null); setPath(null); setError(null) }, [node, classSet])
  async function load(direction: 'out' | 'in') {
    setError(null)
    try { setDeps(await getDependencies(projectId, snapshotId, node, direction, classSet)) } catch (reason) { setError(describe(reason)) }
  }
  async function find() {
    setError(null)
    try { setPath(await getPath(projectId, snapshotId, node, target.startsWith('module:') ? target : `module:${target}`, classSet)) } catch (reason) { setError(describe(reason)) }
  }
  return <div className="traversal">
    <h3>Transitive relationships <span className="note">(class set {classSet})</span></h3>
    <div className="tabs"><button onClick={() => void load('out')}>Depends on</button><button onClick={() => void load('in')}>Depended on by</button></div>
    {error && <p className="error" role="alert">{error}</p>}
    {deps && <>
      <p className="note">{deps.reached.length} modules · search {deps.search}{deps.frontier ? ` · frontier ${deps.frontier}` : ''}</p>
      <ul className="edge-list">{deps.reached.slice(0, 200).map(item => <li key={item.node}>
        <span className="note">{item.depth}</span> <button className="link-button" onClick={() => onSelect(item.node)}>{label(item.node)}</button>
        {item.path.length > 1 && <details><summary className="note">witness ({item.path.length} hops)</summary><ol>{item.path.map(key => <li key={key}>{label(deps.edges[key]?.source_key ?? '')} → {label(deps.edges[key]?.target_key ?? '')} <ClassChip value={deps.edges[key]?.import_class ?? null} /></li>)}</ol></details>}
      </li>)}</ul>
      <Gaps gaps={deps.gaps} />
    </>}
    {module && <form className="path-form" onSubmit={event => { event.preventDefault(); void find() }}>
      <label>Path to module <input value={target} onChange={event => setTarget(event.target.value)} placeholder="app/db.py" /></label>
      <button type="submit" disabled={!target}>Find path</button>
    </form>}
    {path && <div>
      <p><strong>{path.status.replaceAll('_', ' ')}</strong></p>
      {path.path.length > 0 && <ol>{path.path.map(edge => <li key={edge.key}>{label(edge.source_key)} → {label(edge.target_key ?? '')} <ClassChip value={edge.import_class} /> <TierBadge tier={edge.tier} /></li>)}</ol>}
      {path.status !== 'found' && <Gaps gaps={path.gaps} />}
    </div>}
  </div>
}
