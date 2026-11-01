import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { buildFlow, buildFlowEvidence, getFlow, getFlowEvidence, type FlowEvidenceStatus, type FlowStatus } from '../../api/flow'
import { describeFlow } from './format'

type Props = { projectId: string; snapshotId: string; revision?: number }

export function FlowPanel(props: Props) {
  return <FlowScope key={`${props.projectId}/${props.snapshotId}/${props.revision ?? 0}`} {...props} />
}

/** Stage 15 builds for one snapshot: the flow evidence run (the only Stage 15 source read),
 * then the flow model (PostgreSQL only). Nothing rebuilds implicitly. */
function FlowScope({ projectId, snapshotId }: Props) {
  const [evidence, setEvidence] = useState<FlowEvidenceStatus | null>(null)
  const [model, setModel] = useState<FlowStatus | null>(null)
  const [busy, setBusy] = useState<'evidence' | 'model' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const lifetime = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller
    void Promise.all([getFlowEvidence(projectId, snapshotId, controller.signal), getFlow(projectId, snapshotId, controller.signal)])
      .then(([e, m]) => { if (!controller.signal.aborted) { setEvidence(e); setModel(m) } })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(describeFlow(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId])
  async function run(kind: 'evidence' | 'model') {
    const signal = lifetime.current?.signal
    setBusy(kind); setError(null)
    try {
      if (kind === 'evidence') {
        const value = await buildFlowEvidence(projectId, snapshotId, signal)
        if (!signal?.aborted) { setEvidence(value); setModel(await getFlow(projectId, snapshotId, signal)) }
      } else {
        const value = await buildFlow(projectId, snapshotId, signal)
        if (!signal?.aborted) setModel(value)
      }
    } catch (reason) { if (!signal?.aborted) setError(describeFlow(reason)) }
    finally { if (!signal?.aborted) setBusy(null) }
  }
  const active = model?.active_run
  return <section className="structural-panel" aria-label="Flow model">
    <div className="section-heading"><h3>Flow tracing</h3>
      <span>
        <button disabled={busy !== null || evidence?.structure_available === false} onClick={() => void run('evidence')}>{busy === 'evidence' ? 'Extracting…' : 'Extract flow evidence'}</button>{' '}
        <button disabled={busy !== null || model?.inputs_ready === false} onClick={() => void run('model')}>{busy === 'model' ? 'Building…' : 'Build flow model'}</button>
      </span>
    </div>
    <p className="note">Flow evidence reads source once through the secure reader; the flow model and traces read PostgreSQL only. Every HTTP hop stays inferred until the Stage 14 API suite is human-evaluated.</p>
    {error && <p className="error" role="alert">{error}</p>}
    <p>Flow evidence: {evidence?.active_run ? `#${evidence.active_run.revision} · ${evidence.active_run.outcome} · ${evidence.freshness}` : 'none'}</p>
    <p>Flow model: {active ? `#${active.revision} · ${active.outcome} · ${model?.freshness}` : 'none'}</p>
    {model?.inputs_ready === false && <p className="note">Needs fresh flow evidence, semantic catalog and architecture model.</p>}
    {[...(evidence?.freshness_reasons ?? []), ...(model?.freshness_reasons ?? [])].map(reason => <p key={reason} className="note">{reason}</p>)}
    {active && model?.freshness === 'fresh' && <Link to={`/flows?project=${projectId}&snapshot=${snapshotId}`}>Open flow traces →</Link>}
  </section>
}
