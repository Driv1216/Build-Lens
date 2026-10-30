import { useState } from 'react'
import { ApiError } from '../../api/client'
import {
  declareRules, getRuleResults, getView,
  type ArchDiagnostic, type ArchNode, type ConformanceResult, type RuleResults, type ViewName, type ViewResult,
} from '../../api/architecture'
import { visible } from '../explore/visible'
import { TierBadge } from './parts'
import { describe, label } from './format'
import { Status } from './Views'
import { useRequest } from './useRequest'

type Scope = { projectId: string; snapshotId: string; revision: string }
type Props = Scope & { onSelect: (key: string) => void }

function useView(scope: Scope, name: ViewName) {
  return useRequest<ViewResult>(signal => getView(scope.projectId, scope.snapshotId, name, signal), [scope.projectId, scope.snapshotId, scope.revision, name])
}

function Diagnostics({ items, onSelect }: { items: ArchDiagnostic[]; onSelect: (key: string) => void }) {
  if (items.length === 0) return null
  return <><h3>Diagnostics</h3><ul className="edge-list">{items.map(item => <li key={item.ordinal} className="edge-line">
    <TierBadge tier={item.tier} /> <span className="kind">{item.code}</span>{' '}
    {item.subjects.slice(0, 3).map(key => <button key={key} className="link-button" onClick={() => onSelect(key)}>{label(key)}</button>)}
    {Array.isArray(item.attributes.names) && <span className="note"> {visible((item.attributes.names as string[]).join(', '))}</span>}
    {Array.isArray(item.attributes.blocking_gaps) && (item.attributes.blocking_gaps as string[]).length > 0 && <span className="note"> · blocked by {(item.attributes.blocking_gaps as string[]).join(', ')}</span>}
  </li>)}</ul></>
}

function Counts({ value }: { value: Record<string, unknown> | undefined }) {
  if (!value) return null
  return <dl className="snapshot-details">{Object.entries(value).filter(([, item]) => typeof item === 'number').map(([key, item]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(item)}</dd></div>)}</dl>
}

const ORIGIN_TEXT: Record<string, string> = {
  declared: 'declared', stdlib: 'standard library', builtin: 'Node built-in', declared_only: 'declared, no import observed',
  declared_not_matched: 'declared, import name not matched',
  not_matched_to_declaration: 'import name not matched to a declaration', imported_not_declared: 'imported, not declared',
  workspace_package: 'workspace package', mixed: 'differs by workspace',
}

export function IntegrationsView(props: Props) {
  const { value, error } = useView(props, 'integrations')
  if (!value) return <Status error={error} />
  const coverage = value.coverage.integrations as Record<string, unknown> | undefined
  const packages = value.nodes.filter(node => node.kind === 'external_package')
  const hosts = value.nodes.filter(node => node.kind === 'external_destination')
  const groups = new Map<string, ArchNode[]>()
  for (const item of packages) {
    const origin = String(item.attributes.origin ?? 'unknown')
    groups.set(origin, [...(groups.get(origin) ?? []), item])
  }
  return <div className="views">
    <p className="banner">Packages from stored manifests and resolved imports. Python import names are matched to declared distributions only by normalised name; a mismatch stays unknown.</p>
    <Counts value={coverage} />
    {[...groups.entries()].sort().map(([origin, items]) => <details key={origin} open={origin !== 'stdlib'}>
      <summary>{ORIGIN_TEXT[origin] ?? origin} ({items.length})</summary>
      <ul className="tag-list">{items.map(item => <li key={item.key}><button className="link-button" onClick={() => props.onSelect(item.key)}>{visible(item.name)}</button> <span className="note">{String(item.attributes.ecosystem ?? '')}</span></li>)}</ul>
    </details>)}
    {hosts.length > 0 && <><h3>External hosts</h3><ul className="edge-list">{hosts.map(item => <li key={item.key}><code>{visible(String(item.attributes.host))}</code> <span className="note">ownership unknown</span></li>)}</ul></>}
    <Diagnostics items={value.diagnostics} onSelect={props.onSelect} />
  </div>
}

