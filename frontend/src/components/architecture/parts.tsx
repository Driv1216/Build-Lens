import type { ArchEdge, Tier } from '../../api/architecture'
import { visible } from '../explore/visible'
import { label } from './format'

const TIER_LABEL: Record<Tier, string> = { observed: 'Observed', derived: 'Derived', inferred: 'Inferred', unknown: 'Unknown' }
const CLASS_LABEL: Record<string, string> = {
  unconditional: 'unconditional',
  presumed_unconditional: 'presumed unconditional',
  conditional: 'conditional',
  elision_dependent: 'may be elided',
  type_level: 'type level',
  deferred: 'deferred',
}

export function TierBadge({ tier }: { tier: Tier }) {
  return <span className={`badge tier-badge tier-${tier}`} title={`Evidence tier: ${TIER_LABEL[tier]}`}>{TIER_LABEL[tier]}</span>
}

export function ClassChip({ value }: { value: string | null }) {
  if (!value) return null
  return <span className={`class-chip class-${value}`}>{CLASS_LABEL[value] ?? value}</span>
}

export function EdgeLine({ edge, onSelect, direction = 'out' }: { edge: ArchEdge; onSelect?: (key: string) => void; direction?: 'out' | 'in' }) {
  const other = direction === 'out' ? edge.target_key : edge.source_key
  return <li className="edge-line">
    <TierBadge tier={edge.tier} /> <span className="kind">{edge.kind}</span>{' '}
    {other && onSelect ? <button className="link-button" onClick={() => onSelect(other)}>{label(other)}</button> : <code>{visible(edge.target_text ?? other ?? '')}</code>}
    {edge.resolution_status && edge.resolution_status !== 'resolved' && <span className="badge warn"> {edge.resolution_status}</span>}
    {' '}<ClassChip value={edge.import_class} /> <span className="note">×{edge.count}</span>
    {edge.conditions.length > 0 && <span className="note conditions"> {edge.conditions.join(' · ')}</span>}
  </li>
}

export function Gaps({ gaps }: { gaps: { complete: boolean; unknown_exits: ArchEdge[]; coverage_gaps: string[]; run_gaps: string[] } }) {
  if (gaps.complete) return <p className="note">No known gaps along this search.</p>
  return <details className="gaps">
    <summary>Why this is not a complete claim ({gaps.unknown_exits.length + gaps.coverage_gaps.length + gaps.run_gaps.length})</summary>
    {gaps.run_gaps.length > 0 && <p className="note">Run-level: {gaps.run_gaps.join(', ')}</p>}
    {gaps.coverage_gaps.length > 0 && <p className="note">Not fully analysed: {gaps.coverage_gaps.map(label).join(', ')}</p>}
    {gaps.unknown_exits.length > 0 && <ul className="edge-list">{gaps.unknown_exits.map(edge => <EdgeLine key={edge.key} edge={edge} />)}</ul>}
  </details>
}
