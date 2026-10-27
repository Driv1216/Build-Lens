import { useEffect, useState } from 'react'
import { getBoundaries, getIndex, listIndexes, type Boundaries, type IndexSummary } from '../../api/lab'
import { BoundaryLanes } from './visuals'

export function IndexInspector() {
  const [indexes, setIndexes] = useState<IndexSummary[]>([])
  const [selected, setSelected] = useState('')
  const [manifest, setManifest] = useState<Record<string, unknown> | null>(null)
  const [locator, setLocator] = useState('')
  const [boundaries, setBoundaries] = useState<Boundaries | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    listIndexes(controller.signal).then((value) => !controller.signal.aborted && setIndexes(value.indexes)).catch(() => undefined)
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!selected) return
    const controller = new AbortController()
    setManifest(null)
    setBoundaries(null)
    getIndex(selected, controller.signal).then((value) => !controller.signal.aborted && setManifest(value)).catch(() => undefined)
    return () => controller.abort()
  }, [selected])

  useEffect(() => {
    if (!selected || !locator) return
    const controller = new AbortController()
    getBoundaries(selected, locator, controller.signal)
      .then((value) => !controller.signal.aborted && setBoundaries(value))
      .catch(() => !controller.signal.aborted && setBoundaries(null))
    return () => controller.abort()
  }, [selected, locator])

  const gateway = (manifest?.gateway ?? {}) as { available_locators?: string[] | null; skipped?: Record<string, number>; root_identity?: Record<string, unknown> }
  const sizes = (boundaries?.framework_spans ?? []).flatMap(([s, e]) => (s === null || e === null ? [] : [e - s]))
  return (
    <section className="card lab-section">
      <h2>Index inspector</h2>
      <label>
        Index
        <select aria-label="Index" value={selected} onChange={(event) => setSelected(event.target.value)}>
          <option value="">Select an index</option>
          {indexes.map((index) => (
            <option key={index.index_id} value={index.index_id}>
              {index.config?.name} · {index.config?.pipeline} · {index.nodes ?? 0} nodes
            </option>
          ))}
        </select>
      </label>
      {manifest && (
        <div className="lab-grid">
          <div className="lab-tile"><strong>Embedding provenance</strong><pre>{JSON.stringify(manifest.embedding, null, 2)}</pre></div>
          <div className="lab-tile"><strong>Reuse verification</strong><pre>{JSON.stringify(manifest.reuse_verification, null, 2)}</pre></div>
          <div className="lab-tile"><strong>Gateway</strong><pre>{JSON.stringify({ skipped: gateway.skipped, root_identity: gateway.root_identity }, null, 2)}</pre></div>
          <div className="lab-tile"><strong>Build</strong><pre>{JSON.stringify({ build_ms: manifest.build_ms, bridge_ms: manifest.bridge_ms, worker_peak_rss: manifest.worker_peak_rss, versions: manifest.framework_versions }, null, 2)}</pre></div>
        </div>
      )}
      {gateway.available_locators && (
        <label>
          Artifact (boundary diff)
          <select aria-label="Artifact" value={locator} onChange={(event) => setLocator(event.target.value)}>
            <option value="">Select an artifact</option>
            {gateway.available_locators.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
      )}
      {boundaries && boundaries.text && (
        <>
          <h3>Native chunks vs framework nodes · {boundaries.locator}</h3>
          <BoundaryLanes length={boundaries.text.length} native={boundaries.native_spans.map(([s, e]) => [s, e])} framework={boundaries.framework_spans.map(([s, e, m]) => [s, e, m])} />
          <p className="note">
            {boundaries.native_spans.length} native chunks · {boundaries.framework_spans.length} framework nodes ·{' '}
            {boundaries.framework_spans.filter(([s]) => s === null).length} unanchored · median node{' '}
            {sizes.length ? sizes.sort((a, b) => a - b)[Math.floor(sizes.length / 2)] : 0} chars
          </p>
        </>
      )}
    </section>
  )
}
