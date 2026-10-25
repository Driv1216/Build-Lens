import { useState, type FormEvent } from 'react'
import type { Entity, EntityFilters, EntityPage } from '../../api/semantic'

type Props = {
  page: EntityPage | null
  filters: EntityFilters
  selected: string
  onFilters: (value: EntityFilters) => void
  onSelect: (key: string) => void
  onMore: () => void
}

const KINDS = ['', 'file', 'symbol', 'component', 'hook', 'api_endpoint', 'db_table']
const LANGUAGES = ['', 'Python', 'TypeScript', 'JavaScript']
const STORED = ['', 'complete', 'incomplete', 'unavailable', 'coordinate_mismatch']
const CONTEXT = ['', 'full', 'outline', 'not_eligible']

export function EntityList({ page, filters, selected, onFilters, onSelect, onMore }: Props) {
  const [query, setQuery] = useState(filters.q ?? '')
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); onFilters({ ...filters, q: query.trim() }) }
  const groups = new Map<string, Entity[]>()
  for (const item of page?.items ?? []) groups.set(item.locator, [...(groups.get(item.locator) ?? []), item])
  return <aside className="explore-column" aria-label="Entities">
    <form className="explore-filters" onSubmit={submit}>
      <input aria-label="Search entities" placeholder="Search name or path" value={query} maxLength={200} onChange={event => setQuery(event.target.value)} />
      {([['kind', KINDS], ['language', LANGUAGES], ['stored_coverage', STORED], ['context_coverage', CONTEXT]] as const).map(([field, values]) =>
        <select key={field} aria-label={`Filter ${field.replace('_', ' ')}`} value={filters[field] ?? ''} onChange={event => onFilters({ ...filters, [field]: event.target.value })}>
          {values.map(value => <option key={value} value={value}>{value ? value.replace('_', ' ') : `any ${field.replace('_', ' ')}`}</option>)}
        </select>)}
    </form>
    {page && <p className="note">{page.total} entities</p>}
    {!page && <p className="note" role="status">Loading entities…</p>}
    <ul className="entity-tree">
      {[...groups].map(([locator, items]) => <li key={locator}>
        <code>{locator}</code>
        <ul>{items.map(item => <li key={item.entity_key}>
          <button className={`link-button entity ${item.entity_key === selected ? 'selected' : ''}`} style={{ paddingLeft: `${indent(item) * 12}px` }} onClick={() => onSelect(item.entity_key)}>
            <span className={`kind kind-${item.kind}`}>{label(item)}</span> {item.kind === 'file' ? 'file' : item.name}
            {item.stored_coverage !== 'complete' && <span className="badge warn"> {item.stored_coverage.replace('_', ' ')}</span>}
          </button>
        </li>)}</ul>
      </li>)}
    </ul>
    {page && page.items.length < page.total && <button onClick={onMore}>Load more</button>}
  </aside>
}

function indent(item: Entity): number {
  if (item.kind === 'file') return 0
  return 1 + Math.min(4, (item.qualified_name ?? '').split('.').length - 1)
}

function label(item: Entity): string {
  if (item.kind === 'symbol') return item.symbol_kind?.replace('_', ' ') ?? 'symbol'
  return item.kind.replace('_', ' ')
}
