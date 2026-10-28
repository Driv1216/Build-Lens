import { useEffect, useMemo, useState } from 'react'
import {
  getCycles, getDiagnostics, getMatrix, getNeighbourhood, getOverview,
  type ClassSet, type Cycles, type DiagnosticPage, type Matrix, type Neighbourhood, type Overview,
} from '../../api/architecture'
import { visible } from '../explore/visible'
import { layered } from './layout'
import { ClassChip, TierBadge } from './parts'
import { describe, label } from './format'

type Scope = { projectId: string; snapshotId: string; revision: string }

function useRequest<T>(load: (signal: AbortSignal) => Promise<T>, deps: unknown[]): { value: T | null; error: string | null } {
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

function Status({ error }: { error: string | null }) {
  return error ? <p className="error" role="alert">{error}</p> : <p className="note" role="status">Verifying…</p>
}

export function OverviewView({ projectId, snapshotId, revision }: Scope) {
  const { value, error } = useRequest<Overview>(signal => getOverview(projectId, snapshotId, signal), [projectId, snapshotId, revision])
  if (!value) return <Status error={error} />
  const coverage = value.coverage as { modules?: Record<string, number>; alias?: { statements: number; prefixes: Record<string, number> }; statements?: { by_resolution: Record<string, number>; by_class: Record<string, number> } }
  return <div className="arch-overview">
    <p className="banner">Every count below was re-verified against its persisted rows and evidence for this request.</p>
    <h3>Workspaces</h3>
    <ul className="arch-cards">{value.workspaces.map(item => <li key={item.key} className="arch-card">
      <strong>{label(item.key) || '(repository root)'}</strong>
      <span className="note">{(item.attributes.manifests as string[] | undefined)?.join(', ') || 'repository root'}</span>
      <span className="note">efferent {item.metrics.efferent_modules ?? 0} · afferent {item.metrics.afferent_modules ?? 0}</span>
    </li>)}</ul>
    <h3>Elements</h3>
    <dl className="snapshot-details">{Object.entries(value.node_counts).map(([kind, count]) => <div key={kind}><dt>{kind}</dt><dd>{count}</dd></div>)}</dl>
    <h3>Import statements</h3>
    <dl className="snapshot-details">
      {Object.entries(coverage.statements?.by_resolution ?? {}).map(([key, count]) => <div key={key}><dt>{key}</dt><dd>{count}</dd></div>)}
      {Object.entries(coverage.statements?.by_class ?? {}).map(([key, count]) => <div key={key}><dt><ClassChip value={key} /></dt><dd>{count}</dd></div>)}
    </dl>
    <h3>Cycles and diagnostics</h3>
    <p className="note">{Object.entries(value.component_counts).map(([key, count]) => `${key}: ${count}`).join(' · ') || 'No cycles in any class set.'}</p>
    <p className="note">{Object.entries(value.diagnostic_counts).map(([key, count]) => `${key}: ${count}`).join(' · ')}</p>
    {(coverage.alias?.statements ?? 0) > 0 && <p className="note">Alias imports are not resolved (tsconfig is never read): {Object.entries(coverage.alias?.prefixes ?? {}).map(([key, count]) => `${key} ×${count}`).join(', ')}</p>}
  </div>
}

export function MatrixView({ projectId, snapshotId, revision, parent, classSet, onSelect, onOpen }: Scope & { parent: string; classSet: ClassSet; onSelect: (key: string) => void; onOpen: (key: string) => void }) {
  const { value, error } = useRequest<Matrix>(signal => getMatrix(projectId, snapshotId, parent, classSet, signal), [projectId, snapshotId, revision, parent, classSet])
  const cells = useMemo(() => new Map((value?.cells ?? []).map(cell => [`${cell.source}|${cell.target}`, cell])), [value])
  const cyclic = useMemo(() => new Set((value?.cyclic_pairs ?? []).flatMap(([a, b]) => [`${a}|${b}`, `${b}|${a}`])), [value])
  if (!value) return <Status error={error} />
  if (value.children.length === 0) return <p className="note">No children.</p>
  return <div className="matrix-wrap">
    <p className="note">Dependency structure matrix of <strong>{label(value.parent.key) || '(repository root)'}</strong>: row depends on column (class set {classSet}). Cells count imported names; highlighted pairs depend on each other.</p>
    <table className="dsm">
      <thead><tr><th />{value.children.map((child, index) => <th key={child.key} title={label(child.key)} scope="col">{index + 1}</th>)}</tr></thead>
      <tbody>{value.children.map((row, index) => <tr key={row.key}>
        <th scope="row">
          <span className="note">{index + 1}</span>{' '}
          <button className="link-button" onClick={() => onSelect(row.key)}>{label(row.key).split('/').pop()}</button>
          {(row.kind === 'container' || row.kind === 'workspace') && <button className="tree-toggle" aria-label={`Open ${row.name}`} onClick={() => onOpen(row.key)}>↘</button>}
        </th>
        {value.children.map(column => {
          const cell = cells.get(`${row.key}|${column.key}`)
          return <td key={column.key} className={`${row.key === column.key ? 'diagonal' : ''}${cell ? ' filled' : ''}${cell && cyclic.has(`${row.key}|${column.key}`) ? ' cyclic' : ''}`} title={cell ? `${cell.count} name(s) via ${cell.edges} module edge(s)` : ''}>{cell ? cell.count : ''}</td>
        })}
      </tr>)}</tbody>
    </table>
  </div>
}

export function NeighbourhoodView({ projectId, snapshotId, revision, node, classSet, onSelect }: Scope & { node: string; classSet: ClassSet; onSelect: (key: string) => void }) {
  const { value, error } = useRequest<Neighbourhood>(signal => getNeighbourhood(projectId, snapshotId, node, classSet, 1, signal), [projectId, snapshotId, revision, node, classSet])
  const layout = useMemo(() => value ? layered(value.nodes.map(item => item.key), value.edges.map(edge => ({ source: edge.source_key, target: edge.target_key ?? '' }))) : null, [value])
  if (!value || !layout) return <Status error={error} />
  const at = new Map(layout.nodes.map(item => [item.key, item]))
  return <figure className="neighbourhood">
    <figcaption className="note">Direct imports and importers of <strong>{label(node)}</strong> ({value.nodes.length} of {value.total_nodes} modules{value.truncated ? ', truncated for display' : ''}). Arrows point from importer to imported module.</figcaption>
    <svg role="img" aria-label={`Dependency neighbourhood of ${label(node)}`} viewBox={`0 0 ${layout.width} ${layout.height}`} width={layout.width} height={layout.height}>
      <defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" /></marker></defs>
      {value.edges.map(edge => {
        const a = at.get(edge.source_key); const b = at.get(edge.target_key ?? '')
        if (!a || !b) return null
        return <line key={edge.key} className={`arch-edge class-${edge.import_class}`} x1={a.x + 180} y1={a.y + 14} x2={b.x} y2={b.y + 14} markerEnd="url(#arrow)" />
      })}
      {layout.nodes.map(item => <g key={item.key} transform={`translate(${item.x},${item.y})`} className={item.key === node ? 'arch-node origin' : 'arch-node'} onClick={() => onSelect(item.key)}>
        <rect width="180" height="28" rx="6" />
        <text x="8" y="18">{label(item.key).split('/').pop()?.slice(0, 26)}</text>
        <title>{label(item.key)}</title>
      </g>)}
    </svg>
    <ul className="note sr-list">{value.edges.map(edge => <li key={edge.key}>{label(edge.source_key)} imports {label(edge.target_key ?? '')} ({edge.import_class})</li>)}</ul>
  </figure>
}

export function CyclesView({ projectId, snapshotId, revision, onSelect }: Scope & { onSelect: (key: string) => void }) {
  const { value, error } = useRequest<Cycles>(signal => getCycles(projectId, snapshotId, signal), [projectId, snapshotId, revision])
  if (!value) return <Status error={error} />
  if (value.items.length === 0) return <p className="note">The static import relation has no cycles in any class set.</p>
  return <div className="cycles">
    <p className="note">A cycle here means the static import relation, restricted to the class set shown, contains a cycle. It does not claim a runtime circular-import failure. Aggregate cycles between folders may exist without any module-level cycle.</p>
    {value.items.map(item => <article key={item.key} className="fact">
      <p><span className="kind">{item.level === 'module' ? 'import cycle' : 'aggregate dependency cycle'}</span> · class set {item.class_set} · {item.members.length} members
        {item.level === 'container' && <span className="note"> · {item.attributes.module_cycle_underlying ? 'a module-level cycle underlies it' : 'no import cycle among modules'}</span>}</p>
      <ol className="witness">{item.witness.map(key => {
        const edge = value.edges[key]
        return edge ? <li key={key}><button className="link-button" onClick={() => onSelect(edge.source_key)}>{label(edge.source_key)}</button> → {label(edge.target_key ?? '')} <ClassChip value={edge.import_class} /> <TierBadge tier={edge.tier} /></li> : null
      })}</ol>
    </article>)}
  </div>
}

export function DiagnosticsView({ projectId, snapshotId, revision, onSelect }: Scope & { onSelect: (key: string) => void }) {
  const { value, error } = useRequest<DiagnosticPage>(signal => getDiagnostics(projectId, snapshotId, 0, signal), [projectId, snapshotId, revision])
  const [code, setCode] = useState('')
  if (!value) return <Status error={error} />
  const items = value.items.filter(item => !code || item.code === code)
  return <div className="diagnostics">
    <label>Code <select aria-label="Diagnostic code" value={code} onChange={event => setCode(event.target.value)}>
      <option value="">all ({value.total})</option>
      {Object.entries(value.counts).map(([key, count]) => <option key={key} value={key}>{key} ({count})</option>)}
    </select></label>
    <p className="note">Diagnostics describe what the evidence shows; they are not verdicts.</p>
    <ul className="edge-list">{items.map(item => <li key={item.ordinal} className="edge-line">
      <TierBadge tier={item.tier} /> <span className="kind">{item.code}</span>{' '}
      {item.subjects.slice(0, 4).map(key => <button key={key} className="link-button" onClick={() => onSelect(key)}>{label(key)}</button>)}
      {item.subjects.length > 4 && <span className="note"> +{item.subjects.length - 4}</span>}
      {Array.isArray(item.attributes.targets) && <span className="note"> {visible((item.attributes.targets as string[]).join(', '))}</span>}
      {item.attributes.class_set ? <span className="note"> · class set {String(item.attributes.class_set)}</span> : null}
    </li>)}</ul>
    {value.total > value.items.length && <p className="note">Showing the first {value.items.length} of {value.total}.</p>}
  </div>
}
