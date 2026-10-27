import { useState } from 'react'
import type { Entry, EntityDetail, Fact, Relation } from '../../api/semantic'
import { GenerateTab, HistoryTab, InterpretationTab } from './GenerationTabs'
import { visible } from './visible'

type Tab = 'facts' | 'relations' | 'coverage' | 'interpretation' | 'history' | 'generate'
const BASIS: Record<Fact['basis'], string> = {
  parsed: 'Parsed',
  pattern_with_local_evidence: 'Pattern · local evidence',
  pattern_only: 'Pattern only',
}

type Props = {
  detail: EntityDetail | null
  entry: Entry | null
  projectId: string
  snapshotId: string
  onSelect: (key: string) => void
  onGenerated: () => void
}

export function EntityTabs({ detail, entry, projectId, snapshotId, onSelect, onGenerated }: Props) {
  const [tab, setTab] = useState<Tab>('facts')
  if (!detail) return <aside className="explore-column" aria-label="Entity details" />
  const key = detail.entity.entity_key
  return <aside className="explore-column" aria-label="Entity details">
    <div className="tabs" role="tablist">
      {(['facts', 'interpretation', 'relations', 'history', 'coverage', 'generate'] as const).map(name => <button key={name} role="tab" aria-selected={tab === name} className={tab === name ? 'active' : ''} onClick={() => setTab(name)}>{name[0].toUpperCase() + name.slice(1)}</button>)}
    </div>
    {tab === 'facts' && <Facts detail={detail} />}
    {tab === 'interpretation' && <InterpretationTab entry={entry} />}
    {tab === 'relations' && <Relations detail={detail} onSelect={onSelect} />}
    {tab === 'history' && <HistoryTab projectId={projectId} snapshotId={snapshotId} entityKey={key} />}
    {tab === 'coverage' && <Coverage detail={detail} />}
    {tab === 'generate' && <GenerateTab projectId={projectId} snapshotId={snapshotId} entityKey={key} onGenerated={onGenerated} />}
  </aside>
}

function Facts({ detail }: { detail: EntityDetail }) {
  const own = detail.facts.filter(fact => fact.fact_kind !== 'relation')
  return <div className="facts">
    {own.map(fact => <FactItem key={fact.fact_digest} fact={fact} />)}
    {detail.neighbor_facts.length > 0 && <><h3>Imported declarations used here</h3>{detail.neighbor_facts.map(fact => <FactItem key={fact.fact_digest} fact={fact} />)}</>}
  </div>
}

function FactItem({ fact }: { fact: Fact }) {
  return <article className="fact">
    <p className="item-head">
      <span className={`tier ${fact.basis === 'parsed' ? 'tier-approved' : fact.basis === 'pattern_only' ? 'tier-derived' : 'tier-span'}`}>{BASIS[fact.basis]}</span>
      <span className="tier tier-constructed">{fact.fact_kind.replace('_', ' ')}</span>
      <span className={`tier ${fact.verification === 'verified' ? 'tier-approved' : 'tier-derived'}`}>{fact.verification ?? 'unverified'}</span>
    </p>
    <p>{visible(fact.rendered_text)}</p>
    {fact.fact_kind === 'recognizer_match' && <p className="note">Met: {(fact.derived.met ?? []).join(', ') || 'none'} · Unmet: {(fact.derived.unmet ?? []).join(', ') || 'none'}</p>}
    <details><summary>Source rows ({fact.source_refs.length})</summary>
      <ul>{fact.source_refs.map(ref => <li key={`${ref.table}:${ref.id}`}><code>{ref.table}</code> · <code>{ref.locator}</code> · <code>{ref.id.slice(0, 8)}</code></li>)}</ul>
      <p className="note">Digest <code>{fact.fact_digest.slice(0, 16)}</code> · {fact.renderer_version}</p>
    </details>
  </article>
}

function Relations({ detail, onSelect }: { detail: EntityDetail; onSelect: (key: string) => void }) {
  const row = (item: Relation, key: string | null) => <li key={`${item.kind}:${item.fact_digest}`}>
    {item.kind.replace('_', ' ')} · {key ? <button className="link-button" onClick={() => onSelect(key)}>{detail.related[key] ?? key}</button> : <code>{item.target_text}</code>}
    {item.resolution_status && <span className="note"> [{item.resolution_status.replace('_', ' ')}]</span>}
  </li>
  return <div>
    <h3>Outgoing ({detail.outgoing.length})</h3>
    <ul className="relations">{detail.outgoing.map(item => row(item, item.target_entity_key))}</ul>
    <h3>Incoming ({detail.incoming.length})</h3>
    <ul className="relations">{detail.incoming.map(item => row(item, item.source_entity_key))}</ul>
    <p className="note">Same-file references and one-hop import bindings only; there is no cross-file call graph.</p>
  </div>
}

function Coverage({ detail }: { detail: EntityDetail }) {
  const { entity } = detail
  return <dl className="snapshot-details">
    <dt>Stored source</dt><dd>{entity.stored_coverage.replace('_', ' ')}{entity.coverage_reason ? ` · ${entity.coverage_reason}` : ''}</dd>
    <dt>Context</dt><dd>{entity.context_coverage.replace('_', ' ')}{entity.context_reason ? ` · ${entity.context_reason}` : ''}</dd>
    <dt>Package</dt><dd>{entity.package_digest ? <><code>{entity.package_digest.slice(0, 16)}</code> · {entity.package_bytes.toLocaleString()} bytes</> : 'none'}</dd>
    <dt>Omitted</dt><dd>{detail.omitted.length ? detail.omitted.map(item => `lines ${item.start_line}–${item.end_line}`).join(', ') : 'none'}</dd>
    <dt>Gaps</dt><dd>{detail.gaps.count ?? 0} whitespace gaps · {detail.gaps.characters ?? 0} characters</dd>
    <dt>Limitations</dt><dd>{entity.limitations.join(', ') || 'none'}</dd>
    <dt>Entity key</dt><dd><code>{entity.entity_key}</code></dd>
  </dl>
}
