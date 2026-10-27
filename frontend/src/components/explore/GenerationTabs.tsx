import { useEffect, useRef, useState } from 'react'
import {
  createPlan,
  executePlan,
  getEntryHistory,
  getGenerationConfig,
  type Claim,
  type Entry,
  type GenerationConfig,
  type GenerationPlan,
  type GenerationRun,
} from '../../api/semantic'
import { visible } from './visible'

const LABELS: Record<Claim['basis_label'], string> = {
  cited: 'cited',
  per_documentation: 'per documentation',
  from_declarations_only: 'from declarations only',
}

/** AI interpretation: plain-text claims, each with its verified citations (never HTML). */
export function InterpretationTab({ entry }: { entry: Entry | null }) {
  if (!entry) return <p className="note" role="status">Loading interpretation…</p>
  const value = entry.interpretation
  if (entry.state === 'absent' || !value) return <p className="note">No AI interpretation for this entity in the current index. Use the Generate tab to plan one.</p>
  const accepted = value.claims.filter(claim => claim.status === 'cited')
  const conflicts = value.claims.filter(claim => claim.status === 'conflicts_with_fact')
  return <div className="facts">
    <p className="banner" role="note">
      AI interpretation · {value.provider}/{value.returned_model ?? value.requested_model} · {new Date(value.created_at).toLocaleString()} · <strong>{entry.state}</strong>
      {entry.mode && entry.mode !== 'generated' ? ` · ${entry.mode}` : ''}
      {entry.reused_from_profile ? ` from profile ${entry.reused_from_profile.slice(0, 12)}` : ''}
    </p>
    {entry.stale_reasons.length > 0 && <p className="note">Stale: {entry.stale_reasons.join(', ')}</p>}
    {value.integrity_errors.length > 0 && <p className="error" role="alert">Citation integrity errors: {value.integrity_errors.join(', ')}</p>}
    <p className="note">Context coverage: {value.context_coverage ?? 'unknown'} · status {value.status.replaceAll('_', ' ')}. "Cited" means the quotes are real code or facts, not that the claim is true.</p>
    {accepted.map(claim => <ClaimItem key={claim.ordinal} claim={claim} />)}
    {conflicts.length > 0 && <><h3>Conflicts with parsed facts</h3>{conflicts.map(claim => <ClaimItem key={claim.ordinal} claim={claim} />)}</>}
    {value.unknowns.length > 0 && <><h3>Unknowns</h3><ul>{value.unknowns.map(text => <li key={text}>{visible(text)}</li>)}</ul></>}
  </div>
}

function ClaimItem({ claim }: { claim: Claim }) {
  return <article className="fact">
    <p className="item-head">
      <span className="tier tier-constructed">{claim.category.replace('_', ' ')}</span>
      <span className={`tier ${claim.status === 'cited' ? 'tier-approved' : 'tier-derived'}`}>{claim.status === 'cited' ? LABELS[claim.basis_label] : 'conflicts with fact'}</span>
      {claim.assertion_outcome === 'matches_fact' && <span className="tier tier-span">matches parsed fact</span>}
      {claim.scope_warning && <span className="tier tier-derived">scope warning</span>}
    </p>
    <p>{claim.text === null ? 'Withheld: a citation failed read-time verification.' : visible(claim.text)}</p>
    <ul className="relations">{claim.citations.map(citation => <li key={`${citation.evidence_id}-${citation.kind}`}>
      <code>{citation.evidence_id}</code> {citation.locator ?? ''}{citation.start_line ? ` line ${citation.start_line}` : ''} · {citation.basis}
      {citation.quote ? <> · <q>{visible(citation.quote)}</q></> : <span className="error"> · {citation.verification}</span>}
    </li>)}</ul>
  </article>
}

