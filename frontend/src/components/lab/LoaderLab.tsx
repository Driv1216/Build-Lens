import { useState } from 'react'
import { ApiError } from '../../api/client'
import { runLoaderDiff } from '../../api/lab'

type Summary = Record<string, { files_loaded: number; symlink_escape_followed: boolean; secret_bait_files_loaded: string[]; loaded_but_buildlens_excluded_or_rejected: string[]; absolute_paths_exposed: boolean; errors: string[] }>
type DiffRow = { path: string; buildlens: { outcome: string; reasons?: string[] } } & Record<string, unknown>

export function LoaderLab() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [rows, setRows] = useState<DiffRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true)
    setError(null)
    try {
      const result = await runLoaderDiff(12)
      setSummary(result.summary as Summary)
      setRows(result.rows as DiffRow[])
    } catch (reason) {
      setError(reason instanceof ApiError ? `${reason.code}: ${reason.message}` : 'Loader experiment failed.')
    } finally {
      setBusy(false)
    }
  }

  const variants = summary ? Object.keys(summary) : []
  return (
    <section className="card lab-section">
      <h2>Loader safety (LD-1)</h2>
      <p className="note">
        Framework file loaders run only inside a freshly generated synthetic sandbox (symlink escape canary, synthetic secret bait,
        gitignored/hidden/oversized/binary files). BuildLens's own inventory, reader and secret screen judge the same files.
      </p>
      <button type="button" disabled={busy} onClick={() => void run()}>{busy ? 'Running…' : 'Generate sandbox and compare'}</button>
      {error && <p className="error" role="alert">{error}</p>}
      {summary && (
        <div className="lab-grid">
          {variants.map((name) => (
            <div key={name} className="lab-tile">
              <strong>{name}</strong>
              <ul>
                <li>files loaded: {summary[name].files_loaded}</li>
                <li className={summary[name].symlink_escape_followed ? 'error' : ''}>symlink escape followed: {String(summary[name].symlink_escape_followed)}</li>
                <li className={summary[name].secret_bait_files_loaded.length ? 'error' : ''}>secret bait loaded: {summary[name].secret_bait_files_loaded.join(', ') || 'none'}</li>
                <li>absolute paths in metadata: {String(summary[name].absolute_paths_exposed)}</li>
                <li>loaded but BuildLens excluded/rejected: {summary[name].loaded_but_buildlens_excluded_or_rejected.length}</li>
                {summary[name].errors.length > 0 && <li className="error">errors: {summary[name].errors.join(', ')}</li>}
              </ul>
            </div>
          ))}
        </div>
      )}
      {rows.length > 0 && (
        <div className="inventory-scroll">
          <table aria-label="Loader matrix">
            <thead>
              <tr><th>Path</th><th>BuildLens</th>{variants.map((v) => <th key={v}>{v}</th>)}</tr>
            </thead>
            <tbody>
              {rows.filter((row) => row.buildlens.outcome !== 'not_file').map((row) => (
                <tr key={row.path}>
                  <td><code>{row.path}</code></td>
                  <td>{row.buildlens.outcome}{row.buildlens.reasons?.length ? ` (${row.buildlens.reasons.join(', ')})` : ''}</td>
                  {variants.map((v) => {
                    const cell = row[v] as { loaded: boolean; contains_canary?: boolean; contains_bait?: boolean } | undefined
                    return <td key={v} className={cell?.loaded && row.buildlens.outcome !== 'processed' ? 'warn' : ''}>{cell?.loaded ? `loaded${cell.contains_canary ? ' · CANARY' : ''}${cell.contains_bait ? ' · BAIT' : ''}` : '—'}</td>
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
