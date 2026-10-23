import { useEffect, useRef, useState } from 'react'
import { useOutletContext } from 'react-router'
import { ApiError } from '../../api/client'
import { listExperiments, probe, type ExperimentConfig, type LabItem, type LabResult, type ProbeResponse } from '../../api/lab'
import type { LabScope } from './LabLayout'
import { RankAlignment, Waterfall } from './visuals'

function ItemRow({ item, texts }: { item: LabItem; texts: Record<string, string> }) {
  const text = texts[item.text_sha256]
  return (
    <li>
      <div className="item-head">
        <span>#{item.rank}</span>
        <code>{item.locator ?? item.ref}</code>
        {item.span ? <span className="note">[{item.span.start_offset}, {item.span.end_offset})</span> : <span className="tier tier-derived">unanchored</span>}
        <span className="note">{item.score_kind} {item.score === null ? '' : item.score.toFixed(4)}</span>
        <span className="note">{String(item.detail.anchor_method ?? '')}</span>
      </div>
      {text && (
        <details>
          <summary>Source ({item.text_bytes} bytes)</summary>
          <pre>{text}</pre>
        </details>
      )}
    </li>
  )
}

function ResultColumn({ result }: { result: LabResult }) {
  const answer = result.answer as { buildlens_validation?: { valid: boolean; problems: string[] }; model?: string } | null
  return (
    <article className="probe-column">
      <h3>{result.pipeline}</h3>
      <p className="note">
        total {result.timings.total_ms?.toFixed(1)} ms · worker {result.timings.worker_ms?.toFixed(1) ?? '—'} ms · bridge{' '}
        {result.timings.bridge_ms?.toFixed(1) ?? '—'} ms
      </p>
      <p className="note">
        Verification: {Object.entries(result.verification).map(([k, v]) => `${k}=${String(v)}`).join(' · ')}
      </p>
      <Waterfall steps={result.steps} />
      <details open>
        <summary>Ranked ({result.ranked.length})</summary>
        <ol className="lab-items">
          {result.ranked.slice(0, 10).map((item) => <ItemRow key={`${item.ref}-${item.rank}`} item={item} texts={result.texts} />)}
        </ol>
      </details>
      <details>
        <summary>Packed context ({result.context.length} items)</summary>
        <ol className="lab-items">
          {result.context.map((item) => <ItemRow key={item.ref} item={item} texts={result.texts} />)}
        </ol>
      </details>
      {answer && (
        <p className="note">
          Answer ({answer.model ?? 'offline'}): BuildLens validation {answer.buildlens_validation?.valid ? 'passed' : 'failed'}
          {answer.buildlens_validation?.problems?.length ? ` — ${answer.buildlens_validation.problems.join(', ')}` : ''}
        </p>
      )}
    </article>
  )
}

export function ProbeWorkbench() {
  const scope = useOutletContext<LabScope>()
  const [experiments, setExperiments] = useState<(ExperimentConfig & { digest: string })[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [question, setQuestion] = useState('')
  const [response, setResponse] = useState<ProbeResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const sequence = useRef(0)

  useEffect(() => {
    const controller = new AbortController()
    listExperiments(controller.signal)
      .then((value) => !controller.signal.aborted && setExperiments(value.experiments.filter((e) => e.valid)))
      .catch(() => undefined)
    return () => controller.abort()
  }, [])

  useEffect(() => {
    sequence.current += 1 // any input change invalidates in-flight probes
    setResponse(null)
    setBusy(false) // a stale request must not keep the workbench locked
  }, [question, selected, scope?.snapshotId])

  async function onProbe() {
    if (!scope) return
    const current = ++sequence.current
    setBusy(true)
    setError(null)
    try {
      const configs = experiments.filter((e) => selected.includes(e.name))
      const value = await probe(scope.projectId, scope.snapshotId, question, configs)
      if (current === sequence.current) setResponse(value)
    } catch (reason) {
      if (current === sequence.current) setError(reason instanceof ApiError ? `${reason.code}: ${reason.message}` : 'Probe failed.')
    } finally {
      if (current === sequence.current) setBusy(false)
    }
  }

  const ok = (response?.results ?? []).flatMap((r) => (r.ok ? [r.result] : []))
  return (
    <section className="card lab-section">
      <h2>Probe workbench</h2>
      <p className="note">Runs one question through up to three saved pipelines, sequentially (clean latency), with full step traces.</p>
      <label>
        Question
        <input aria-label="Probe question" value={question} onChange={(event) => setQuestion(event.target.value)} />
      </label>
      <fieldset className="lab-checks">
        <legend>Pipelines (max 3)</legend>
        {experiments.map((experiment) => (
          <label key={experiment.name} className="inline">
            <input
              type="checkbox"
              aria-label={experiment.name}
              checked={selected.includes(experiment.name)}
              disabled={!selected.includes(experiment.name) && selected.length >= 3}
              onChange={(event) =>
                setSelected(event.target.checked ? [...selected, experiment.name] : selected.filter((n) => n !== experiment.name))
              }
            />
            {experiment.name}
          </label>
        ))}
      </fieldset>
      <button type="button" disabled={busy || !scope || !question.trim() || !selected.length} onClick={() => void onProbe()}>
        {busy ? 'Probing…' : 'Probe'}
      </button>
      {error && <p className="error" role="alert">{error}</p>}
      {response && (
        <>
          {response.results.map((r, i) =>
            r.ok ? null : <p key={i} className="error">{r.pipeline}: {r.error.code}</p>,
          )}
          <div className="probe-grid">{ok.map((result) => <ResultColumn key={result.pipeline + result.config_digest} result={result} />)}</div>
          {ok.length >= 2 && (
            <div className="alignment-wrap">
              <h3>Rank alignment: {ok[0].pipeline} ↔ {ok[1].pipeline}</h3>
              <RankAlignment left={ok[0].ranked} right={ok[1].ranked} />
              <p className="note">Overlap@k: {JSON.stringify(response.overlap['0-1'])}</p>
            </div>
          )}
        </>
      )}
    </section>
  )
}