export function HistoryTab({ projectId, snapshotId, entityKey }: { projectId: string; snapshotId: string; entityKey: string }) {
  const [items, setItems] = useState<Entry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setItems(null)
    void getEntryHistory(projectId, snapshotId, entityKey, controller.signal)
      .then(value => { if (!controller.signal.aborted) setItems(value.items) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(String(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId, entityKey])
  if (error) return <p className="error" role="alert">{error}</p>
  if (!items) return <p className="note" role="status">Loading history…</p>
  if (!items.length) return <p className="note">No interpretations exist for this entity in any snapshot.</p>
  return <ul className="relations">{items.map(item => item.interpretation && <li key={item.interpretation.interpretation_id}>
    {new Date(item.interpretation.created_at).toLocaleString()} · {item.interpretation.provider}/{item.interpretation.returned_model ?? item.interpretation.requested_model} · profile <code>{item.interpretation.generator_profile_digest.slice(0, 12)}</code> · <strong>{item.state}</strong>{item.stale_reasons.length ? ` (${item.stale_reasons.join(', ')})` : ''}
  </li>)}</ul>
}

/** Plan first (nothing is sent), review every contributing locator, then authorise. */
export function GenerateTab({ projectId, snapshotId, entityKey, onGenerated }: { projectId: string; snapshotId: string; entityKey: string; onGenerated: () => void }) {
  const [config, setConfig] = useState<GenerationConfig | null>(null)
  const [plan, setPlan] = useState<GenerationPlan | null>(null)
  const [run, setRun] = useState<GenerationRun | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lifetime = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller
    setPlan(null); setRun(null); setConfirmed(false); setError(null)
    void getGenerationConfig(controller.signal)
      .then(value => { if (!controller.signal.aborted) setConfig(value) })
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(String(reason)) })
    return () => controller.abort()
  }, [projectId, snapshotId, entityKey])
  async function preflight() {
    const signal = lifetime.current?.signal
    setBusy(true); setError(null); setRun(null); setConfirmed(false)
    try { const value = await createPlan(projectId, snapshotId, [entityKey], signal); if (!signal?.aborted) setPlan(value) }
    catch (reason) { if (!signal?.aborted) setError(String(reason)) }
    finally { if (!signal?.aborted) setBusy(false) }
  }
  async function authorise() {
    if (!plan) return
    const signal = lifetime.current?.signal
    setBusy(true); setError(null)
    try { const value = await executePlan(projectId, snapshotId, plan, signal); if (!signal?.aborted) { setRun(value); onGenerated() } }
    catch (reason) { if (!signal?.aborted) setError(String(reason)) }
    finally { if (!signal?.aborted) setBusy(false) }
  }
  const item = plan?.items[0]
  return <div className="facts">
    {config && <p className="note">{config.enabled ? 'Enabled' : 'Disabled'} · {config.provider}/{config.model} · {config.sharing_class === 'cloud' ? 'cloud provider' : 'loopback endpoint'} · {config.retention_note}</p>}
    {error && <p className="error" role="alert">{error}</p>}
    <button disabled={busy || !config?.enabled} onClick={() => void preflight()}>{busy && !plan ? 'Planning…' : 'Create plan'}</button>
    {plan && item && <section aria-label="Generation plan">
      <dl className="snapshot-details">
        <dt>Disposition</dt><dd>{item.disposition}{item.reason ? ` · ${item.reason}` : ''}</dd>
        <dt>Context</dt><dd>{item.context_coverage}</dd>
        <dt>Request</dt><dd>{item.request_bytes.toLocaleString()} bytes · <code>{item.request_digest?.slice(0, 16) ?? '—'}</code></dd>
        <dt>Sends</dt><dd>{plan.contributing_locators.map(value => `${value.locator} (${value.role.replace('_', ' ')}, ${value.bytes} B)`).join('; ') || 'nothing'}</dd>
        <dt>Caps</dt><dd>{String(plan.caps.max_outbound_requests)} requests · {String(plan.caps.deadline_seconds)} s</dd>
        <dt>Sharing</dt><dd>{plan.sharing_class} · {plan.retention_note}</dd>
        {item.reuse_interpretation_id && <><dt>Reuse</dt><dd>{item.reuse_provider}/{item.reuse_returned_model} · profile {item.reuse_profile_digest?.slice(0, 12)}</dd></>}
        <dt>Expires</dt><dd>{new Date(plan.expires_at).toLocaleString()}</dd>
        <dt>Plan</dt><dd><code>{plan.plan_digest.slice(0, 16)}</code></dd>
      </dl>
      {item.disposition !== 'ineligible' && !run && <>
        <label className="inline"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> I authorise exactly this plan</label>
        <button disabled={busy || !confirmed} onClick={() => void authorise()}>{busy ? 'Running…' : item.disposition === 'reuse' ? 'Authorise reuse' : 'Authorise and run'}</button>
      </>}
    </section>}
    {run && <p role="status">Run {run.state} · {run.attempts_started} request(s){run.attempts.map(a => ` · ${a.outcome ?? 'pending'}${a.rejection_codes.length ? ` (${a.rejection_codes.join(', ')})` : ''}`).join('')}</p>}
  </div>
}
