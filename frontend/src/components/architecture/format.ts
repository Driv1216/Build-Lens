import { ApiError } from '../../api/client'
import { visible } from '../explore/visible'

export function label(key: string): string {
  if (key === 'workspace:.') return '(repository root)'
  return visible(key.replace(/^(module|container|workspace|external):/, ''))
}

export function describe(reason: unknown): string {
  if (reason instanceof ApiError) {
    switch (reason.code) {
      case 'ARCHITECTURE_REQUIRED': return 'No architecture model yet: build it from the snapshot panel (after the semantic catalog).'
      case 'ARCHITECTURE_STALE': return 'The architecture model is stale because its inputs changed: rebuild it from the snapshot panel.'
      case 'ARCHITECTURE_INTEGRITY_FAILED': return 'Persisted architecture rows failed integrity verification; nothing is shown. Run the integrity check for details.'
      case 'ARCHITECTURE_EVIDENCE_INVALID': return 'Evidence behind this answer failed verification, so no partial answer is shown.'
      case 'ARCHITECTURE_VERIFICATION_BUDGET_EXCEEDED': return 'This view needs more verification than one request allows. Drill down to a smaller container.'
      case 'SEMANTIC_CATALOG_REQUIRED': return 'Build the semantic catalog first.'
      case 'SEMANTIC_CATALOG_STALE': return 'The semantic catalog is stale: rebuild it first.'
    }
  }
  if (reason instanceof DOMException && reason.name === 'AbortError') return ''
  return reason instanceof Error ? reason.message : String(reason)
}
