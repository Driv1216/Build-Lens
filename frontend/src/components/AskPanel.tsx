import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { askBuildLens, getGeneration, type AskResponse, type GenerationStatus } from '../api/ask'
import { getEmbeddings, getReranker, type EmbeddingStatus, type RankingPolicy, type RerankerStatus, type RetrievalMode, type SemanticSearchFilters } from '../api/embeddings'

type Props = { projectId: string; snapshotId: string; invalidation: number }

export function AskPanel(props: Props) {
  return <AskScope key={`${props.projectId}/${props.snapshotId}`} {...props} />
}

function AskScope({ projectId, snapshotId, invalidation }: Props) {
  const [question, setQuestion] = useState('')
  const [mode, setMode] = useState<RetrievalMode>('hybrid')
  const [ranking, setRanking] = useState<RankingPolicy>('local_cross_encoder')
  const [locator, setLocator] = useState('')
  const [language, setLanguage] = useState('')
  const [kind, setKind] = useState('')
  const [generation, setGeneration] = useState<GenerationStatus | null>(null)
  const [embedding, setEmbedding] = useState<EmbeddingStatus | null>(null)
  const [reranker, setReranker] = useState<RerankerStatus | null>(null)
  const [result, setResult] = useState<AskResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const sequence = useRef(0)

  const clear = useCallback(() => {
    sequence.current += 1
    controller.current?.abort()
    controller.current = null
    setResult(null)
    setError(null)
    setPending(false)
  }, [])
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [nextGeneration, nextEmbedding, nextReranker] = await Promise.all([
      getGeneration(signal), getEmbeddings(projectId, snapshotId, signal), getReranker(signal),
    ])
    if (!signal?.aborted) {
      setGeneration(nextGeneration)
      setEmbedding(nextEmbedding)
      setReranker(nextReranker)
    }
    return nextEmbedding
  }, [projectId, snapshotId])

  useEffect(() => {
    const request = new AbortController()
    void refresh(request.signal).catch((reason: unknown) => {
      if (!request.signal.aborted) setError(message(reason))
    })
    return () => { request.abort(); sequence.current += 1; controller.current?.abort() }
  }, [refresh, invalidation])
  useEffect(() => { clear() }, [question, mode, ranking, locator, language, kind, invalidation, clear])
  useEffect(() => {
    const onFocus = () => {
      const request = new AbortController()
      void refresh(request.signal).then(current => {
        if (!request.signal.aborted && embedding &&
            (current.active_processing_run_id !== embedding.active_processing_run_id ||
             current.active_run?.run_id !== embedding.active_run?.run_id)) {
          clear()
          setError('Evidence authority changed. Ask again with current evidence.')
        }
      }).catch(() => undefined)
    }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [clear, refresh, embedding])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    clear()
    const request = new AbortController()
    controller.current = request
    const current = sequence.current
    setPending(true)
    const filters: SemanticSearchFilters = {}
    if (locator) filters.locator_prefix = locator
    if (language) filters.language = language
    if (kind) filters.kind = kind
    try {
      const value = await askBuildLens(projectId, snapshotId, question, mode, ranking, filters, request.signal)
      if (request.signal.aborted || current !== sequence.current) return
      const currentAuthority = await getEmbeddings(projectId, snapshotId, request.signal)
      if (request.signal.aborted || current !== sequence.current) return
      if (currentAuthority.active_processing_run_id !== value.context.processing_run_id ||
          (value.context.embedding_run_id && currentAuthority.active_run?.run_id !== value.context.embedding_run_id)) {
        clear()
        setError('Evidence authority changed. Ask again with current evidence.')
        setEmbedding(currentAuthority)
        return
      }
      setEmbedding(currentAuthority)
      setResult(value)
    } catch (reason: unknown) {
      if (!request.signal.aborted && current === sequence.current) setError(message(reason))
    } finally {
      if (!request.signal.aborted && current === sequence.current) setPending(false)
    }
  }

  const semantic = mode === 'semantic' || mode === 'hybrid'
  const ready = Boolean(generation?.configured) && (!semantic || Boolean(embedding?.search_available)) &&
    (ranking === 'retrieval_order' || Boolean(reranker?.available))
  return <section className="structural-panel ask-panel">
    <h3>Ask BuildLens</h3>
    <p className="note">Your question and selected source evidence are sent to {generation ? `${generation.provider} (${generation.model_id})` : 'the selected provider'}. Answers are returned only with validated evidence IDs.</p>
    {!generation && !error && <p role="status">Checking Ask configuration…</p>}
    {generation && !generation.configured && <p className="note">{generation.guidance}</p>}
    {semantic && !embedding?.search_available && <p className="note">Prepare current embeddings for {mode} retrieval.</p>}
    {ranking === 'local_cross_encoder' && !reranker?.available && <p className="note">Acquire the local reranker or choose retrieval order.</p>}
    <form className="semantic-form" onSubmit={(event) => void submit(event)}>
      <label>Question<input aria-label="Ask question" value={question} onChange={event => setQuestion(event.target.value)} maxLength={4096} required /></label>
      <label>Retrieval<select value={mode} onChange={event => setMode(event.target.value as RetrievalMode)}><option value="hybrid">Hybrid</option><option value="semantic">Semantic</option><option value="lexical">Lexical</option><option value="exact">Exact</option></select></label>
      <label>Ranking<select value={ranking} onChange={event => setRanking(event.target.value as RankingPolicy)}><option value="local_cross_encoder">Local cross-encoder</option><option value="retrieval_order">Retrieval order</option></select></label>
      <label>Locator prefix<input value={locator} onChange={event => setLocator(event.target.value)} maxLength={2048} /></label>
      <label>Language<input value={language} onChange={event => setLanguage(event.target.value)} maxLength={64} /></label>
      <label>Chunk kind<input value={kind} onChange={event => setKind(event.target.value)} maxLength={32} /></label>
      <button type="submit" disabled={!ready || !question.trim() || pending}>{pending ? 'Asking…' : 'Ask'}</button>
    </form>
    {pending && <p role="status">Checking evidence and generating an answer…</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {result && <div className="semantic-results" aria-live="polite">
      <h4>{result.status === 'answered' ? 'Answer' : 'Insufficient evidence'}</h4>
      {result.status === 'insufficient_evidence' && <p>{result.answer.text}</p>}
      {result.answer.statements.map((statement, index) => <p key={index}>
        {statement.support === 'inference' && <strong>Inference: </strong>}{statement.text}{' '}
        {statement.evidence_ids.map(id => <a key={id} href={`#ask-evidence-${result.request_id}-${id}`}>[{id}]</a>)}
      </p>)}
      {result.evidence.length > 0 && <details><summary>Source evidence ({result.evidence.length})</summary><ol>{result.evidence.map(source => <li id={`ask-evidence-${result.request_id}-${source.evidence_id}`} key={source.evidence_id}>
        <strong>{source.evidence_id}</strong> · <code>{source.item.locator}</code> · lines {source.item.span.start_line}–{source.item.span.end_line} · <code>{source.item.unit_anchor}</code>
        <p className="note">Chunk <code>{source.item.stable_key}</code> · part {source.item.part_index}</p><pre>{source.item.content}</pre>
      </li>)}</ol></details>}
      <details><summary>Policy and provenance</summary><p>Requested {result.context.retrieval.requested_mode}; executed {result.context.retrieval.executed_mode}; ranking {result.context.ranking_policy}; reranker {result.context.reranker_state}.</p><p>Provider {String(result.model_contract.provider)}; model {String(result.model_contract.model_id)}; returned {result.returned_model ?? 'none'}; policy {String(result.generation_policy.version)}; prompt {result.prompt_version}.</p><p>Context {result.context.budget.used} bytes; prompt {result.prompt_measurement?.input_tokens ?? 'not counted'} tokens; provider usage {result.usage_input_tokens ?? 'unavailable'} input / {result.usage_output_tokens ?? 'unavailable'} output.</p><p>Generation exclusions: {result.generation_decisions.length}. Total {result.total_seconds.toFixed(2)} seconds.</p></details>
    </div>}
  </section>
}

function message(reason: unknown): string { return reason instanceof Error ? reason.message : 'Ask could not be completed.' }
