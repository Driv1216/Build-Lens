import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import { analyzeImpact, impactCoverage, interpretImpact, loadImpact, prepareImpact, verifyImpact } from '../../api/impact'
import type { Coverage, ImpactInput, ImpactOperation, ImpactReport, ImpactSelection, Interpretation, Preparation, ImpactVerification } from '../../api/impact'
import { ApiError } from '../../api/client'
import { listProjects } from '../../api/projects'
import { latestSnapshot } from '../../api/snapshots'
import { useLatestRequest, useRequest } from '../architecture/useRequest'

type Mode = ImpactInput['mode'] | 'proposal'
const OPERATIONS: { id: ImpactOperation['kind']; label: string }[] = [
  { id: 'require_parameter', label: 'Require a parameter' },
  { id: 'remove_parameter', label: 'Remove a parameter' },
  { id: 'remove_return_field', label: 'Remove a return field' },
  { id: 'set_return_boolean', label: 'Change a boolean return field' },
  { id: 'set_integer_threshold', label: 'Change an integer threshold' },
  { id: 'rename_symbol', label: 'Rename a function declaration' },
  { id: 'remove_symbol', label: 'Remove a function declaration' },
  { id: 'remove_api_field', label: 'Remove an API field' },
  { id: 'require_api_field', label: 'Require an API field' },
  { id: 'rename_api_field', label: 'Rename an API field' },
  { id: 'set_endpoint_path', label: 'Change an endpoint path' },
  { id: 'set_endpoint_method', label: 'Change an endpoint method' },
  { id: 'remove_database_column', label: 'Remove a mapped column' },
  { id: 'set_database_constraint', label: 'Change an explicit column constraint' },
  { id: 'set_configuration_default', label: 'Change an environment default' },
]

const words = (value: string) => value.replaceAll('_', ' ')
const describe = (reason: unknown) => reason instanceof ApiError
  ? `${reason.message} (${words(reason.code)})`
  : 'The impact request could not be completed.'

