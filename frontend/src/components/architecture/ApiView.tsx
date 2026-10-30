import { useState } from 'react'
import { getApiView, type ApiBranch, type ApiView as ApiViewData, type ArchNode } from '../../api/architecture'
import { visible } from '../explore/visible'
import { Status } from './Views'
import { useRequest } from './useRequest'

type Scope = { projectId: string; snapshotId: string; revision: string }

const OUTCOME_TEXT: Record<string, string> = {
  conditional_candidate: 'conditional candidate',
  ambiguous: 'ambiguous',
  method_mismatch: 'method mismatch',
  method_unknown: 'method unknown',
  no_candidate: 'no candidate',
  external_destination: 'external host (ownership unknown)',
  opaque_origin: 'base unknown',
  opaque_path: 'path unknown',
}

type EndpointPath = { path: string | null; status: string; reasons: string[]; conditions: string[] }

function Outcome({ value }: { value: string }) {
  return <span className={`badge outcome-${value}`}>{OUTCOME_TEXT[value] ?? value}</span>
}

function Branch({ branch, endpoints, onSelect }: { branch: ApiBranch; endpoints: Map<string, ArchNode>; onSelect: (key: string) => void }) {
  return <li className="api-branch">
    <code>{visible(branch.display)}</code> <Outcome value={branch.outcome} />
    {branch.host && <span className="note"> · {visible(branch.host)}{branch.port ? `:${branch.port}` : ''}</span>}
    {branch.candidates.length > 0 && <span> → {branch.candidates.map(key => {
      const endpoint = endpoints.get(key)
      return <button key={key} className="link-button" onClick={() => onSelect(key)}>{endpoint ? visible(String(endpoint.attributes.function ?? endpoint.name)) : 'endpoint'}</button>
    })}</span>}
    {branch.strength && <span className="note"> · {branch.strength.replaceAll('_', ' ')}</span>}
    {branch.conditions.length > 0 && <div className="conditions">{branch.conditions.map(item => <span key={item} className="class-chip">{item}</span>)}</div>}
    {branch.reasons.length > 0 && <div className="note">{branch.reasons.join(' · ')}</div>}
  </li>
}

/** Milestone B API map: every connection is a conditional candidate with its conditions. */
export function ApiView({ projectId, snapshotId, revision, onSelect }: Scope & { onSelect: (key: string) => void }) {
  const [outcome, setOutcome] = useState('')
  const [offset, setOffset] = useState(0)
  const { value, error } = useRequest<ApiViewData>(
    signal => getApiView(projectId, snapshotId, outcome, offset, signal),
    [projectId, snapshotId, revision, outcome, offset],
  )
  if (!value) return <Status error={error} />
  const endpoints = new Map(value.endpoints.map(item => [item.key, item]))
  const coverage = value.coverage as { endpoints?: { total: number; with_full_path: number; reasons: Record<string, number> }; unrecognized_clients?: number; url_usage?: Record<string, number> }
  return <div className="api-view">
    <p className="banner">Connections are conditional candidates inferred from literal URLs and route patterns. Each lists the assumptions it needs; none is a confirmed call.</p>
    <dl className="snapshot-details">
      <div><dt>endpoints</dt><dd>{coverage.endpoints?.total ?? 0}</dd></div>
      <div><dt>with full path</dt><dd>{coverage.endpoints?.with_full_path ?? 0}</dd></div>
      <div><dt>request sites</dt><dd>{value.clients_total}</dd></div>
      <div><dt>unrecognized clients</dt><dd>{coverage.unrecognized_clients ?? 0}</dd></div>
      {Object.entries(coverage.url_usage ?? {}).map(([key, count]) => <div key={key}><dt>{key.replaceAll('_', ' ').toLowerCase()}</dt><dd>{count}</dd></div>)}
    </dl>
    <label>Outcome <select aria-label="Request outcome" value={outcome} onChange={event => { setOutcome(event.target.value); setOffset(0) }}>
      <option value="">all branches</option>
      {Object.entries(value.outcome_counts).map(([key, count]) => <option key={key} value={key}>{OUTCOME_TEXT[key] ?? key} ({count})</option>)}
    </select></label>
    <h3>Request sites ({value.clients_total})</h3>
    <ul className="edge-list">{value.clients.map(item => <li key={item.key} className="api-site">
      <button className="link-button" onClick={() => onSelect(item.key)}>{visible(item.locator ?? '')}:{String(item.attributes.line)}</button>{' '}
      <strong>{String(item.attributes.method ?? '?')}</strong>
      {item.attributes.wrapper ? <span className="note"> via {visible(String(item.attributes.wrapper))}</span> : null}
      <ul>{(item.attributes.branches as ApiBranch[]).map((branch, index) => <Branch key={index} branch={branch} endpoints={endpoints} onSelect={onSelect} />)}</ul>
    </li>)}</ul>
    {value.clients_total > value.offset + value.clients.length && <button onClick={() => setOffset(value.offset + value.limit)}>Next page</button>}
    {value.offset > 0 && <button onClick={() => setOffset(Math.max(0, value.offset - value.limit))}>Previous page</button>}
    <h3>Endpoints ({value.endpoints_total})</h3>
    <ul className="edge-list">{value.endpoints.map(item => {
      const paths = item.attributes.paths as EndpointPath[]
      return <li key={item.key}>
        <button className="link-button" onClick={() => onSelect(item.key)}>{visible(String(item.attributes.function ?? item.name))}</button>{' '}
        <strong>{(item.attributes.methods as string[]).join(',') || '?'}</strong>{' '}
        {paths.length ? paths.map((path, index) => <code key={index}>{path.path ? visible(path.path) : `unknown path (${path.reasons.join(', ')})`}</code>) : <span className="note">no full path ({(item.attributes.reasons as string[]).join(', ')})</span>}
      </li>
    })}</ul>
    {value.destinations.length > 0 && <>
      <h3>External hosts</h3>
      <p className="note">Hosts outside the repository. Their ownership is unknown, so they are never matched to local endpoints.</p>
      <ul className="edge-list">{value.destinations.map(item => <li key={item.key}><code>{visible(String(item.attributes.host))}</code> <span className="note">{String(item.attributes.class).replaceAll('_', ' ')}</span></li>)}</ul>
    </>}
  </div>
}
