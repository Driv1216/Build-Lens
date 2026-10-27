import type { LabItem, LabStep } from '../../api/lab'

const KIND_COLORS: Record<string, string> = {
  retrieve: '#7aa2ff',
  embed: '#b48cff',
  rerank: '#ff9f6b',
  fuse: '#5fd4c4',
  postprocess: '#e4c56b',
  expand: '#8fd18f',
  merge: '#8fd18f',
  generate: '#ff7d9c',
  parse: '#ffb3c6',
  validate: '#c3e88d',
  tool: '#f7a8ff',
  agent: '#d7a8ff',
  bridge: '#5c6f91',
  store: '#9aa7bd',
  split: '#c0a5ff',
  pack: '#9fe0ff',
  prompt: '#ffd28a',
}

export function TierBadge({ tier }: { tier: string }) {
  const label: Record<string, string> = {
    approved: 'Approved (R1)',
    span: 'Span coverage',
    derived: 'Derived · not human-approved',
    constructed: 'Constructed · synthetic',
  }
  return <span className={`tier tier-${tier}`}>{label[tier] ?? tier}</span>
}

/** Step waterfall: bars are laid out sequentially by step order with widths by duration. */
export function Waterfall({ steps }: { steps: LabStep[] }) {
  const shown = steps.slice(0, 60)
  const total = Math.max(1, shown.reduce((sum, step) => sum + Math.max(step.duration_ms, 0.05), 0))
  const width = 640
  let cursor = 0
  return (
    <svg className="waterfall" role="img" aria-label="Pipeline step waterfall" viewBox={`0 0 ${width} ${shown.length * 18 + 4}`}>
      {shown.map((step, index) => {
        const w = Math.max(2, (Math.max(step.duration_ms, 0.05) / total) * (width - 230))
        const x = 220 + (cursor / total) * (width - 230)
        cursor += Math.max(step.duration_ms, 0.05)
        return (
          <g key={`${step.seq}-${index}`}>
            <title>{`${step.kind} · ${step.name} · ${step.framework_event} · ${step.duration_ms} ms`}</title>
            <text x={step.parent ? 14 : 4} y={index * 18 + 13} className="waterfall-label">
              {step.name.slice(0, 32)}
            </text>
            <rect x={x} y={index * 18 + 3} width={w} height={12} rx={3} fill={KIND_COLORS[step.kind] ?? '#8b97ab'} />
          </g>
        )
      })}
    </svg>
  )
}

/** Lines connect the same evidence across two ranked lists (first 20 positions). */
export function RankAlignment({ left, right }: { left: LabItem[]; right: LabItem[] }) {
  const key = (item: LabItem) => item.chunk_id ?? `${item.locator}:${item.span?.start_offset}`
  const a = left.slice(0, 20)
  const b = right.slice(0, 20)
  const index = new Map(b.map((item, i) => [key(item), i]))
  return (
    <svg className="alignment" role="img" aria-label="Rank alignment" viewBox="0 0 220 362">
      {a.map((item, i) => {
        const j = index.get(key(item))
        return j === undefined ? (
          <circle key={i} cx={20} cy={i * 18 + 10} r={3} fill="#5c6f91" />
        ) : (
          <line key={i} x1={20} y1={i * 18 + 10} x2={200} y2={j * 18 + 10} stroke={j === i ? '#8fd18f' : '#e4c56b'} strokeWidth={1.5} />
        )
      })}
      {b.map((_, j) => (
        <circle key={`r${j}`} cx={200} cy={j * 18 + 10} r={3} fill="#7aa2ff" />
      ))}
    </svg>
  )
}

/** Two lanes over the same artifact: BuildLens chunks vs framework nodes. */
export function BoundaryLanes({
  length,
  native,
  framework,
}: {
  length: number
  native: [number, number][]
  framework: [number | null, number | null, string][]
}) {
  const width = 900
  const scale = (offset: number) => (offset / Math.max(length, 1)) * width
  return (
    <svg className="boundaries" role="img" aria-label="Chunk boundary comparison" viewBox={`0 0 ${width} 70`}>
      <text x={0} y={10} className="waterfall-label">BuildLens chunks</text>
      {native.map(([start, end], i) => (
        <rect key={`n${i}`} x={scale(start)} y={14} width={Math.max(1, scale(end) - scale(start))} height={16} fill={i % 2 ? '#3d5a99' : '#5f7fc4'} />
      ))}
      <text x={0} y={46} className="waterfall-label">Framework nodes</text>
      {framework.map(([start, end, method], i) =>
        start === null || end === null ? null : (
          <rect key={`f${i}`} x={scale(start)} y={50} width={Math.max(1, scale(end) - scale(start))} height={16} fill={method === 'composite' ? '#e4c56b' : i % 2 ? '#3e8a6d' : '#5fbf98'} opacity={0.85} />
        ),
      )}
    </svg>
  )
}