export function ImpactPage() {
  const [params, setParams] = useSearchParams()
  const projects = useRequest(signal => listProjects(signal), [])
  const project = params.get('project') || projects.value?.[0]?.id || ''
  const selected = params.get('snapshot') ?? ''
  const latest = useRequest(signal => project && !selected ? latestSnapshot(project, signal) : Promise.resolve(null),
    [project, selected])
  const snapshot = selected || latest.value?.id || ''

  return <main className="shell">
    <header>
      <h1>Engineering impact</h1>
      <nav className="app-nav"><Link to="/">Projects</Link><Link to="/explore">Explore</Link><Link to="/flows">Flow traces</Link></nav>
    </header>
    <section className="card">
      <label>Project<select value={project} onChange={event => setParams({ project: event.target.value })}>
        <option value="">Choose a project</option>
        {projects.value?.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
      <label>Baseline snapshot
        <input value={selected} placeholder={snapshot || 'Latest snapshot ID'} onChange={event => {
          const next = new URLSearchParams(params)
          next.set('project', project)
          if (event.target.value) next.set('snapshot', event.target.value)
          else next.delete('snapshot')
          setParams(next, { replace: true })
        }} />
      </label>
      {(projects.error || latest.error) && <p role="alert">{projects.error || latest.error}</p>}
    </section>
    {project && snapshot && <Workbench key={`${project}/${snapshot}/${params.get('entity') || ''}`} project={project}
      snapshot={snapshot} initialEntity={params.get('entity') ?? ''} />}
  </main>
}

function Workbench({ project, snapshot, initialEntity }: { project: string; snapshot: string; initialEntity: string }) {
  const [mode, setMode] = useState<Mode>('entity')
  const [flowRun, setFlowRun] = useState('')
  const [after, setAfter] = useState('')
  const [ruleVersion, setRuleVersion] = useState('')
  const [entity, setEntity] = useState(initialEntity)
  const [locator, setLocator] = useState('')
  const [patch, setPatch] = useState('')
  const [text, setText] = useState('')
  const [target, setTarget] = useState('')
  const [kind, setKind] = useState<ImpactOperation['kind']>('require_parameter')
  const [member, setMember] = useState('')
  const [value, setValue] = useState('')
  const [newName, setNewName] = useState('')
  const [family, setFamily] = useState<ImpactOperation['family']>('response')
  const [constraint, setConstraint] = useState<ImpactOperation['constraint']>('nullable')
  const [valueType, setValueType] = useState('string')
  const [save, setSave] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [savedId, setSavedId] = useState('')
  const [seal, setSeal] = useState(false)
  const [coverage, setCoverage] = useState<Coverage | null>(null)
  const [preparation, setPreparation] = useState<Preparation | null>(null)
  const [interpretation, setInterpretation] = useState<Interpretation | null>(null)
  const [report, setReport] = useState<ImpactReport | null>(null)
  const [verification, setVerification] = useState<ImpactVerification | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const { run, cancel } = useLatestRequest()
  const before: ImpactSelection = { snapshot_id: snapshot, ...(flowRun ? { flow_run_id: flowRun } : {}) }
  const locators = locator.split('\n').map(item => item.trim()).filter(Boolean)
  const apiField = ['remove_api_field', 'require_api_field', 'rename_api_field'].includes(kind)
  const rename = kind === 'rename_symbol' || kind === 'rename_api_field'
  const needsMember = !['rename_symbol', 'remove_symbol', 'set_endpoint_path', 'set_endpoint_method', 'set_integer_threshold'].includes(kind)
  const booleanValue = kind === 'set_return_boolean' || kind === 'set_database_constraint'

  function execute<T>(load: (signal: AbortSignal) => Promise<T>, apply: (result: T) => void) {
    setBusy(true)
    setError('')
    void run(load, result => { setBusy(false); apply(result) }, reason => { setBusy(false); setError(describe(reason)) })
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setReport(null)
    setVerification(null)
    if (mode === 'proposal' && !interpretation) {
      execute(signal => interpretImpact(project, before, text, signal), setInterpretation)
      return
    }
    let request: ImpactInput
    if (mode === 'entity') request = { mode, before, entity }
    else if (mode === 'snapshots') request = { mode, before, after: { snapshot_id: after }, locators,
      ...(ruleVersion ? { architecture_rule_version: Number(ruleVersion) } : {}) }
    else if (mode === 'patch') request = { mode, before, patch }
    else {
      const operation: ImpactOperation | null = mode === 'proposal'
        ? interpretation?.candidate ?? null
        : { kind, target, member: needsMember ? member : '',
            ...(rename ? { new_name: newName } : {}),
            ...(apiField ? { family } : {}),
            ...(kind === 'set_database_constraint' ? { constraint } : {}),
            value: booleanValue ? value === 'true' : kind === 'set_integer_threshold' ? Number(value)
              : kind === 'set_configuration_default' ? valueType === 'null' ? null
                : valueType === 'boolean' ? value === 'true' : valueType === 'integer' ? Number(value) : value
              : kind === 'set_endpoint_path' || kind === 'set_endpoint_method' ? value : null }
      if (!confirm || !operation || locators.length !== 1) return
      request = { mode: 'hypothesis', before, locators, operations: [operation], confirm: true }
    }
    execute(signal => analyzeImpact(project, request, save, signal), setReport)
  }

  return <>
    <section className="card">
      <h2>Analyze a change</h2>
      <form onSubmit={submit} onChange={() => {
        setCoverage(null)
        if (busy) { cancel(); setBusy(false) }
      }}>
        <label>Input type<select value={mode} onChange={event => {
          cancel(); setBusy(false); setMode(event.target.value as Mode); setReport(null); setInterpretation(null); setConfirm(false)
        }}>
          <option value="entity">Entity exposure</option>
          <option value="snapshots">Snapshot comparison</option>
          <option value="hypothesis">Structured hypothesis</option>
          <option value="patch">Exact patch</option>
          <option value="proposal">Developer proposal</option>
        </select></label>
        <label>Historical flow run ID<input value={flowRun} placeholder="Use the active run" onChange={event => {
          setFlowRun(event.target.value); setInterpretation(null); setConfirm(false); setReport(null)
        }} /></label>
        {mode === 'entity' && <label>Entity or flow key<input required value={entity} onChange={event => setEntity(event.target.value)} /></label>}
        {mode === 'snapshots' && <>
          <label>After snapshot ID<input required value={after} onChange={event => setAfter(event.target.value)} /></label>
          <label>Architecture rule version<input type="number" min="1" max="2147483647" step="1"
            value={ruleVersion} onChange={event => setRuleVersion(event.target.value)} /></label>
          {ruleVersion && <p>Choose 1–16 source paths for the rule comparison.</p>}
        </>}
        {mode === 'patch' && <label>Unified diff<textarea required value={patch} rows={10} onChange={event => setPatch(event.target.value)} /></label>}
        {mode === 'hypothesis' && <>
          <label>Operation<select value={kind} onChange={event => {
            setKind(event.target.value as ImpactOperation['kind']); setConfirm(false); setValue('')
          }}>{OPERATIONS.map(operation => <option key={operation.id} value={operation.id}>{operation.label}</option>)}</select></label>
          <label>Target<input required value={target} onChange={event => { setTarget(event.target.value); setConfirm(false) }} /></label>
          {needsMember && <label>Parameter, field or environment key<input required value={member} onChange={event => { setMember(event.target.value); setConfirm(false) }} /></label>}
          {rename && <label>New name<input required value={newName} onChange={event => { setNewName(event.target.value); setConfirm(false) }} /></label>}
          {apiField && <label>API contract<select value={family} onChange={event => { setFamily(event.target.value as ImpactOperation['family']); setConfirm(false) }}>
            <option value="query">Query</option><option value="body">Request body</option><option value="response">Response</option>
          </select></label>}
          {kind === 'set_database_constraint' && <label>Column constraint<select value={constraint} onChange={event => { setConstraint(event.target.value as ImpactOperation['constraint']); setConfirm(false) }}>
            <option value="nullable">Nullable</option><option value="unique">Unique</option><option value="primary_key">Primary key</option>
          </select></label>}
          {kind === 'set_integer_threshold' && <label>Threshold<input required type="number" step="1" value={value} onChange={event => { setValue(event.target.value); setConfirm(false) }} /></label>}
          {booleanValue && <label>Value<select value={value} required onChange={event => { setValue(event.target.value); setConfirm(false) }}>
            <option value="">Choose a value</option><option value="true">True</option><option value="false">False</option>
          </select></label>}
          {kind === 'set_endpoint_path' && <label>Endpoint path<input required value={value} onChange={event => { setValue(event.target.value); setConfirm(false) }} /></label>}
          {kind === 'set_endpoint_method' && <label>HTTP method<select required value={value} onChange={event => { setValue(event.target.value); setConfirm(false) }}>
            <option value="">Choose a method</option>{['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'].map(method => <option key={method}>{method}</option>)}
          </select></label>}
          {kind === 'set_configuration_default' && <>
            <label>Default type<select value={valueType} onChange={event => { setValueType(event.target.value); setValue(''); setConfirm(false) }}>
              {['string', 'integer', 'boolean', 'null'].map(type => <option key={type}>{type}</option>)}
            </select></label>
            {valueType !== 'null' && <label>Default value{valueType === 'boolean'
              ? <select value={value} required onChange={event => { setValue(event.target.value); setConfirm(false) }}><option value="">Choose a value</option><option value="true">True</option><option value="false">False</option></select>
              : <input value={value} type={valueType === 'integer' ? 'number' : 'text'} step={valueType === 'integer' ? '1' : undefined}
                  onChange={event => { setValue(event.target.value); setConfirm(false) }} />}</label>}
          </>}
        </>}
        {mode === 'proposal' && <label>Proposal<textarea required maxLength={2048} value={text} rows={3} placeholder="Require parameter notify in reserve"
          onChange={event => { setText(event.target.value); setInterpretation(null); setConfirm(false) }} /></label>}
        {mode === 'proposal' && interpretation && <div>
          {interpretation.candidate && <p>{words(interpretation.candidate.kind)} <code>{interpretation.candidate.member} {interpretation.candidate.target}</code></p>}
          {interpretation.targets.map(item => <button key={item.key} type="button" onClick={() => { setLocator(item.locator); setConfirm(false) }}>{item.locator}{item.line ? `:${item.line}` : ''}</button>)}
          {interpretation.unknowns.map(reason => <p key={reason}>{words(reason)}</p>)}
        </div>}
        {mode !== 'entity' && mode !== 'patch' && <label>Source paths<textarea value={locator} rows={2} placeholder="One repository-relative path per line"
          onChange={event => { setLocator(event.target.value); setConfirm(false) }} /></label>}
        {(mode === 'hypothesis' || (mode === 'proposal' && interpretation?.candidate)) &&
          <label><input type="checkbox" checked={confirm} onChange={event => setConfirm(event.target.checked)} /> Confirm this hypothesis</label>}
        <label><input type="checkbox" checked={save} onChange={event => setSave(event.target.checked)} /> Save the verified report</label>
        <div className="app-nav">
          <button disabled={busy || (mode === 'hypothesis' && !confirm) || (mode === 'proposal' && Boolean(interpretation) && !confirm)
            || (mode === 'snapshots' && Boolean(ruleVersion) && (new Set(locators).size === 0 || new Set(locators).size > 16))}>
            {mode === 'proposal' && !interpretation ? 'Interpret proposal' : 'Analyze'}
          </button>
          {busy && <button type="button" onClick={() => { cancel(); setBusy(false) }}>Cancel</button>}
        </div>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {busy && <p role="status">Working with the selected evidence…</p>}
    </section>
    <section className="card">
      <h2>Prepare evidence</h2>
      <button type="button" disabled={busy || locators.length > 1}
        onClick={() => execute(signal => impactCoverage(project, before, locators[0], mode === 'snapshots' ? after : '', signal), setCoverage)}>Check capabilities</button>
      {coverage && <div role="status">
        {coverage.supplemental_comparison && <p>Syntax producer · {words(coverage.supplemental_comparison)}</p>}
        <ul>{Object.entries(coverage.sides).map(([side, value]) => <li key={side}>
          {side} · declarations {words(value.declarations)} · relationship exposure {words(value.relationship_exposure)} · supplemental facts {words(value.supplemental_properties)}
          {Object.keys(value.properties).length > 0 && <ul>{Object.entries(value.properties).map(([property, state]) =>
            <li key={property}>{words(property)} · {words(state).toLowerCase()}</li>)}</ul>}
        </li>)}</ul>
        {Object.keys(coverage.property_comparison).length > 0 && <details open>
          <summary>Snapshot property comparison</summary>
          <ul>{Object.entries(coverage.property_comparison).map(([property, state]) =>
            <li key={property}>{words(property)} · {words(state).toLowerCase()}</li>)}</ul>
        </details>}
      </div>}
      <label>Source paths<textarea value={locator} rows={2} placeholder="One repository-relative path per line"
        onChange={event => { setLocator(event.target.value); setConfirm(false); setPreparation(null) }} /></label>
      <label><input type="checkbox" checked={seal} onChange={event => setSeal(event.target.checked)} /> Seal indexed flow slices</label>
      <button type="button" disabled={busy || (!locators.length && !seal)}
        onClick={() => execute(signal => prepareImpact(project, before, locators, seal, signal), setPreparation)}>Prepare baseline</button>
      {after && <button type="button" disabled={busy || (!locators.length && !seal)} onClick={() =>
        execute(signal => prepareImpact(project, { snapshot_id: after }, locators, seal, signal), setPreparation)}>Prepare after snapshot</button>}
      {preparation && <div role="status">
        <p>{words(preparation.status)} · {preparation.completed.length} artifacts prepared{preparation.seal ? ' · Flow slices sealed' : ''}</p>
        {preparation.gaps.map(item => <p key={item.locator}>{item.locator} · {words(item.reason)}</p>)}
      </div>}
    </section>
    <section className="card">
      <h2>Open a saved report</h2>
      <form onSubmit={event => { event.preventDefault(); setVerification(null); execute(signal => loadImpact(project, savedId, signal), setReport) }}>
        <label>Result digest<input required pattern="[a-f0-9]{64}" value={savedId} onChange={event => setSavedId(event.target.value)} /></label>
        <button disabled={busy}>Open report</button>
      </form>
    </section>
    {report && <>
      <button type="button" disabled={busy} onClick={() => execute(
        signal => verifyImpact(project, report.result_digest, signal), setVerification)}>Verify saved evidence</button>
      {verification && <div role="status">
        <p>Evidence integrity · {words(verification.integrity)}</p>
        <p>Current authority · {words(verification.freshness)}</p>
        <p>Analytical contract · {words(verification.compatibility)}</p>
        <p>Evidence verification · {words(verification.evidence_verification)}</p>
        <p>Analysis completeness · {words(verification.analysis_completeness)}</p>
        {verification.reasons.map(reason => <p key={reason}>{words(reason)}</p>)}
      </div>}
      <Report key={report.result_digest} report={report} />
    </>}
  </>
}

function Report({ report }: { report: ImpactReport }) {
  const [limit, setLimit] = useState(30)
  return <section className="card">
    <h2>Impact report</h2>
    <p><strong>{words(report.status)}</strong> · {report.changes.length} changes · {report.findings.length} findings</p>
    <p><code>{report.result_digest}</code></p>
    {report.architecture_rules && <p>Architecture rule version {report.architecture_rules.version} · <code>{report.architecture_rules.digest}</code></p>}
    {report.gaps.length > 0 && <details open><summary>Evidence gaps</summary>
      <ul>{report.gaps.map(gap => <li key={gap}>{words(gap)}</li>)}</ul>
    </details>}
    {report.changes.length > 0 && <h3>Changes</h3>}
    {report.changes.slice(0, limit).map(change => <details key={change.id}>
      <summary>{change.entity || change.locator}{change.field ? ` · ${change.field}` : ''} · {words(change.property)} · {words(change.comparison)}</summary>
      <p><code>{change.locator}</code></p>
      <div className="impact-before-after">
        <div><h4>Before</h4><pre className="source">{JSON.stringify(change.before, null, 2)}</pre></div>
        <div><h4>After</h4><pre className="source">{JSON.stringify(change.after, null, 2)}</pre></div>
      </div>
    </details>)}
    {report.findings.length > 0 && <h3>Findings</h3>}
    {report.findings.slice(0, limit).map((finding, index) => <details key={`${finding.origin}/${finding.entity}/${index}`}>
      <summary>{finding.label || finding.entity} · {words(finding.consequence)}</summary>
      <p>{finding.side ? `${finding.side} · ` : ''}{finding.distance} · {words(finding.mechanism)} · {finding.relationship_tier}</p>
      {finding.priority && <p>{words(finding.priority.level)} · {finding.priority.reasons.map(reason => words(reason).toLowerCase()).join(', ')}</p>}
      {finding.locator && <p><code>{finding.locator}</code></p>}
      <pre className="source">{JSON.stringify(finding.witnesses || finding.witness, null, 2)}</pre>
    </details>)}
    {(report.tests.length > 0 || report.verification_gaps.length > 0) && <h3>Verification</h3>}
    {report.tests.slice(0, limit).map(item => <details key={`${item.origin}/${item.test}`}>
      <summary>{item.test} · {item.property_assertions.length ? 'Property assertion found' : 'Potential execution'}</summary>
      <p><code>{item.locator}</code> · Condition-specific execution {words(item.condition_specific_execution)}</p>
      {item.matching_input !== undefined && <p>Literal input in the changed region: <code>{item.matching_input}</code></p>}
      {item.property_assertions.length > 0 && <pre className="source">{JSON.stringify(item.property_assertions, null, 2)}</pre>}
    </details>)}
    {report.verification_gaps.slice(0, limit).map(item => <p key={item.origin}>
      {words(item.property)} · <code>{item.locator}</code> · {words(item.reason).toLowerCase()}
    </p>)}
    {report.frontier.length > 0 && <details><summary>Unfinished analysis frontier</summary>
      <pre className="source">{JSON.stringify(report.frontier.slice(0, limit), null, 2)}</pre>
    </details>}
    {Math.max(report.changes.length, report.findings.length, report.frontier.length,
      report.tests.length, report.verification_gaps.length) > limit &&
      <button type="button" onClick={() => setLimit(current => current + 30)}>Show more</button>}
  </section>
}
