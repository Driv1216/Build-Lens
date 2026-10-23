import { useEffect, useMemo, useState } from 'react'
import { useOutletContext } from 'react-router'
import { ApiError } from '../../api/client'
import {
  buildIndex,
  getLabStatus,
  saveExperiment,
  validateConfig,
  type ExperimentConfig,
  type LabStatus,
  type ParamSpec,
} from '../../api/lab'
import type { LabScope } from './LabLayout'

function initialParams(params: Record<string, ParamSpec>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(params).map(([name, spec]) => [name, spec.default]))
}

export function ExperimentBuilder() {
  const scope = useOutletContext<LabScope>()
  const [status, setStatus] = useState<LabStatus | null>(null)
  const [pipeline, setPipeline] = useState('langchain.vector')
  const [name, setName] = useState('my-experiment')
  const [mode, setMode] = useState('')
  const [source, setSource] = useState('')
  const [params, setParams] = useState<Record<string, unknown>>({})
  const [result, setResult] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    getLabStatus(controller.signal).then((value) => !controller.signal.aborted && setStatus(value)).catch(() => undefined)
    return () => controller.abort()
  }, [])

  const spec = status?.pipelines.find((entry) => entry.id === pipeline)
  useEffect(() => {
    if (!spec) return
    setParams(initialParams(spec.params))
    setMode(spec.corpus_modes[0])
    setSource(spec.corpus_modes[0] === 'native_chunks' ? 'reuse-stored' : 'compute')
  }, [spec])

  const config: ExperimentConfig = useMemo(
    () => ({ name, pipeline, corpus: { mode }, embedding: { source }, params }),
    [name, pipeline, mode, source, params],
  )

  async function run(action: 'validate' | 'save' | 'build') {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      if (action === 'validate') setResult(JSON.stringify(await validateConfig(config), null, 2))
      if (action === 'save') setResult(JSON.stringify(await saveExperiment(config), null, 2))
      if (action === 'build' && scope) {
        const built = await buildIndex(scope.projectId, scope.snapshotId, config)
        setResult(JSON.stringify({ ...built, steps: undefined }, null, 2))
      }
    } catch (reason) {
      setError(reason instanceof ApiError ? `${reason.code}: ${reason.message}` : 'Request failed.')
    } finally {
      setBusy(false)
    }
  }

  if (!status) return <p className="note">Loading catalog…</p>
  return (
    <section className="card lab-section">
      <h2>Experiment builder</h2>
      <p className="note">The form is generated from the server's strict schema. Unavailable pipelines are disabled with their reason.</p>
      <div className="lab-form">
        <label>
          Pipeline
          <select aria-label="Pipeline" value={pipeline} onChange={(event) => setPipeline(event.target.value)}>
            {status.pipelines.map((entry) => (
              <option key={entry.id} value={entry.id} disabled={!entry.available}>
                {entry.outcome} · {entry.id}{entry.available ? '' : ` (unavailable: ${entry.reasons[0]})`}
              </option>
            ))}
          </select>
        </label>
        <label>
          Name
          <input aria-label="Experiment name" value={name} onChange={(event) => setName(event.target.value)} />
        </label>
        <label>
          Corpus mode
          <select aria-label="Corpus mode" value={mode} onChange={(event) => setMode(event.target.value)}>
            {spec?.corpus_modes.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        <label>
          Embedding source
          <select aria-label="Embedding source" value={source} onChange={(event) => setSource(event.target.value)}>
            {['reuse-stored', 'reuse-or-compute', 'compute', 'deterministic-fake'].map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
        </label>
        {spec &&
          Object.entries(spec.params).map(([key, param]) => (
            <label key={key} title={param.help}>
              {key}
              {param.kind === 'bool' ? (
                <input type="checkbox" aria-label={key} checked={Boolean(params[key])} onChange={(event) => setParams({ ...params, [key]: event.target.checked })} />
              ) : param.kind === 'choice' ? (
                <select aria-label={key} value={String(params[key] ?? '')} onChange={(event) => setParams({ ...params, [key]: event.target.value })}>
                  {param.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
                </select>
              ) : param.kind.endsWith('list') ? (
                <input aria-label={key} value={Array.isArray(params[key]) ? (params[key] as number[]).join(',') : ''} onChange={(event) => setParams({ ...params, [key]: event.target.value.split(',').map(Number) })} />
              ) : (
                <input type="number" aria-label={key} value={String(params[key] ?? '')} min={param.minimum ?? undefined} max={param.maximum ?? undefined} step={param.kind === 'float' ? 0.05 : 1} onChange={(event) => setParams({ ...params, [key]: param.kind === 'int' ? parseInt(event.target.value, 10) : parseFloat(event.target.value) })} />
              )}
            </label>
          ))}
      </div>
      <div className="lab-actions">
        <button type="button" disabled={busy} onClick={() => void run('validate')}>Validate</button>
        <button type="button" disabled={busy} onClick={() => void run('save')}>Save</button>
        <button type="button" disabled={busy || !scope || !spec?.available} onClick={() => void run('build')}>Build index</button>
      </div>
      <h3>Configuration preview</h3>
      <pre aria-label="Configuration preview">{JSON.stringify(config, null, 2)}</pre>
      {error && <p className="error" role="alert">{error}</p>}
      {result && <pre aria-label="Server response">{result}</pre>}
    </section>
  )
}
