import { useState } from 'react'
import { getExplanationConfig, listExplanations, planExplanation, runExplanation, type ExplanationPlan, type ExplanationRun } from '../../api/flow'
import { useLatestRequest, useRequest } from '../architecture/useRequest'
import { visible } from '../explore/visible'
import { describeFlow, explained } from './format'

type Props = { projectId: string; snapshotId: string; digest: string; pinned: boolean; onSelect: (step: string) => void }

/** Plan-gated explanations of a pinned trace. Planning sends nothing; one confirmed plan sends
 * exactly one request. Claims are AI-generated, validated against the trace, never evidence. */
export function ExplanationPanel({ projectId, snapshotId, digest, pinned, onSelect }: Props) {
  const config = useRequest(signal => explained(getExplanationConfig(projectId, snapshotId, signal)), [projectId, snapshotId])
  const [refresh, setRefresh] = useState(0)
  const runs = useRequest(signal => pinned ? explained(listExplanations(projectId, snapshotId, digest, signal)) : Promise.resolve(null), [projectId, snapshotId, digest, pinned, refresh])
  const [plan, setPlan] = useState<ExplanationPlan | null>(null)
  const [latest, setLatest] = useState<ExplanationRun | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const action = useLatestRequest()
  if (!pinned) return <section aria-label="Explanation"><p className="note">Pin this trace first: explanations are bound to a pinned trace digest.</p></section>
  function makePlan() {
    setError(null); setPlan(null); setBusy(true)
    void action.run(signal => planExplanation(projectId, snapshotId, digest, signal), value => { setPlan(value); setBusy(false) }, reason => { setError(describeFlow(reason)); setBusy(false) })
  }
  function send(value: ExplanationPlan) {
    setError(null); setBusy(true)
    void action.run(signal => runExplanation(projectId, snapshotId, value, signal), run => { setLatest(run); setPlan(null); setBusy(false); setRefresh(x => x + 1) }, reason => { setError(describeFlow(reason)); setBusy(false) })
  }
  const shown = latest ?? runs.value?.items[0] ?? null
  return <section aria-label="Explanation">
    <p className="note">AI-generated text, validated against this pinned trace by <code>flow-validator.v1</code>. A kept claim is consistent with the trace, not a statement of runtime behaviour, and is never used as evidence.</p>
    {config.value && <p className="note">Provider <strong>{config.value.provider}</strong> · model <code>{visible(config.value.model)}</code> · {config.value.retention_note}{config.value.enabled ? '' : ' · generation is disabled, so nothing can be sent'}</p>}
    {config.error && <p className="error" role="alert">{config.error}</p>}
    <button disabled={busy} onClick={makePlan}>Plan an explanation</button>
    {plan && <div className="card explanation-plan">
      <p>Plan <code>{plan.plan_digest.slice(0, 16)}…</code> · one request of {plan.request_bytes.toLocaleString()} bytes to {plan.provider} (<code>{visible(plan.model)}</code>) · expires {new Date(plan.expires_at).toLocaleTimeString()}</p>
      <p className="note">The package holds {plan.package.steps} steps{plan.package.truncated ? ' (truncated)' : ''} and {plan.package.excerpts} verified source excerpts. Nothing has been sent.</p>
      <button disabled={busy || !plan.enabled} onClick={() => send(plan)}>Confirm and send this request</button>
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
    {runs.error && <p className="error" role="alert">{runs.error}</p>}
    {shown && <div className="explanation-result">
      <p><strong>{(shown.outcome ?? shown.state).replaceAll('_', ' ')}</strong> · {shown.claims.length} claim{shown.claims.length === 1 ? '' : 's'} kept · {shown.rejection_codes.length} rejected · {shown.requests_sent} request sent{shown.returned_model ? ` · model ${visible(shown.returned_model)}` : ''}</p>
      <ol>{shown.claims.map(claim => <li key={claim.claim_digest}>
        <span className="badge">AI · {claim.level}</span> {visible(claim.text)}{' '}
        {claim.steps.map(step => <button key={step} className="link-button" onClick={() => onSelect(step)}>{step}</button>)}
        {claim.citations.length > 0 && <span className="note"> · quotes {claim.citations.map(c => c.id).join(', ')}</span>}
      </li>)}</ol>
      {shown.unknowns.length > 0 && <><h3>Stated unknowns</h3><ul>{shown.unknowns.map(text => <li key={text}>{visible(text)}</li>)}</ul></>}
      {shown.rejection_codes.length > 0 && <details><summary>Rejected claims ({shown.rejection_codes.length})</summary><ul>{shown.rejection_codes.map(code => <li key={code}><code>{code}</code></li>)}</ul></details>}
    </div>}
  </section>
}
