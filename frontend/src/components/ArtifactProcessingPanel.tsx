import { useEffect, useRef, useState } from 'react'
import { request } from '../api/client'
import type { ArtifactOutcome, Chunk, ProcessingRun, ProcessingStatus } from '../api/processing'
import { EvidencePage } from './EvidencePage'

type Props = { projectId: string; snapshotId: string; revision?: number; onRefresh?: () => void; onAskInvalidate?: () => void }
export function ArtifactProcessingPanel(props: Props) {
  return <ProcessingScope key={`${props.projectId}/${props.snapshotId}/${props.revision ?? 0}`} {...props} />
}
function ProcessingScope({ projectId, snapshotId, onRefresh, onAskInvalidate }: Props) {
  const base = `/projects/${projectId}/snapshots/${snapshotId}/processing`
  const [status, setStatus] = useState<ProcessingStatus | null>(null)
  const [history, setHistory] = useState<ProcessingRun | null>(null)
  const [selected, setSelected] = useState<Chunk | null>(null)
  const [revision, setRevision] = useState(0)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lifetime = useRef<AbortController | null>(null)
  const contentRequest = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller
    setSelected(null); setHistory(null)
    void request<ProcessingStatus>(base, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setStatus(value)
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(String(reason)) })
    return () => { controller.abort(); contentRequest.current?.abort() }
  }, [base, revision])
  async function run() {
    onAskInvalidate?.()
    const signal = lifetime.current?.signal
    setRunning(true); setError(null); setSelected(null); contentRequest.current?.abort()
    try {
      await request(base, { method: 'POST', body: '{}', signal })
      if (!signal?.aborted) { setRevision(x => x + 1); onRefresh?.() }
    } catch (reason) { if (!signal?.aborted) setError(String(reason)) }
    finally { onAskInvalidate?.(); if (!signal?.aborted) setRunning(false) }
  }
  function choose(run: ProcessingRun | null) { contentRequest.current?.abort(); setSelected(null); setHistory(run); setError(null) }
  async function show(item: Chunk) {
    contentRequest.current?.abort()
    const controller = new AbortController(); contentRequest.current = controller
    setSelected(null)
    try {
      const value = await request<Chunk>(`${base}/chunks/${item.id}${history ? `?run_id=${history.run_id}` : ''}`, { signal: controller.signal })
      if (!controller.signal.aborted) setSelected(value)
    } catch (reason) { if (!controller.signal.aborted) setError(String(reason)) }
  }
  const runId = history?.run_id
  const contentAvailable = history ? !history.quarantined : status?.active_freshness === 'fresh'
  return <section className="structural-panel"><div className="section-heading"><h3>Artifact processing</h3><button disabled={running} onClick={() => void run()}>{running ? 'Processing…' : 'Process artifacts'}</button></div>
    {error && <p className="error" role="alert">{error}</p>}
    <p>Latest attempt: {status?.latest_attempt ? `#${status.latest_attempt.revision} · ${status.latest_attempt.outcome}${status.latest_attempt.quarantined ? ' · quarantined' : ''}${status.latest_attempt.superseded ? ' · superseded' : ''}` : 'none'}</p>
    <p>Usable evidence: {status?.active_run ? `#${status.active_run.revision} · ${status.active_freshness}` : 'none'}</p>
    {status?.freshness_reasons?.map(reason => <p key={reason} className="note">{reason}</p>)}
    <EvidencePage<ProcessingRun> key={`history-${revision}`} title="Processing history" path={`${base}/runs`} render={run => <button className="link-button" onClick={() => choose(run)}>#{run.revision} · {run.outcome} {run.quarantined ? '· quarantined' : ''} {run.superseded ? '· superseded' : ''}</button>} />
    {history && <p>Historical run #{history.revision} · <code>{history.run_id}</code> <button onClick={() => choose(null)}>Use current evidence</button></p>}
    {(history || status?.latest_attempt) && <EvidencePage<ArtifactOutcome> key={`outcomes-${revision}`} title="Processing outcomes" path={`${base}/artifacts${runId ? `?run_id=${runId}` : ''}`} render={item => <><code>{item.locator}</code> · {item.outcome} · {item.chunk_count} chunks · {item.execution_mode}{item.diagnostics.map(d => <span key={d.code}> · {d.code}</span>)}</>} />}
    {contentAvailable && <EvidencePage<Chunk> key={`chunks-${revision}`} title="Chunks" path={`${base}/chunks${runId ? `?run_id=${runId}` : ''}`} render={item => <button className="link-button" onClick={() => void show(item)}><code>{item.locator}</code> · {item.kind} · lines {item.span.start_line}–{item.span.end_line}</button>} />}
    {history?.quarantined && <p>Quarantined content is unavailable. Regenerate evidence explicitly.</p>}
    {selected && <details open><summary>Chunk provenance</summary><p><code>{selected.locator}</code> · run <code>{selected.run_id}</code></p><p><code>{selected.stable_key}</code></p><pre>{selected.content}</pre></details>}
  </section>
}
