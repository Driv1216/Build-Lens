import type { EntityDetail, EvidenceChunk } from '../../api/semantic'
import { visible } from './visible'

/** Source assembled from stored, hash-verified chunks; nothing is read from disk. */
export function SourceView({ detail, cited = new Set<number>() }: { detail: EntityDetail | null; cited?: Set<number> }) {
  if (!detail) return <section className="explore-column source-view" aria-label="Source"><p className="note">Select an entity to see its stored source.</p></section>
  const { entity, evidence } = detail
  const outline = entity.context_coverage === 'outline'
  const included = new Set(detail.package_chunks)
  const chunks = [...evidence].sort((a, b) => a.start_offset - b.start_offset)
  return <section className="explore-column source-view" aria-label="Source">
    <h2>{entity.name}</h2>
    <p className="note"><code>{entity.locator}</code>{entity.start_line ? ` · lines ${entity.start_line}–${entity.end_line}` : ''} · stored source {entity.stored_coverage.replace('_', ' ')}{entity.coverage_reason ? ` (${entity.coverage_reason})` : ''}</p>
    {detail.integrity_errors.length > 0 && <p className="error" role="alert">Integrity errors: {detail.integrity_errors.join(', ')}</p>}
    {detail.freshness_reasons.length > 0 && <p className="note">Historical catalog: {detail.freshness_reasons.join(', ')}</p>}
    {outline && <p className="note">Outline context: shaded declarations are declared omitted and are represented by their parsed signatures only.</p>}
    {chunks.length === 0 && <p className="note">No stored chunks are available for this entity.</p>}
    <div className="source-chunks">
      {chunks.map((chunk, index) => <div key={chunk.chunk_id}>
        {index > 0 && chunk.start_offset > chunks[index - 1].end_offset && <p className="gap">… {chunk.start_offset - chunks[index - 1].end_offset} whitespace characters not stored</p>}
        <Chunk chunk={chunk} omitted={outline && !included.has(chunk.stable_key)} cited={cited} />
      </div>)}
    </div>
  </section>
}

function Chunk({ chunk, omitted, cited }: { chunk: EvidenceChunk; omitted: boolean; cited: Set<number> }) {
  if (chunk.verified === false || chunk.content === null) return <p className="error">Chunk {chunk.stable_key.slice(0, 12)} failed verification; content is withheld.</p>
  const lines = chunk.content.split(/\r\n|\r|\n/)
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
  return <pre className={`source ${omitted ? 'omitted' : ''}`} aria-label={`Lines ${chunk.start_line}–${chunk.end_line}`}>
    {lines.map((line, offset) => <span key={offset} className={`source-line${cited.has(chunk.start_line + offset) ? ' cited' : ''}`}><span className="line-number">{chunk.start_line + offset}</span>{visible(line)}{'\n'}</span>)}
  </pre>
}
