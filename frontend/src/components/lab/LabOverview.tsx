import { useEffect, useState } from 'react'
import { getLabStatus, type LabStatus } from '../../api/lab'

export function LabOverview() {
  const [status, setStatus] = useState<LabStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    getLabStatus(controller.signal)
      .then((value) => !controller.signal.aborted && setStatus(value))
      .catch(() => !controller.signal.aborted && setError('Laboratory status is unavailable.'))
    return () => controller.abort()
  }, [])

  if (error) return <p className="error">{error}</p>
  if (!status) return <p className="note">Loading laboratory status…</p>
  return (
    <section className="card lab-section">
      <h2>Capabilities on this machine</h2>
      <p className="note">
        Generation: <strong>{status.generation.state}</strong>. Lab database:{' '}
        <strong>{status.lab_database_configured ? 'configured (separate, least-privilege)' : 'not configured'}</strong>.
        Python {status.python}.
      </p>
      <div className="lab-grid">
        {Object.entries(status.extras).map(([extra, packages]) => (
          <div key={extra} className="lab-tile">
            <strong>{extra}</strong> {status.extras_installed[extra] ? '✓' : '—'}
            <ul>
              {Object.entries(packages).map(([name, version]) => (
                <li key={name}><code>{name}</code> {version ?? 'not installed'}</li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      <h2>Pipelines</h2>
      <div className="inventory-scroll">
        <table>
          <thead>
            <tr><th>Outcome</th><th>Pipeline</th><th>Family</th><th>Corpus</th><th>Identity</th><th>Availability</th></tr>
          </thead>
          <tbody>
            {status.pipelines.map((pipeline) => (
              <tr key={pipeline.id}>
                <td>{pipeline.outcome}</td>
                <td><code>{pipeline.id}</code><br />{pipeline.label}</td>
                <td>{pipeline.family}</td>
                <td>{pipeline.corpus_modes.join(', ')}</td>
                <td>{pipeline.native_chunk_identity ? 'BuildLens chunks' : 'framework nodes'}</td>
                <td>{pipeline.available ? 'available' : <span className="error">{pipeline.reasons.join('; ')}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
