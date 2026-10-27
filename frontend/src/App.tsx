import { Suspense, lazy, useEffect, useState, type FormEvent } from 'react'
import { BrowserRouter, Link, Route, Routes } from 'react-router'
import { createProject, listProjects, type Project } from './api/projects'
import { ProjectSnapshotPanel } from './components/ProjectSnapshotPanel'
import { ExperimentBuilder } from './components/lab/ExperimentBuilder'
import { IndexInspector } from './components/lab/IndexInspector'
import { LabLayout } from './components/lab/LabLayout'
import { LabOverview } from './components/lab/LabOverview'
import { LoaderLab } from './components/lab/LoaderLab'
import { ProbeWorkbench } from './components/lab/ProbeWorkbench'

// Charting/table libraries are only needed on the runs page: keep them out of the main bundle.
const RunsBrowser = lazy(() => import('./components/lab/RunsBrowser').then((m) => ({ default: m.RunsBrowser })))

function Dashboard() {
  const [projects, setProjects] = useState<Project[]>([])
  const [name, setName] = useState('')
  const [rootPath, setRootPath] = useState('')
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void listProjects()
      .then((items) => active && setProjects(items))
      .catch((reason: unknown) => active && setError(messageFrom(reason)))
      .finally(() => active && setLoading(false))
    return () => {
      active = false
    }
  }, [])

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const project = await createProject({ name, root_path: rootPath })
      setProjects((current) => [project, ...current])
      setName('')
      setRootPath('')
    } catch (reason: unknown) {
      setError(messageFrom(reason))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="shell">
      <header>
        <p className="eyebrow">Phase 2 · Stage 12 · Evidence, retrieval and framework laboratory</p>
        <h1>BuildLens</h1>
        <p className="subtitle">Register local projects, capture snapshots, prepare local embeddings, and search exact source evidence.</p>
        <nav className="app-nav"><Link to="/lab">Open the LangChain / LlamaIndex laboratory →</Link></nav>
      </header>

      <section className="card" aria-labelledby="new-project-heading">
        <h2 id="new-project-heading">Add a project</h2>
        <form onSubmit={onSubmit}>
          <label>
            Project name
            <input value={name} onChange={(event) => setName(event.target.value)} required maxLength={200} />
          </label>
          <label>
            Root path
            <input value={rootPath} onChange={(event) => setRootPath(event.target.value)} required />
          </label>
          <button type="submit" disabled={submitting}>
            {submitting ? 'Adding…' : 'Add project'}
          </button>
        </form>
      </section>

      <section className="card" aria-labelledby="projects-heading">
        <div className="section-heading">
          <h2 id="projects-heading">Projects</h2>
          <span>{projects.length}</span>
        </div>
        {loading && <p role="status">Loading projects…</p>}
        {!loading && error && <p className="error" role="alert">{error}</p>}
        {!loading && !error && projects.length === 0 && <p>No projects registered yet.</p>}
        {!loading && !error && projects.length > 0 && (
          <ul className="project-list">
            {projects.map((project) => (
              <li key={project.id}>
                <strong>{project.name}</strong>
                <code>{project.root_path}</code>
                <ProjectSnapshotPanel project={project} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  )
}

function messageFrom(reason: unknown): string {
  return reason instanceof Error ? reason.message : 'Unable to reach the BuildLens API.'
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/lab" element={<LabLayout />}>
          <Route index element={<LabOverview />} />
          <Route path="experiments" element={<ExperimentBuilder />} />
          <Route path="probe" element={<ProbeWorkbench />} />
          <Route path="indexes" element={<IndexInspector />} />
          <Route path="runs" element={<Suspense fallback={<p className="note">Loading runs…</p>}><RunsBrowser /></Suspense>} />
          <Route path="loaders" element={<LoaderLab />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
