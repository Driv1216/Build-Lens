import { createColumnHelper, flexRender, getCoreRowModel, getSortedRowModel, useReactTable, type SortingState } from '@tanstack/react-table'
import { useEffect, useMemo, useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { getRunCase, getRunFile, listRunCases, listRuns, type LabStep, type RunListing, type Scores } from '../../api/lab'
import { TierBadge, Waterfall } from './visuals'

type Row = { pipeline: string; [metric: string]: number | string | null }
const helper = createColumnHelper<Row>()

export function RunsBrowser() {
  const [runs, setRuns] = useState<RunListing[]>([])
  const [runId, setRunId] = useState('')
  const [scores, setScores] = useState<Scores | null>(null)
  const [other, setOther] = useState<Record<string, unknown> | null>(null)
  const [subset, setSubset] = useState<'matched' | 'all'>('matched')
  const [metric, setMetric] = useState('span:context_coverage')
  const [sorting, setSorting] = useState<SortingState>([])
  const [cases, setCases] = useState<string[]>([])
  const [caseName, setCaseName] = useState('')
  const [record, setRecord] = useState<Record<string, unknown> | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    listRuns(controller.signal).then((value) => !controller.signal.aborted && setRuns(value.runs)).catch(() => undefined)
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!runId) return
    const controller = new AbortController()
    const run = runs.find((r) => r.run_id === runId)
    setScores(null)
    setOther(null)
    setRecord(null)
    if (run?.files.includes('scores.json')) {
      getRunFile<Scores>(runId, 'scores.json', controller.signal).then((v) => !controller.signal.aborted && setScores(v)).catch(() => undefined)
      listRunCases(runId, controller.signal).then((v) => !controller.signal.aborted && setCases(v.cases)).catch(() => undefined)
    } else if (run?.files[0]) {
      getRunFile<Record<string, unknown>>(runId, run.files[0], controller.signal).then((v) => !controller.signal.aborted && setOther(v)).catch(() => undefined)
    }
    return () => controller.abort()
  }, [runId, runs])

  useEffect(() => {
    if (!runId || !caseName) return
    const controller = new AbortController()
    getRunCase(runId, caseName, controller.signal).then((v) => !controller.signal.aborted && setRecord(v)).catch(() => undefined)
    return () => controller.abort()
  }, [runId, caseName])

  const means = useMemo(() => {
    if (!scores) return {}
    const matched = (scores.summary as Scores['summary'] & { matched?: { means: Scores['summary']['means'] } }).matched
    return subset === 'matched' ? matched?.means ?? {} : scores.summary.means
  }, [scores, subset])
  const shown = useMemo(() => {
    const keys = new Set<string>()
    Object.values(means).forEach((m) => Object.keys(m).forEach((k) => keys.add(k)))
    return [...keys].sort().filter((m) => /MRR@32|recall@5|precision@5|evidence_coverage@5|context_coverage$|context_kib|coverage_per_kib|derived_recall@5/.test(m))
  }, [means])
  // Stable references: react-table resets internal state when data/columns change identity.
  const rows: Row[] = useMemo(
    () =>
      Object.entries(means).map(([pipeline, values]) => ({
        pipeline,
        ...Object.fromEntries(shown.map((m) => [m, values[m]?.mean ?? null])),
        p50: scores?.summary.latency[pipeline]?.p50_ms ?? null,
      })),
    [means, shown, scores],
  )
  const columns = useMemo(
    () => [
      helper.accessor('pipeline', { header: 'Pipeline' }),
      ...shown.map((m) => helper.accessor((row) => row[m], { id: m, header: m, cell: (info) => (typeof info.getValue() === 'number' ? (info.getValue() as number).toFixed(4) : '—') })),
      helper.accessor((row) => row.p50, { id: 'p50', header: 'p50 ms', cell: (info) => String(info.getValue() ?? '—') }),
    ],
    [shown],
  )
  const table = useReactTable({ data: rows, columns, state: { sorting }, onSortingChange: setSorting, getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel() })
  const chart = rows.map((row) => ({ pipeline: row.pipeline, value: typeof row[metric] === 'number' ? row[metric] : 0 }))

  return (
    <section className="card lab-section">
      <h2>Benchmark runs</h2>
      <label>
        Run
        <select aria-label="Run" value={runId} onChange={(event) => setRunId(event.target.value)}>
          <option value="">Select a run</option>
          {runs.map((run) => <option key={run.run_id} value={run.run_id}>{run.run_id}</option>)}
        </select>
      </label>
      {other && <pre aria-label="Run artifact">{JSON.stringify(other, null, 2).slice(0, 20000)}</pre>}
      {scores && (
        <>
          <p className="note">
            Dataset <code>{scores.dataset.id}</code> ({scores.dataset.review_status}) · status {scores.status} · {JSON.stringify(scores.counts)}
          </p>
          <div className="tier-legend">
            {Object.entries(scores.tiers).map(([tier, note]) => (
              <span key={tier} title={note}><TierBadge tier={tier} /></span>
            ))}
          </div>
          <label className="inline">
            <input type="checkbox" aria-label="Matched subset" checked={subset === 'matched'} onChange={(event) => setSubset(event.target.checked ? 'matched' : 'all')} />
            Matched subset (cases every pipeline could fully see)
          </label>
          <div className="inventory-scroll">
            <table>
              <thead>
                {table.getHeaderGroups().map((group) => (
                  <tr key={group.id}>
                    {group.headers.map((header) => (
                      <th key={header.id} onClick={header.column.getToggleSortingHandler()} className="sortable">
                        {flexRender(header.column.columnDef.header, header.getContext())}
                      </th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody>
                {table.getRowModel().rows.map((row) => (
                  <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
          <label>
            Chart metric
            <select aria-label="Chart metric" value={metric} onChange={(event) => setMetric(event.target.value)}>
              {shown.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
          <div className="chart">
            <ResponsiveContainer width="100%" height={Math.max(220, rows.length * 22)}>
              <BarChart data={chart} layout="vertical" margin={{ left: 160 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#2b384e" />
                <XAxis type="number" stroke="#9fb0ca" />
                <YAxis type="category" dataKey="pipeline" stroke="#9fb0ca" width={200} />
                <Tooltip />
                <Bar dataKey="value" fill="#7aa2ff" />
              </BarChart>
            </ResponsiveContainer>
          </div>
          {scores.comparisons.length > 0 && (
            <details>
              <summary>Paired comparisons vs baseline (descriptive)</summary>
              <table>
                <thead><tr><th>Candidate</th><th>Metric</th><th>n</th><th>mean Δ</th><th>W/L/T</th><th>bootstrap 95%</th></tr></thead>
                <tbody>
                  {scores.comparisons.map((c) => (
                    <tr key={c.candidate + c.metric}><td>{c.candidate}</td><td>{c.metric}</td><td>{c.n}</td><td>{c.mean_delta.toFixed(4)}</td><td>{c.wins}/{c.losses}/{c.ties}</td><td>{c.bootstrap_95.join(' … ')}</td></tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
          <label>
            Case drill-down
            <select aria-label="Case record" value={caseName} onChange={(event) => setCaseName(event.target.value)}>
              <option value="">Select a case</option>
              {cases.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          {record && (
            <div>
              <Waterfall steps={(record.steps as LabStep[]) ?? []} />
              <pre>{JSON.stringify({ tiers: record.tiers, verification: record.verification, embedding: record.embedding, timings_ms: record.timings_ms, targets_available: record.targets_available }, null, 2)}</pre>
            </div>
          )}
        </>
      )}
    </section>
  )
}