export function DatabaseView(props: Props) {
  const { value, error } = useView(props, 'database')
  if (!value) return <Status error={error} />
  const coverage = value.coverage.database as { absence_gaps?: string[]; not_covered?: string[] } & Record<string, unknown> | undefined
  const references = value.edges.filter(edge => edge.kind === 'references_table')
  const uses = new Map<string, number>()
  for (const item of value.edges.filter(edge => edge.kind === 'uses_model')) uses.set(item.target_key ?? '', (uses.get(item.target_key ?? '') ?? 0) + 1)
  return <div className="views">
    <p className="banner">Tables from SQLAlchemy models and Alembic <code>op.create_table</code> calls, merged by name. No database is contacted and no SQL is parsed.</p>
    <Counts value={coverage} />
    {coverage?.not_covered && <p className="note">Not covered: {coverage.not_covered.join(', ')}.</p>}
    <table className="views-table"><thead><tr><th scope="col">Table</th><th scope="col">Models</th><th scope="col">Migrations</th><th scope="col">Importers of the model</th><th scope="col">References</th></tr></thead>
      <tbody>{value.nodes.map(item => <tr key={item.key}>
        <th scope="row"><button className="link-button" onClick={() => props.onSelect(item.key)}>{visible(item.name)}</button>{item.attributes.merged_by_name ? <span className="note"> · merged by name</span> : null}</th>
        <td>{(item.attributes.models as { model: string }[]).map(model => visible(model.model)).join(', ') || '—'}</td>
        <td>{(item.attributes.migrations as unknown[]).length || '—'}</td>
        <td>{uses.get(item.key) ?? 0}</td>
        <td>{references.filter(edge => edge.source_key === item.key).map(edge => label(edge.target_key ?? '').replace('db_table:', '')).join(', ') || '—'}</td>
      </tr>)}</tbody>
    </table>
    <Diagnostics items={value.diagnostics} onSelect={props.onSelect} />
  </div>
}

export function FrontendView(props: Props) {
  const { value, error } = useView(props, 'frontend')
  if (!value) return <Status error={error} />
  const targets = new Map(value.edges.map(edge => [edge.source_key, edge.target_key ?? '']))
  return <div className="views">
    <p className="banner">JSX <code>&lt;Route&gt;</code> elements with their enclosing routes joined. Object route configurations and lazy elements are not covered.</p>
    <Counts value={value.coverage.frontend_routes as Record<string, unknown> | undefined} />
    <ul className="edge-list">{value.nodes.map(item => <li key={item.key}>
      <code>{visible(String(item.attributes.full_path ?? 'unknown path'))}</code> → {item.attributes.element ? visible(String(item.attributes.element)) : <span className="note">no element</span>}
      {targets.get(item.key) ? <> <button className="link-button" onClick={() => props.onSelect(targets.get(item.key) ?? '')}>{label(targets.get(item.key) ?? '')}</button></> : <span className="note"> · module not bound</span>}
    </li>)}</ul>
  </div>
}

const OUTCOME_TEXT: Record<ConformanceResult['outcome'], string> = {
  not_satisfied: 'not satisfied',
  satisfied_in_observed_graph: 'satisfied in the observed graph',
  not_determinable: 'not determinable',
  selector_matches_nothing: 'selector matches nothing',
}

