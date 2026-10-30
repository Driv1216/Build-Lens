import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { buildArchitecture, getArchitecture, type ArchitectureStatus } from '../../api/architecture'
import { describe } from './format'

type Props = { projectId: string; snapshotId: string; revision?: number }

export function ArchitecturePanel(props: Props) {
  return <ArchitectureScope key={`${props.projectId}/${props.snapshotId}/${props.revision ?? 0}`} {...props} />
}

function ArchitectureScope({ projectId, snapshotId }: Props) {
  const [status, setStatus] = useState<ArchitectureStatus | null>(null)
  const [building, setBuilding] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lifetime = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller
    void getArchitecture(projectId, snapshotId, controller.signal)
      .then(value => { if (!controller.signal.aborted) setStatus(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(describe(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId])
  async function build() {
    const signal = lifetime.current?.signal
    setBuilding(true); setError(null)
    try {
      const value = await buildArchitecture(projectId, snapshotId, signal)
      if (!signal?.aborted) setStatus(value)
    } catch (reason) { if (!signal?.aborted) setError(describe(reason)) }
    finally { if (!signal?.aborted) setBuilding(false) }
  }
  const active = status?.active_run
  return <section className="structural-panel" aria-label="Architecture model">
    <div className="section-heading"><h3>Architecture model</h3><button disabled={building || status?.catalog_ready === false} onClick={() => void build()}>{building ? 'Building…' : 'Build architecture'}</button></div>
    <p className="note">Projected from the active semantic catalog in PostgreSQL: no source reads, no AI text.</p>
    {status?.catalog_ready === false && <p className="note">Build a current semantic catalog first.</p>}
    {error && <p className="error" role="alert">{error}</p>}
    <p>Latest attempt: {status?.latest_attempt ? `#${status.latest_attempt.revision} · ${status.latest_attempt.outcome}${status.latest_attempt.superseded ? ' · superseded' : ''}${status.latest_attempt.diagnostics.length ? ` · ${status.latest_attempt.diagnostics.join(', ')}` : ''}` : 'none'}</p>
    <p>Active model: {active ? `#${active.revision} · ${active.outcome} · ${status?.freshness}` : 'none'}</p>
    {status?.freshness_reasons.map(reason => <p key={reason} className="note">{reason}</p>)}
    {active && status?.freshness === 'fresh' && <Link to={`/architecture?project=${projectId}&snapshot=${snapshotId}`}>Open the architecture workspace →</Link>}
  </section>
}
