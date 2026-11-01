import { useMemo } from 'react'
import type { FlowTrace, TraceStep } from '../../api/flow'
import { TierBadge } from '../architecture/parts'
import { layered } from '../architecture/layout'
import { FEASIBILITY_TEXT, REACHABILITY_TEXT, atomText, hopsById, relation, shown } from './format'

const HTTP_CAVEAT = 'HTTP_LINK_INDEPENDENT_EVALUATION_PENDING'

type Common = { trace: FlowTrace; selected: string; onSelect: (step: string) => void }

export function VerdictChips({ step }: { step: TraceStep }) {
  const reach = step.reachability.verdict
  return <span className="verdicts">
    <TierBadge tier={step.resolution.tier} />{' '}
    <span className={`chip reach-${reach}`} title="L2 static reachability in the model">L2 {REACHABILITY_TEXT[reach] ?? reach}</span>{' '}
    <span className={`chip feasibility-${step.feasibility.verdict}`} title="L4 feasibility in the sound fragment">L4 {FEASIBILITY_TEXT[step.feasibility.verdict]}</span>
  </span>
}

/** The primary view: steps in walk order, indented by depth, with the four verdicts apart. */
export function TraceOutline({ trace, selected, onSelect, hideRefuted }: Common & { hideRefuted: boolean }) {
  const hops = useMemo(() => hopsById(trace), [trace])
  const shown_ = hideRefuted ? trace.steps.filter(step => step.feasibility.verdict !== 'refuted_in_fragment') : trace.steps
  const hidden = trace.steps.length - shown_.length
  return <section aria-label="Trace outline">
    {hidden > 0 && <p className="note">{hidden} refuted step{hidden === 1 ? '' : 's'} hidden by the filter; the trace keeps them.</p>}
    <ol className="trace-outline">
      {shown_.map(step => {
        const from = hops.get(step.from)
        const to = step.to ? hops.get(step.to) : undefined
        return <li key={step.id} className={`trace-step${selected === step.id ? ' selected' : ''}`} style={{ paddingLeft: `${(from?.depth ?? 0) * 14}px` }}>
          <button className="link-button" aria-label={`Step ${step.id}`} onClick={() => onSelect(step.id)}>{step.id}</button>{' '}
          <span className="kind">{relation(step.relation)}</span>{' '}
          <strong>{to ? shown(to.label) : step.relation === 'unresolved' ? '(unknown callee)' : '—'}</strong>{' '}
          <VerdictChips step={step} />
          {step.lane !== 'main' && <span className="chip lane"> lane {shown(step.lane)}</span>}
          {step.segment && <span className="badge warn"> alternative · {step.segment}</span>}
          {step.candidate && <span className="badge warn"> candidate</span>}
          {step.body_runs === false && <span className="badge"> body does not run</span>}
          {step.shared && <span className="note"> (seen above)</span>}
          {step.cycle_ref && <span className="badge warn"> cycle</span>}
          {step.resolution.conditions.includes(HTTP_CAVEAT) && <span className="badge warn"> HTTP link not independently evaluated</span>}
          {step.resolution.conditions.length > 0 && <span className="note conditions"> {step.resolution.conditions.filter(c => c !== HTTP_CAVEAT).map(shown).join(' · ')}</span>}
        </li>
      })}
    </ol>
  </section>
}

const COLUMN = 34
const ROW = 46

/** Lanes: one row per scheduler. Independent lanes claim no order unless a contract states
 * one; the diagram's left-to-right placement is the walk's layering, not time. */
