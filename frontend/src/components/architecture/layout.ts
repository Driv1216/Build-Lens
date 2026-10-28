// Deterministic layered layout for small dependency neighbourhoods (no layout library).
// Strongly connected members share a layer; layers come from longest paths on the
// condensation; order within a layer uses two barycentre sweeps with key tie-breaks.

export type LayoutEdge = { source: string; target: string }
export type Placed = { key: string; layer: number; order: number; x: number; y: number }
export type Layout = { nodes: Placed[]; width: number; height: number }

const COLUMN = 220
const ROW = 46
const MARGIN = 24

function components(keys: string[], edges: LayoutEdge[]): Map<string, number> {
  const out = new Map<string, string[]>(keys.map(key => [key, []]))
  for (const edge of edges) if (out.has(edge.source) && out.has(edge.target)) out.get(edge.source)?.push(edge.target)
  for (const list of out.values()) list.sort()
  const index = new Map<string, number>()
  const low = new Map<string, number>()
  const owner = new Map<string, number>()
  const stack: string[] = []
  const onStack = new Set<string>()
  let counter = 0
  let component = 0
  for (const root of [...keys].sort()) {
    if (index.has(root)) continue
    const work: [string, number][] = [[root, 0]]
    while (work.length) {
      const frame = work[work.length - 1]
      const [node, position] = frame
      if (position === 0 && !index.has(node)) {
        index.set(node, counter); low.set(node, counter); counter += 1
        stack.push(node); onStack.add(node)
      }
      const next = out.get(node) ?? []
      if (position < next.length) {
        frame[1] = position + 1
        const target = next[position]
        if (!index.has(target)) work.push([target, 0])
        else if (onStack.has(target)) low.set(node, Math.min(low.get(node) ?? 0, index.get(target) ?? 0))
        continue
      }
      work.pop()
      if (work.length) {
        const parent = work[work.length - 1][0]
        low.set(parent, Math.min(low.get(parent) ?? 0, low.get(node) ?? 0))
      }
      if (low.get(node) === index.get(node)) {
        let member = ''
        do {
          member = stack.pop() ?? node
          onStack.delete(member)
          owner.set(member, component)
        } while (member !== node)
        component += 1
      }
    }
  }
  return owner
}

export function layered(keys: string[], edges: LayoutEdge[]): Layout {
  const sorted = [...new Set(keys)].sort()
  const owner = components(sorted, edges)
  const successors = new Map<number, Set<number>>()
  const indegree = new Map<number, number>()
  for (const value of owner.values()) { successors.set(value, successors.get(value) ?? new Set()); indegree.set(value, indegree.get(value) ?? 0) }
  for (const edge of edges) {
    const a = owner.get(edge.source); const b = owner.get(edge.target)
    if (a === undefined || b === undefined || a === b || successors.get(a)?.has(b)) continue
    successors.get(a)?.add(b); indegree.set(b, (indegree.get(b) ?? 0) + 1)
  }
  const rank = new Map<number, number>([...successors.keys()].map(key => [key, 0]))
  const queue = [...indegree.entries()].filter(([, value]) => value === 0).map(([key]) => key).sort((a, b) => a - b)
  while (queue.length) {
    const current = queue.shift() as number
    for (const next of [...(successors.get(current) ?? [])].sort((a, b) => a - b)) {
      rank.set(next, Math.max(rank.get(next) ?? 0, (rank.get(current) ?? 0) + 1))
      indegree.set(next, (indegree.get(next) ?? 0) - 1)
      if (indegree.get(next) === 0) queue.push(next)
    }
  }
  const layerOf = new Map(sorted.map(key => [key, rank.get(owner.get(key) ?? 0) ?? 0]))
  const layers: string[][] = []
  for (const key of sorted) (layers[layerOf.get(key) ?? 0] ??= []).push(key)
  const position = new Map<string, number>()
  const place = () => layers.forEach(layer => layer.forEach((key, i) => position.set(key, i)))
  place()
  const neighbours = (key: string, upward: boolean) =>
    edges.filter(edge => (upward ? edge.target === key : edge.source === key)).map(edge => (upward ? edge.source : edge.target))
  const sweep = (upward: boolean) => {
    const order = upward ? layers.map((_, i) => i) : layers.map((_, i) => layers.length - 1 - i)
    for (const index of order) {
      const layer = layers[index]
      if (!layer) continue
      const score = new Map(layer.map(key => {
        const values = neighbours(key, upward).filter(other => position.has(other) && layerOf.get(other) !== index).map(other => position.get(other) ?? 0)
        return [key, values.length ? values.reduce((a, b) => a + b, 0) / values.length : position.get(key) ?? 0]
      }))
      layer.sort((a, b) => (score.get(a) ?? 0) - (score.get(b) ?? 0) || a.localeCompare(b))
      layer.forEach((key, i) => position.set(key, i))
    }
  }
  sweep(true); sweep(false)
  const nodes = layers.flatMap((layer, column) => (layer ?? []).map((key, order) => ({
    key, layer: column, order, x: MARGIN + column * COLUMN, y: MARGIN + order * ROW,
  })))
  const height = Math.max(1, ...layers.map(layer => layer?.length ?? 0)) * ROW + MARGIN * 2
  return { nodes, width: Math.max(1, layers.length) * COLUMN + MARGIN, height }
}
