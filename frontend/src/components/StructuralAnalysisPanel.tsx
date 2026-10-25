import { useEffect, useRef, useState } from 'react'
import { request } from '../api/client'
import type { ArtifactAnalysis, Extraction, ImportReference, Structure, Symbol, SymbolDetail } from '../api/structure'
import { EvidencePage } from './EvidencePage'

type Props = { projectId: string; snapshotId: string; revision?: number; onRefresh?: () => void; onAskInvalidate?: () => void }
type Status = { latest_attempt: Structure | null; active_run: Structure | null }
// Each additive pass carries its own authority; a stale contract is never shown as current.
function extraction(item: Extraction) {
  return <span key={item.extractor}> · {item.extractor} ({item.outcome}, {item.execution_mode}{item.contract_current ? '' : ', contract changed — not current'})</span>
}
function importText(item: ImportReference) {
  if (item.form === 'from') return `from ${'.'.repeat(item.level)}${item.from_module ?? ''} import ${item.name}${item.alias ? ` as ${item.alias}` : ''}`
  if (item.form === 'import') return `import ${item.name}${item.alias ? ` as ${item.alias}` : ''}`
  const binding = item.name === '' ? '(side effect)' : item.name === '*' ? `* as ${item.alias ?? '—'}` : `${item.name}${item.alias ? ` as ${item.alias}` : ''}`
  return `${item.form.replace(/_/g, ' ')} ${binding} from '${item.from_module ?? ''}'`
}
export function StructuralAnalysisPanel(props: Props) {
  return <StructureScope key={`${props.projectId}/${props.snapshotId}/${props.revision ?? 0}`} {...props} />
}
function StructureScope({ projectId, snapshotId, onRefresh, onAskInvalidate }: Props) {
  const base = `/projects/${projectId}/snapshots/${snapshotId}/structure`
  const [status, setStatus] = useState<Status | null>(null)
  const [history, setHistory] = useState<Structure | null>(null)
  const [detail, setDetail] = useState<SymbolDetail | null>(null)
  const [revision, setRevision] = useState(0)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lifetime = useRef<AbortController | null>(null)
  const contentRequest = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller
    setDetail(null); setHistory(null)
    void request<Status>(`${base}/status`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setStatus(value)
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(String(reason)) })
    return () => { controller.abort(); contentRequest.current?.abort() }
  }, [base, revision])
  async function run() {
    onAskInvalidate?.()
    const signal = lifetime.current?.signal
    setRunning(true); setError(null); setDetail(null); contentRequest.current?.abort()
    try {
      await request(base, { method: 'POST', body: '{}', signal })
      if (!signal?.aborted) { setRevision(x => x + 1); onRefresh?.() }
    } catch (reason) { if (!signal?.aborted) setError(String(reason)) }
    finally { onAskInvalidate?.(); if (!signal?.aborted) setRunning(false) }
  }
  function choose(run: Structure | null) { contentRequest.current?.abort(); setDetail(null); setHistory(run); setError(null) }
  async function show(item: Symbol) {
    contentRequest.current?.abort()
    const controller = new AbortController(); contentRequest.current = controller
    setDetail(null)
    try {
      const value = await request<SymbolDetail>(`${base}/symbols/${item.id}${history ? `?run_id=${history.analysis_id}` : ''}`, { signal: controller.signal })
      if (!controller.signal.aborted) setDetail(value)
    } catch (reason) { if (!controller.signal.aborted) setError(String(reason)) }
  }
  const analysis = history ?? status?.active_run
  const query = history ? `?run_id=${history.analysis_id}` : ''
  return <section className="structural-panel"><div className="section-heading"><h3>Source structure</h3><button disabled={running} onClick={() => void run()}>{running ? 'Analysing…' : 'Analyse source files'}</button></div>
    {error && <p className="error" role="alert">{error}</p>}
    <p className="note">Parse-only declarations for Python, TypeScript/TSX and JavaScript/JSX. Nothing is executed or resolved.</p>
    <p>Latest attempt: {status?.latest_attempt ? `#${status.latest_attempt.revision} · ${status.latest_attempt.outcome}${status.latest_attempt.quarantined ? ' · quarantined' : ''}${status.latest_attempt.superseded ? ' · superseded' : ''}` : 'none'}</p>
    <p>Usable structural evidence: {status?.active_run ? `#${status.active_run.revision} · ${status.active_run.outcome}` : 'none'}</p>
    <EvidencePage<Structure> key={`history-${revision}`} title="Structural history" path={`${base}/runs`} render={run => <button className="link-button" onClick={() => choose(run)}>#{run.revision} · {run.outcome} {run.quarantined ? '· quarantined' : ''} {run.superseded ? '· superseded' : ''}</button>} />
    {history && <p>Historical run #{history.revision} · <code>{history.analysis_id}</code> <button onClick={() => choose(null)}>Use current structure</button></p>}
    {analysis && <><p>{analysis.summary.modules} modules · {analysis.summary.symbols} symbols · {analysis.summary.imports} imports · {analysis.reuse_count} reused · {analysis.reparse_count} reread</p><p className="note">{analysis.observation_note}</p></>}
    {analysis?.quarantined && <p>Quarantined source evidence is unavailable. Regenerate explicitly.</p>}
    {analysis && !analysis.quarantined && <>
      <EvidencePage<ArtifactAnalysis> key={`artifacts-${revision}`} title="Structural outcomes" path={`${base}/artifacts${query}`} render={item => <><code>{item.locator}</code>{item.language ? ` · ${item.language}` : ''} · {item.outcome} · {item.execution_mode} · {item.symbol_count} symbols · {item.import_count} imports{item.diagnostics.map((d, index) => <span key={`${d.code}-${index}`}> · {d.code}</span>)}{item.extractions?.map(extraction)}</>} />
      <EvidencePage<Symbol> key={`symbols-${revision}`} title="Declarations" path={`${base}/symbols${query}`} render={item => <button className="link-button" onClick={() => void show(item)}><code>{item.locator}</code> · {item.kind} {item.qualified_name}{item.declaration_form && item.declaration_form !== 'def' ? ` · ${item.declaration_form.replace(/_/g, ' ')}` : ''}{item.exported ? ` · exported (${item.exported})` : ''} · line {item.declaration_span.start_line}</button>} />
      <EvidencePage<ImportReference> key={`imports-${revision}`} title="Unresolved imports" path={`${base}/imports${query}`} render={item => <><code>{item.locator}</code> · {importText(item)}</>} />
    </>}
    {detail && <details open><summary>Declaration details: {detail.qualified_name}</summary><p><code>{detail.stable_identity_id}</code> · {detail.continuity_reason}</p><ul>{detail.parameters?.map(p => <li key={p.name}><code>{p.name}</code> · {p.kind}{p.has_default ? ` · default: ${p.default?.omitted ? 'omitted' : p.default?.text ?? '—'}` : ''}{p.annotation ? ` · annotation: ${p.annotation.text ?? 'omitted'}` : ''}</li>)}</ul>{detail.return_annotation && <p>Return annotation: <code>{detail.return_annotation.text}</code></p>}{detail.decorators.map(d => <p key={d.ordinal}>Decorator: <code>{d.expression.text}</code></p>)}</details>}
  </section>
}
