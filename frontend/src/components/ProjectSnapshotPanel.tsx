import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { createSnapshot, latestSnapshot, listArtifactChanges, listArtifacts, type ArtifactChangePage, type ArtifactPage, type Snapshot } from '../api/snapshots'
import type { Project } from '../api/projects'
import { StructuralAnalysisPanel } from './StructuralAnalysisPanel'
import { ArtifactProcessingPanel } from './ArtifactProcessingPanel'
import { SemanticSearchPanel } from './SemanticSearchPanel'
import { AskPanel } from './AskPanel'

function displayPath(value: string): string {
  return Array.from(value, (character) => {
    const code = character.codePointAt(0) ?? 0
    return code < 32 || code === 127 ? '�' : character
  }).join('')
}
function countList(values: Record<string, number>): string { return Object.entries(values).map(([name, count]) => `${name}: ${count}`).join(' · ') || 'None' }

export function ProjectSnapshotPanel({ project }: { project: Project }) {
  return <SnapshotScope key={project.id} project={project} />
}
function SnapshotScope({ project }: { project: Project }) {
  const [expanded, setExpanded] = useState(false)
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [artifacts, setArtifacts] = useState<ArtifactPage | null>(null)
  const [changes, setChanges] = useState<ArtifactChangePage | null>(null)
  const [loading, setLoading] = useState(false)
  const [revision, setRevision] = useState(0)
  const [askRevision, setAskRevision] = useState(0)
  const lifetime = useRef<AbortController | null>(null)
  const pageRequest = useRef<AbortController | null>(null)
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!expanded) return
    const controller = new AbortController(); lifetime.current = controller
    setLoading(true)
    void latestSnapshot(project.id, controller.signal).then(async (value) => {
      const [inventory, comparison] = await Promise.all([listArtifacts(project.id, value.id, 0, controller.signal), listArtifactChanges(project.id, value.id, 0, controller.signal)])
      if (!controller.signal.aborted) { setSnapshot(value); setArtifacts(inventory); setChanges(comparison) }
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted && !(reason instanceof ApiError && reason.code === 'SNAPSHOT_NOT_FOUND')) setError(String(reason))
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => { controller.abort(); pageRequest.current?.abort() }
  }, [expanded, project.id])

  async function inspect() {
    setAskRevision(x => x + 1)
    const signal = lifetime.current?.signal
    setScanning(true); setError(null); pageRequest.current?.abort()
    try {
      const value = await createSnapshot(project.id, signal)
      const [inventory, comparison] = await Promise.all([listArtifacts(project.id, value.id, 0, signal), listArtifactChanges(project.id, value.id, 0, signal)])
      if (!signal?.aborted) { setSnapshot(value); setArtifacts(inventory); setChanges(comparison) }
    } catch (reason) { if (!signal?.aborted) setError(String(reason)) }
    finally { setAskRevision(x => x + 1); if (!signal?.aborted) setScanning(false) }
  }
  async function showPage(offset: number, kind: 'artifacts' | 'changes' = 'artifacts') {
    if (!snapshot) return
    pageRequest.current?.abort()
    const controller = new AbortController(); pageRequest.current = controller
    try {
      if (kind === 'artifacts') { const page = await listArtifacts(project.id, snapshot.id, offset, controller.signal); if (!controller.signal.aborted) setArtifacts(page) }
      else { const page = await listArtifactChanges(project.id, snapshot.id, offset, controller.signal); if (!controller.signal.aborted) setChanges(page) }
    } catch (reason) { if (!controller.signal.aborted) setError(String(reason)) }
  }

  return <section className="snapshot-panel">
    <button className="disclosure" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? 'Hide repository snapshot' : 'Show repository snapshot'}</button>
    {expanded && <div className="snapshot-content">
      {loading && <p role="status">Loading latest snapshot…</p>}
      {!loading && <button onClick={inspect} disabled={scanning}>{scanning ? 'Inspecting repository…' : snapshot ? 'Rescan' : 'Inspect repository'}</button>}
      {error && <p className="error" role="alert">{error}</p>}
      {snapshot && <>
        <p className={`status ${snapshot.scan_status}`}>{snapshot.scan_status === 'complete' ? 'Complete observation' : 'Partial observation'}</p>
        <dl className="snapshot-details"><dt>Snapshot</dt><dd>#{snapshot.sequence} · predecessor {snapshot.predecessor_id?.slice(0, 8) ?? 'none'}</dd><dt>Canonical root</dt><dd><code>{displayPath(snapshot.canonical_root)}</code></dd><dt>Observed</dt><dd>{new Date(snapshot.scan_started_at).toLocaleString()} – {new Date(snapshot.scan_completed_at).toLocaleString()}</dd><dt>Manifest</dt><dd><code>{snapshot.content_manifest ?? 'Incomplete fingerprint coverage'}</code></dd><dt>Fingerprint coverage</dt><dd>{snapshot.fingerprint_coverage ? `${snapshot.fingerprint_coverage.fingerprinted_files}/${snapshot.fingerprint_coverage.eligible_files} eligible files verified` : 'Legacy/unavailable'}</dd><dt>Git</dt><dd>{snapshot.git_metadata.state === 'available' ? `${snapshot.git_metadata.branch ?? 'detached'} · ${snapshot.git_metadata.head_commit?.slice(0, 12) ?? 'unborn'} · ${snapshot.git_metadata.dirty === null ? 'dirty state unknown' : snapshot.git_metadata.dirty ? 'dirty' : 'clean'}` : snapshot.git_metadata.state === 'not_repository' ? 'Not a Git repository' : 'Git metadata unavailable'}</dd><dt>Files</dt><dd>{snapshot.summary.included_regular_files} included, {snapshot.summary.excluded_regular_files} excluded, {snapshot.summary.included_bytes.toLocaleString()} bytes</dd><dt>Languages</dt><dd>{countList(snapshot.summary.language_counts)}</dd><dt>Categories</dt><dd>{countList(snapshot.summary.tag_counts)}</dd><dt>Exclusions</dt><dd>{countList(snapshot.summary.exclusion_reason_counts)}</dd></dl>
        <p className="note">Pruned directories count as observed entries; their descendants were not counted. Global Git ignore settings are not used by this inventory.</p>
        {snapshot.warnings.length > 0 && <details><summary>Warnings ({snapshot.warnings.length})</summary><ul>{snapshot.warnings.map((warning) => <li key={`${warning.code}-${warning.locator}`}>{warning.code}: {warning.message}{warning.locator ? ` (${displayPath(warning.locator)})` : ''}</li>)}</ul></details>}
        {changes && <details open><summary>Current versus predecessor ({changes.total})</summary><ul>{changes.items.map((change) => <li key={`${change.state}-${change.locator}`}><code>{displayPath(change.locator)}</code> · {change.state.toLowerCase()} · {change.reason}</li>)}</ul><nav className="pager" aria-label="Change pages"><button disabled={!changes.offset} onClick={() => void showPage(Math.max(0, changes.offset - changes.limit), 'changes')}>Previous</button><button disabled={changes.offset + changes.limit >= changes.total} onClick={() => void showPage(changes.offset + changes.limit, 'changes')}>Next</button></nav></details>}
        {artifacts && <><h3>Inventory</h3><div className="inventory-scroll"><table><thead><tr><th>Path</th><th>Kind</th><th>Language</th><th>Tags</th><th>Size</th><th>Exclusion</th></tr></thead><tbody>{artifacts.items.map((artifact) => <tr key={artifact.id}><td><code>{displayPath(artifact.locator)}</code></td><td>{artifact.kind}</td><td>{artifact.language ?? '—'}</td><td>{artifact.tags.join(', ') || '—'}</td><td>{artifact.size_bytes?.toLocaleString() ?? '—'}</td><td>{artifact.exclusion_reasons.join(', ') || '—'}</td></tr>)}</tbody></table></div><p>{artifacts.total} observed entries</p><div className="pager"><button disabled={artifacts.offset === 0} onClick={() => showPage(Math.max(0, artifacts.offset - artifacts.limit))}>Previous</button><button disabled={artifacts.offset + artifacts.limit >= artifacts.total} onClick={() => showPage(artifacts.offset + artifacts.limit)}>Next</button></div></>}
        <StructuralAnalysisPanel projectId={project.id} snapshotId={snapshot.id} revision={revision} onRefresh={() => setRevision(x => x + 1)} onAskInvalidate={() => setAskRevision(x => x + 1)} />
        <ArtifactProcessingPanel projectId={project.id} snapshotId={snapshot.id} revision={revision} onRefresh={() => setRevision(x => x + 1)} onAskInvalidate={() => setAskRevision(x => x + 1)} />
        <SemanticSearchPanel projectId={project.id} snapshotId={snapshot.id} revision={revision} onAskInvalidate={() => setAskRevision(x => x + 1)} />
        <AskPanel projectId={project.id} snapshotId={snapshot.id} invalidation={askRevision} />
      </>}
    </div>}
  </section>
}
