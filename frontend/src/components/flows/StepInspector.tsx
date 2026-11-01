import { useMemo } from 'react'
import { getEntity } from '../../api/semantic'
import type { FlowTrace } from '../../api/flow'
import { useRequest } from '../architecture/useRequest'
import { SourceView } from '../explore/SourceView'
import { visible } from '../explore/visible'
import { FEASIBILITY_TEXT, REACHABILITY_TEXT, atomText, explained, hopsById, relation, shown } from './format'

type Props = { projectId: string; snapshotId: string; trace: FlowTrace; step: string }

/** One step's four verdicts, kept apart, plus the stored source of the scope it belongs to. */
export function StepInspector({ projectId, snapshotId, trace, step }: Props) {
  const hops = useMemo(() => hopsById(trace), [trace])
  const selected = trace.steps.find(item => item.id === step)
  const from = selected ? hops.get(selected.from) : undefined
  const to = selected?.to ? hops.get(selected.to) : undefined
  const scope = [from, to].find(hop => hop?.identity)
  const identity = from?.identity ?? null
  const source = useRequest(
    signal => identity ? explained(getEntity(projectId, snapshotId, `symbol:${identity}`, signal)) : Promise.resolve(null),
    [projectId, snapshotId, identity],
  )
  if (!selected) return <aside className="explore-column inspector" aria-label="Step inspector"><p className="note">Select a step to inspect its verdicts and evidence.</p></aside>
  const cited = new Set<number>(selected.line ? [selected.line] : [])
  return <aside className="explore-column inspector" aria-label="Step inspector">
    <h2>{selected.id} · {relation(selected.relation)}</h2>
    <p className="note">{shown(from?.label)} → {shown(to?.label ?? '(unknown)')} · lane <code>{shown(selected.lane)}</code>{to ? ` · ${to.layer}` : ''}</p>
    <dl className="verdict-list">
      <dt>L1 resolution</dt>
      <dd>{selected.resolution.tier} · <code>{selected.resolution.rule}</code>{selected.resolution.conditions.length > 0 && <> · {selected.resolution.conditions.map(shown).join(' · ')}</>}</dd>
      <dt>L2 reachability</dt>
      <dd>{REACHABILITY_TEXT[selected.reachability.verdict] ?? selected.reachability.verdict}{selected.reachability.raised_at_cfg_nodes?.length ? ` (from CFG nodes ${selected.reachability.raised_at_cfg_nodes.join(', ')})` : ''}</dd>
      <dt>L3 path condition</dt>
      <dd>{selected.path_condition.terms.length === 0 ? 'unguarded from the entry' : selected.path_condition.terms.map(term => term.map(atomText).join(' and ')).join(' or ')}{selected.path_condition.approximate && ' (approximate)'}
        <div className="note">A structural path in the over-approximate model; not an execution.</div></dd>
      <dt>L4 feasibility</dt>
      <dd>{FEASIBILITY_TEXT[selected.feasibility.verdict]} <span className="note">({selected.feasibility.fragment}; assumes {selected.feasibility.assumptions.join(', ')})</span></dd>
    </dl>
    {Object.keys(selected.attributes).length > 0 && <details><summary>Edge attributes</summary><pre className="source">{visible(JSON.stringify(selected.attributes, null, 2))}</pre></details>}
    <p className="note">{selected.evidence.length} verified evidence item{selected.evidence.length === 1 ? '' : 's'} · edge <code>{shown(selected.edge).slice(0, 80)}</code></p>
    {source.error && <p className="error" role="alert">{source.error}</p>}
    {scope && identity && source.value && <SourceView detail={source.value} cited={cited} />}
    {!identity && <p className="note">This step's scope has no catalog entity (a module, frontend callback or framework node).</p>}
  </aside>
}