function Rules({ projectId, snapshotId, revision, onSelect }: Props) {
  const [version, setVersion] = useState(0)
  const [draft, setDraft] = useState('[\n  {"name": "domain is independent", "kind": "forbid", "from": ["**/domain/**"], "to": ["**/application/**"]}\n]')
  const [message, setMessage] = useState<string | null>(null)
  const { value, error } = useRequest<RuleResults | null>(
    signal => getRuleResults(projectId, snapshotId, signal).catch((reason: unknown) => {
      if (reason instanceof ApiError && reason.code === 'ARCHITECTURE_RULES_NOT_FOUND') return null
      throw reason
    }),
    [projectId, snapshotId, revision, version],
  )
  async function declare() {
    setMessage(null)
    try {
      const parsed: unknown = JSON.parse(draft)
      const created = await declareRules(projectId, Array.isArray(parsed) ? parsed : [])
      setMessage(`Declared rule set version ${created.version}.`)
      setVersion(item => item + 1)
    } catch (reason) {
      setMessage(reason instanceof SyntaxError ? 'The rules are not valid JSON.' : describe(reason))
    }
  }
  return <section className="rules">
    <h3>Declared rules <span className="badge intent-badge">Intent</span></h3>
    <p className="note">Rules you declare are intent, stored as immutable versions. They are checked against the observed graph and never enforced.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {value === null && <p className="note">No rule set has been declared for this project.</p>}
    {value && <><p className="note">Version {value.rule_set_version} · result key <code>{value.result_key.slice(0, 12)}</code></p>
      <ul className="edge-list">{value.results.map(item => <li key={String(item.rule.name)}>
        <strong>{visible(String(item.rule.name))}</strong> <span className={`badge conformance-${item.outcome}`}>Conformance: {OUTCOME_TEXT[item.outcome]}</span>
        {item.coverage_qualifier.length > 0 && <span className="note"> · {item.coverage_qualifier.join(', ')}</span>}
        {item.witnesses.length > 0 && <ul>{item.witnesses.map(edge => <li key={edge.key}>
          <button className="link-button" onClick={() => onSelect(edge.source_key)}>{label(edge.source_key)}</button> → {label(edge.target_key ?? '')} <TierBadge tier={edge.tier} />
        </li>)}{item.witness_total > item.witnesses.length && <li className="note">+{item.witness_total - item.witnesses.length} more</li>}</ul>}
      </li>)}</ul></>}
    <details><summary>Declare a new version</summary>
      <textarea aria-label="Rules JSON" rows={6} value={draft} onChange={event => setDraft(event.target.value)} />
      <button onClick={() => void declare()}>Declare version</button>
      {message && <p className="note" role="status">{message}</p>}
    </details>
  </section>
}

export function SubsystemsView(props: Props) {
  const { value, error } = useView(props, 'subsystems')
  if (!value) return <Status error={error} />
  const layers = value.nodes.filter(node => node.kind === 'layer_candidate')
  const slices = value.nodes.filter(node => node.kind === 'functional_slice')
  const slicesCoverage = value.coverage.slices as Record<string, unknown> | undefined
  return <div className="views">
    <h3>Layer candidates <span className="badge tier-badge tier-inferred">Inferred</span></h3>
    <p className="note">Observed dependency direction between sibling containers, ranked from top to bottom. This is not intended architecture.</p>
    {layers.map(item => <article key={item.key} className="fact">
      <p><strong>{visible(item.name)}</strong></p>
      <ol>{(item.attributes.order as string[][]).map((rank, index) => <li key={index} className="layer-rank">{rank.map(key => <button key={key} className="link-button" onClick={() => props.onSelect(key)}>{label(key).split('/').pop()}</button>)}</li>)}</ol>
      {(item.attributes.cyclic_groups as string[][]).length > 0 && <p className="note">Mutually dependent, so no order: {(item.attributes.cyclic_groups as string[][]).map(group => group.map(key => label(key).split('/').pop()).join(' ↔ ')).join('; ')}</p>}
      {(item.attributes.inversions as { from: string; to: string; count: number }[]).map(inversion => <p key={`${inversion.from}|${inversion.to}`} className="note">Against naming convention: {label(inversion.from)} → {label(inversion.to)} ({inversion.count})</p>)}
    </article>)}
    <h3>Functional slice candidates <span className="badge tier-badge tier-inferred">Inferred</span></h3>
    <p className="note">Grouped from imports, file names and API candidates. There is no call graph, so this is not a complete subsystem model.</p>
    <Counts value={slicesCoverage} />
    <ul className="edge-list">{slices.map(item => <li key={item.key}>
      <button className="link-button" onClick={() => props.onSelect(item.key)}>{visible(item.name)}</button>
      <span className="note"> · {String(item.attributes.exclusive)} exclusive · {String(item.attributes.shared)} shared{(item.attributes.grouping as string[]).length ? ` · grouped by ${(item.attributes.grouping as string[]).join(', ')}` : ''}</span>
    </li>)}</ul>
    <Diagnostics items={value.diagnostics} onSelect={props.onSelect} />
    <Rules {...props} />
  </div>
}
