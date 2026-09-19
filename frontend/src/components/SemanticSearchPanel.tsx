import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  getEmbeddingHistory,
  getEmbeddings,
  prepareEmbeddings,
  retrievalSearch,
  type EmbeddingHistory,
  type EmbeddingStatus,
  type RetrievalMode,
  type RetrievalResponse,
  type SemanticSearchFilters,
} from '../api/embeddings'

type Props = { projectId: string; snapshotId: string; revision?: number }

export function SemanticSearchPanel(props: Props) {
  const key = `${props.projectId}/${props.snapshotId}/${props.revision ?? 0}`
  return <SearchScope key={key} {...props} />
}

function SearchScope({ projectId, snapshotId }: Props) {
  const [status, setStatus] = useState<EmbeddingStatus | null>(null)
  const [history, setHistory] = useState<EmbeddingHistory | null>(null)
  const [mode, setMode] = useState<RetrievalMode>('lexical')
  const [query, setQuery] = useState('')
  const [topK, setTopK] = useState(10)
  const [locatorPrefix, setLocatorPrefix] = useState('')
  const [language, setLanguage] = useState('')
  const [kind, setKind] = useState('')
  const [result, setResult] = useState<RetrievalResponse | null>(null)
  const [preparing, setPreparing] = useState(false)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const lifetime = useRef<AbortController | null>(null)
  const searchRequest = useRef<AbortController | null>(null)

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [nextStatus, nextHistory] = await Promise.all([
      getEmbeddings(projectId, snapshotId, signal),
      getEmbeddingHistory(projectId, snapshotId, signal),
    ])
    if (!signal?.aborted) {
      setStatus(nextStatus)
      setHistory(Array.isArray(nextHistory.items) ? nextHistory : null)
    }
  }, [projectId, snapshotId])

  useEffect(() => {
    const controller = new AbortController()
    lifetime.current = controller
    setResult(null)
    setError(null)
    void refresh(controller.signal).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(messageFrom(reason))
    })
    return () => {
      controller.abort()
      searchRequest.current?.abort()
    }
  }, [projectId, snapshotId, refresh])

  useEffect(() => {
    searchRequest.current?.abort()
    setResult(null)
    setError(null)
    setSearching(false)
  }, [mode, query, topK, locatorPrefix, language, kind])

  async function prepare() {
    const signal = lifetime.current?.signal
    searchRequest.current?.abort()
    setPreparing(true)
    setResult(null)
    setError(null)
    try {
      await prepareEmbeddings(projectId, snapshotId, signal)
    } catch (reason: unknown) {
      if (!signal?.aborted) setError(messageFrom(reason))
    } finally {
      if (!signal?.aborted) {
        try {
          await refresh(signal)
        } catch (reason: unknown) {
          setError(messageFrom(reason))
        }
        setPreparing(false)
      }
    }
  }

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    searchRequest.current?.abort()
    const controller = new AbortController()
    searchRequest.current = controller
    setSearching(true)
    setResult(null)
    setError(null)
    const filters: SemanticSearchFilters = {}
    if (locatorPrefix) filters.locator_prefix = locatorPrefix
    if (language) filters.language = language
    if (kind) filters.kind = kind
    try {
      const value = await retrievalSearch(
        projectId,
        snapshotId,
        mode,
        query,
        topK,
        filters,
        controller.signal,
      )
      if (!controller.signal.aborted) setResult(value)
    } catch (reason: unknown) {
      if (!controller.signal.aborted) setError(messageFrom(reason))
    } finally {
      if (!controller.signal.aborted) setSearching(false)
    }
  }

  const active = status?.active_run ?? null
  const coverage = active?.coverage
  const needsSemantic = mode === 'semantic' || mode === 'hybrid'
  const searchable = !needsSemantic || Boolean(status?.search_available)
  return (
    <section className="structural-panel semantic-panel">
      <div className="section-heading">
        <h3>Evidence search</h3>
        <button disabled={preparing} onClick={() => void prepare()}>
          {preparing ? 'Preparing embeddings…' : status?.prepared ? 'Embeddings current' : 'Prepare embeddings'}
        </button>
      </div>
      <p className="note">Exact and lexical search use current PostgreSQL evidence without a model. Semantic and hybrid search require current embeddings.</p>
      {error && <p className="error" role="alert">{error}</p>}
      {error && mode === 'hybrid' && (
        <button type="button" onClick={() => setMode('lexical')}>Switch to lexical</button>
      )}
      {!status && !error && <p role="status">Loading embedding status…</p>}
      {status && <>
        <p>Model: <code>{status.contract.model_id}</code> · {status.contract.dimensions} dimensions · {status.model.available ? 'local cache verified' : `unavailable (${status.model.reason ?? 'unknown'})`}</p>
        <p>Active run: {status.prepared && active ? <><code>{active.run_id}</code> · revision #{active.revision} · {active.chunk_count} chunks · processing #{active.processing_revision}</> : 'not prepared for current evidence'}</p>
        {coverage && <p className="note">Coverage: {coverage.generated_chunks ?? coverage.embedded_chunks ?? 0} generated · {coverage.reused_chunks ?? 0} reused · {coverage.embedding_windows ?? 0} windows · {coverage.gaps ?? 0} Stage 5 gaps</p>}
        {status.latest_attempt?.outcome === 'failed' && <p className="note">Latest attempt #{status.latest_attempt.revision} failed: {status.latest_attempt.diagnostics.join(', ')}</p>}
        {status.freshness_reasons.map((reason) => <p className="note" key={reason}>{reason}</p>)}
        {!status.model.available && <p className="note">Exact and lexical modes remain available. Run <code>buildlens embeddings download-model</code> to enable query inference.</p>}
      </>}
      <form className="semantic-form" onSubmit={(event) => void search(event)}>
        <label>
          Search mode
          <select value={mode} onChange={(event) => setMode(event.target.value as RetrievalMode)} disabled={searching}>
            <option value="exact">Exact</option>
            <option value="lexical">Lexical</option>
            <option value="semantic">Semantic</option>
            <option value="hybrid">Hybrid</option>
          </select>
        </label>
        <label>
          Search query
          <input aria-label="Semantic query" value={query} onChange={(event) => setQuery(event.target.value)} maxLength={4096} required disabled={!searchable || searching} />
        </label>
        <label>
          Top results
          <input type="number" min={1} max={50} value={topK} onChange={(event) => setTopK(Number(event.target.value))} disabled={!searchable || searching} />
        </label>
        <label>
          Locator prefix
          <input value={locatorPrefix} onChange={(event) => setLocatorPrefix(event.target.value)} maxLength={2048} disabled={!searchable || searching} />
        </label>
        <label>
          Language
          <input value={language} onChange={(event) => setLanguage(event.target.value)} maxLength={64} disabled={!searchable || searching} />
        </label>
        <label>
          Chunk kind
          <input value={kind} onChange={(event) => setKind(event.target.value)} maxLength={32} disabled={!searchable || searching} />
        </label>
        <button type="submit" disabled={!searchable || searching || !query.trim()}>{searching ? 'Searching…' : 'Search'}</button>
      </form>
      {needsSemantic && !searchable && <p className="note">{mode === 'hybrid' ? 'Hybrid requires both lexical retrieval and a current semantic embedding run.' : 'Prepare current embeddings to use semantic search.'}</p>}
      {result && <div className="semantic-results" aria-live="polite">
        <h4>{result.returned_count} ranked result{result.returned_count === 1 ? '' : 's'}</h4>
        {result.hits.length === 0 && <p>No {result.executed_mode === 'semantic' ? 'semantic candidates pass the selected filters' : `${result.executed_mode} matches`}.</p>}
        {result.candidates_truncated && <p className="note">Results were selected from bounded lists of {result.candidate_depth} candidates.</p>}
        <ol>{result.hits.map((hit) => <li key={hit.chunk_id}>
          <div><strong>{hit.score === null ? 'Exact' : `${hit.score_kind} ${hit.score.toFixed(6)}`}</strong> · <code>{hit.locator}</code> · lines {hit.span.start_line}–{hit.span.end_line} · {hit.kind}</div>
          {hit.exact_matches.length > 0 && <p className="note">Exact: {hit.exact_matches.map((match) => `${match.field} = ${match.value}`).join(' · ')}</p>}
          {hit.lexical && <p className="note">Lexical rank #{hit.lexical.rank} · score {hit.lexical.score.toFixed(4)} · {Object.entries(hit.lexical.field_scores).map(([field, score]) => `${field} ${score.toFixed(4)}`).join(' · ') || 'exact-only match'}</p>}
          {hit.semantic && <p className="note">Semantic rank #{hit.semantic.rank} · cosine {hit.semantic.cosine_similarity.toFixed(4)} · {hit.semantic.window_count} window{hit.semantic.window_count === 1 ? '' : 's'} · {hit.semantic.execution_mode}</p>}
          {hit.hybrid && <p className="note">RRF: lexical {hit.hybrid.lexical_contribution.toFixed(6)} · semantic {hit.hybrid.semantic_contribution.toFixed(6)}</p>}
          <p className="note">Chunk <code>{hit.stable_key}</code></p>
          {hit.heading_path.length > 0 && <p className="note">Heading: {hit.heading_path.join(' › ')}</p>}
          {hit.json_path && <p className="note">JSON path: <code>{hit.json_path}</code></p>}
          <pre>{hit.content}</pre>
        </li>)}</ol>
      </div>}
      {history && <details>
        <summary>Embedding history ({history.total})</summary>
        {history.items.length === 0 ? <p className="note">No persisted attempts.</p> : <ol>
          {history.items.map((item) => <li key={item.run_id}>
            Run #{item.revision}: {item.outcome} · {item.chunk_count} rows · {item.coverage.generated_chunks ?? 0} generated · {item.coverage.reused_chunks ?? 0} reused{item.superseded ? ' · superseded' : ''}{item.quarantined ? ' · quarantined' : ''}
          </li>)}
        </ol>}
      </details>}
    </section>
  )
}

function messageFrom(reason: unknown): string {
  return reason instanceof Error ? reason.message : 'Search could not be completed.'
}
