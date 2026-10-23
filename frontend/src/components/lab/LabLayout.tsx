import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router'
import { listProjects, type Project } from '../../api/projects'
import { latestSnapshot } from '../../api/snapshots'

export type LabScope = { projectId: string; snapshotId: string } | null

export function LabLayout() {
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [snapshotId, setSnapshotId] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    listProjects()
      .then((items) => {
        if (controller.signal.aborted) return
        setProjects(items)
        if (items[0]) setProjectId((current) => current || items[0].id)
      })
      .catch(() => !controller.signal.aborted && setError('Projects are unavailable.'))
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!projectId) return
    const controller = new AbortController()
    setSnapshotId('')
    latestSnapshot(projectId, controller.signal)
      .then((snapshot) => !controller.signal.aborted && setSnapshotId(snapshot.id))
      .catch(() => !controller.signal.aborted && setSnapshotId(''))
    return () => controller.abort()
  }, [projectId])

  const scope: LabScope = projectId && snapshotId ? { projectId, snapshotId } : null
  return (
    <main className="lab-shell">
      <p className="eyebrow">Phase 2 · Stage 12 · Framework laboratory</p>
      <h1 className="lab-title">LangChain / LlamaIndex lab</h1>
      <p className="subtitle">
        Framework pipelines run in supervised worker processes over authoritative BuildLens evidence. Offline by default:
        no model downloads, no external provider calls.
      </p>
      <div className="lab-scope card">
        <label>
          Project
          <select aria-label="Lab project" value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>{project.name}</option>
            ))}
          </select>
        </label>
        <p className="note">Snapshot: <code>{snapshotId || 'none'}</code></p>
        {error && <p className="error">{error}</p>}
      </div>
      <nav className="lab-nav" aria-label="Laboratory">
        {[
          ['', 'Overview'],
          ['experiments', 'Experiment builder'],
          ['probe', 'Probe workbench'],
          ['indexes', 'Index inspector'],
          ['runs', 'Benchmark runs'],
          ['loaders', 'Loader safety'],
        ].map(([path, label]) => (
          <NavLink key={path} to={path ? `/lab/${path}` : '/lab'} end={!path}>
            {label}
          </NavLink>
        ))}
      </nav>
      <Outlet context={scope} />
    </main>
  )
}
