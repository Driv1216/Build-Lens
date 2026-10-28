import { useEffect, useState } from 'react'
import { getTree, type ArchNode } from '../../api/architecture'
import { visible } from '../explore/visible'
import { describe, label } from './format'

type Props = { projectId: string; snapshotId: string; selected: string; onSelect: (key: string) => void; revision: string }

/** Folders & packages (containment only; never presented as subsystems). Lazily expanded. */
export function HierarchyTree(props: Props) {
  return <section className="explore-column" aria-label="Folders and packages">
    <h2>Folders &amp; packages</h2>
    <p className="note">Containment from file locations and manifests. This is navigation, not a subsystem model.</p>
    <Level {...props} parent={null} depth={0} />
  </section>
}

function Level({ projectId, snapshotId, selected, onSelect, revision, parent, depth }: Props & { parent: string | null; depth: number }) {
  const [children, setChildren] = useState<ArchNode[] | null>(null)
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setChildren(null); setError(null)
    void getTree(projectId, snapshotId, parent, controller.signal)
      .then(value => { if (!controller.signal.aborted) { setChildren(value.children); setCounts(value.diagnostic_counts) } })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(describe(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId, parent, revision])
  if (error) return <p className="error" role="alert">{error}</p>
  if (!children) return <p className="note" role="status">Loading…</p>
  return <ul className="arch-tree" style={{ paddingLeft: depth ? 12 : 0 }}>
    {children.map(child => {
      const expandable = child.kind === 'workspace' || child.kind === 'container'
      return <li key={child.key}>
        {expandable && <button className="tree-toggle" aria-label={`${open[child.key] ? 'Collapse' : 'Expand'} ${child.name}`} aria-expanded={!!open[child.key]} onClick={() => setOpen(current => ({ ...current, [child.key]: !current[child.key] }))}>{open[child.key] ? '▾' : '▸'}</button>}
        <button className={`link-button entity${selected === child.key ? ' selected' : ''}`} onClick={() => onSelect(child.key)}>
          <span className="kind">{child.kind === 'workspace' ? 'workspace' : child.kind === 'container' ? (child.attributes.python_package ? 'package' : 'folder') : child.language ?? 'file'}</span> {child.parent_key === null ? label(child.key) : visible(child.name)}
        </button>
        {child.coverage_state !== 'analysed' && child.coverage_state !== 'derived' && <span className="badge warn"> {child.coverage_state.replace('_', ' ')}</span>}
        {counts[child.key] ? <span className="note"> · {counts[child.key]} diagnostic{counts[child.key] === 1 ? '' : 's'}</span> : null}
        {expandable && open[child.key] && <Level projectId={projectId} snapshotId={snapshotId} selected={selected} onSelect={onSelect} revision={revision} parent={child.key} depth={depth + 1} />}
      </li>
    })}
  </ul>
}
