import { describe, expect, it } from 'vitest'
import { layered } from './layout'

describe('layered layout', () => {
  it('ranks importers before imported modules and keeps cycles in one layer', () => {
    const result = layered(['c', 'a', 'b', 'd'], [
      { source: 'a', target: 'b' }, { source: 'b', target: 'c' }, { source: 'c', target: 'b' }, { source: 'c', target: 'd' },
    ])
    const layer = Object.fromEntries(result.nodes.map(node => [node.key, node.layer]))
    expect(layer.a).toBe(0)
    expect(layer.b).toBe(layer.c)
    expect(layer.d).toBe(layer.b + 1)
  })
  it('is deterministic regardless of input order', () => {
    const edges = [{ source: 'x', target: 'y' }, { source: 'x', target: 'z' }, { source: 'w', target: 'z' }]
    const one = layered(['x', 'y', 'z', 'w'], edges)
    const two = layered(['w', 'z', 'y', 'x'], [...edges].reverse())
    expect(one).toEqual(two)
  })
  it('handles 5,000-node chains without recursion', () => {
    const keys = Array.from({ length: 5000 }, (_, i) => `m${String(i).padStart(4, '0')}`)
    const edges = keys.slice(1).map((key, i) => ({ source: keys[i], target: key }))
    expect(layered(keys, edges).nodes).toHaveLength(5000)
  })
})