export function LaneDiagram({ trace, selected, onSelect }: Common) {
  const hops = useMemo(() => hopsById(trace), [trace])
  const lanes = useMemo(() => [...trace.lanes].sort((a, b) => (a.id === 'main' ? -1 : b.id === 'main' ? 1 : a.id.localeCompare(b.id))), [trace])
  const layout = useMemo(() => layered(
    trace.hops.map(hop => hop.id),
    trace.steps.filter(step => step.to && !step.shared && !step.cycle_ref).map(step => ({ source: step.from, target: step.to as string })),
  ), [trace])
  const row = new Map(lanes.map((lane, index) => [lane.id, index]))
  const slots = new Map<string, number>()
  const placed = new Map<string, { x: number; y: number }>()
  for (const node of [...layout.nodes].sort((a, b) => a.layer - b.layer || a.order - b.order)) {
    const hop = hops.get(node.key)
    const laneIndex = row.get(hop?.lane ?? 'main') ?? 0
    const slot = `${laneIndex}:${node.layer}`
    const offset = slots.get(slot) ?? 0
    slots.set(slot, offset + 1)
    placed.set(node.key, { x: 140 + node.layer * COLUMN, y: 24 + laneIndex * ROW + Math.min(offset, 3) * 8 })
  }
  const width = 180 + Math.max(0, ...layout.nodes.map(n => n.layer)) * COLUMN
  const height = 40 + lanes.length * ROW
  return <section aria-label="Lane diagram">
    <ul className="lane-legend">{lanes.map(lane => <li key={lane.id}><code>{shown(lane.id)}</code> — {lane.order === 'ORDER_NOT_DETERMINED' ? 'order not determined' : lane.order.toLowerCase().replaceAll('_', ' ')}</li>)}</ul>
    <svg role="img" aria-label="Lanes of the trace" width={width} height={height} className="lane-diagram">
      {lanes.map((lane, index) => <g key={lane.id}>
        <line x1={0} x2={width} y1={14 + index * ROW + ROW - 4} y2={14 + index * ROW + ROW - 4} className="lane-rule" />
        <text x={4} y={30 + index * ROW} className="lane-label">{shown(lane.id).slice(0, 22)}</text>
      </g>)}
      {trace.steps.filter(step => step.to && placed.has(step.from) && placed.has(step.to)).map(step => {
        const a = placed.get(step.from) as { x: number; y: number }
        const b = placed.get(step.to as string) as { x: number; y: number }
        return <line key={step.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={`lane-edge tier-${step.resolution.tier}${step.feasibility.verdict === 'refuted_in_fragment' ? ' refuted' : ''}${selected === step.id ? ' selected' : ''}`} />
      })}
      {trace.hops.filter(hop => placed.has(hop.id)).map(hop => {
        const p = placed.get(hop.id) as { x: number; y: number }
        const step = trace.steps.find(item => item.to === hop.id && !item.shared)
        return <circle key={hop.id} cx={p.x} cy={p.y} r={5} className={`lane-hop layer-${hop.layer}`} onClick={() => step && onSelect(step.id)}>
          <title>{`${shown(hop.label)} · ${hop.layer} · ${shown(hop.lane)}`}</title>
        </circle>
      })}
    </svg>
    <ul className="sr-only" aria-label="Lane contents">{lanes.map(lane => <li key={lane.id}>{shown(lane.id)}: {trace.hops.filter(hop => hop.lane === lane.id).map(hop => shown(hop.label)).join(', ')}</li>)}</ul>
  </section>
}

/** Outcomes and where they continue on the client, with database effects on the path. */
export function ResponsePanel({ trace, onSelect }: Common) {
  const hops = useMemo(() => hopsById(trace), [trace])
  const outcomes = trace.hops.filter(hop => hop.kind === 'response_outcome')
  return <section aria-label="Responses">
    {outcomes.length === 0 && <p className="note">No HTTP response outcomes on this trace.</p>}
    {outcomes.length > 0 && <table className="response-table"><thead><tr><th>Outcome</th><th>Reached by</th><th>Client continuation</th></tr></thead><tbody>
      {outcomes.map(hop => {
        const reached = trace.steps.filter(step => step.to === hop.id && (step.relation === 'exits_with' || step.relation === 'raises_to'))
        const responds = trace.steps.filter(step => step.from === hop.id && step.relation === 'responds')
        return <tr key={hop.id}>
          <td><strong>{shown(hop.label)}</strong></td>
          <td>{reached.map(step => <div key={step.id}><button className="link-button" onClick={() => onSelect(step.id)}>{relation(step.relation)}</button> <span className="note">{step.resolution.conditions.map(shown).join(' · ')}</span></div>)}</td>
          <td>{responds.length === 0 ? <span className="note">no client continuation on this trace</span> : responds.map(step => <div key={step.id}>
            {String(step.attributes.continuation ?? '?').replaceAll('_', ' ')} at <code>{shown(step.to ? hops.get(step.to)?.label : '')}</code>{' '}
            {step.resolution.conditions.includes(HTTP_CAVEAT) && <span className="badge warn">HTTP link not independently evaluated</span>}
          </div>)}</td>
        </tr>
      })}
    </tbody></table>}
    <h3>Database effects on the path</h3>
    {trace.effects.length === 0 && <p className="note">No staged or executed writes on this trace.</p>}
    <ul>{trace.effects.map(effect => <li key={effect.step}>
      <button className="link-button" onClick={() => onSelect(effect.step)}>{effect.class}</button> on {effect.tables.length ? effect.tables.map(t => <code key={t}>{shown(t.replace(/^db_table:/, ''))}</code>) : <span className="note">table unknown</span>}
      {' '}· <strong>{effect.state.replaceAll('_', ' ')}</strong>
      {effect.commit_position && <span className={`badge${effect.commit_position.code === 'COMMIT_AFTER_RESPONSE_SENT' ? ' warn' : ''}`}> {effect.commit_position.code === 'COMMIT_AFTER_RESPONSE_SENT' ? 'the client may observe success before the commit' : effect.commit_position.code.toLowerCase().replaceAll('_', ' ')}</span>}
      <span className="note"> — {effect.claim}{effect.conditions.length ? ` (${effect.conditions.join(', ')})` : ''}</span>
    </li>)}</ul>
  </section>
}

/** Control-dependence guards per step, decided only in the sound fragment. */
export function BranchPanel({ trace, onSelect }: Common) {
  const hops = useMemo(() => hopsById(trace), [trace])
  const guarded = trace.steps.filter(step => step.path_condition.terms.length > 0 && !step.shared)
  return <section aria-label="Branches">
    <p className="note">Guards are control dependences accumulated from the entry. Opaque conditions are fresh unknowns and are never used to refute; one activation takes one outcome at a non-loop branch.</p>
    {guarded.length === 0 && <p className="note">No guarded steps on this trace.</p>}
    <ul className="branch-list">{guarded.map(step => <li key={step.id}>
      <button className="link-button" onClick={() => onSelect(step.id)}>{step.id}</button> {relation(step.relation)} <strong>{shown(step.to ? hops.get(step.to)?.label : '')}</strong>{' '}
      <span className={`chip feasibility-${step.feasibility.verdict}`}>{FEASIBILITY_TEXT[step.feasibility.verdict]}</span>
      {step.path_condition.approximate && <span className="badge warn"> guard set approximate</span>}
      <div className="note">{step.path_condition.terms.map(term => term.map(atomText).join(' and ')).join('  or  ')}</div>
    </li>)}</ul>
  </section>
}
