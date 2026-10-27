import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { buildCatalog, getCatalog, type CatalogStatus } from '../api/semantic'

type Props = { projectId: string; snapshotId: string; revision?: number }

export function SemanticCatalogPanel(props: Props) {
  return <CatalogScope key={`${props.projectId}/${props.snapshotId}/${props.revision ?? 0}`} {...props} />
}

function CatalogScope({ projectId, snapshotId }: Props) {
  const [status, setStatus] = useState<CatalogStatus | null>(null)
  const [building, setBuilding] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lifetime = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller
    void getCatalog(projectId, snapshotId, controller.signal)
      .then(value => { if (!controller.signal.aborted) setStatus(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(String(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId])
  async function build() {
    const signal = lifetime.current?.signal
    setBuilding(true); setError(null)
    try {
      const value = await buildCatalog(projectId, snapshotId, signal)
      if (!signal?.aborted) setStatus(value)
    } catch (reason) { if (!signal?.aborted) setError(String(reason)) }
    finally { if (!signal?.aborted) setBuilding(false) }
  }
  const active = status?.active_run
  const counts = (active?.coverage.entity_kinds ?? {}) as Record<string, number>
  return <section className="structural-panel" aria-label="Semantic catalog">
    <div className="section-heading"><h3>Semantic catalog</h3><button disabled={building} onClick={() => void build()}>{building ? 'Building…' : 'Build catalog'}</button></div>
    <p className="note">Deterministic facts from stored structure and chunks only: no source reads and no AI text.</p>
    {error && <p className="error" role="alert">{error}</p>}
    <p>Latest attempt: {status?.latest_attempt ? `#${status.latest_attempt.revision} · ${status.latest_attempt.outcome}${status.latest_attempt.superseded ? ' · superseded' : ''}` : 'none'}</p>
    <p>Active catalog: {active ? `#${active.revision} · ${status?.freshness}` : 'none'}</p>
    {status?.freshness_reasons.map(reason => <p key={reason} className="note">{reason}</p>)}
    {active && <p className="note">{Object.entries(counts).map(([kind, count]) => `${kind}: ${count}`).join(' · ')}</p>}
    {active && status?.freshness === 'fresh' && <Link to={`/explore?project=${projectId}&snapshot=${snapshotId}`}>Explore the catalog →</Link>}
  </section>
}
