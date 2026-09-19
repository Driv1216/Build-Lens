import { request } from './client'

export type Snapshot = {
  id: string
  project_id: string
  canonical_root: string
  scan_started_at: string
  scan_completed_at: string
  scan_status: 'complete' | 'partial'
  policy_version: string
  sequence: number
  predecessor_id: string | null
  content_manifest: string | null
  fingerprint_coverage: { eligible_files: number; fingerprinted_files: number; unavailable_files: number; not_eligible_files: number; bytes_consumed: number; complete: boolean } | null
  eligibility_provenance: Record<string, unknown> | null
  comparison_contract: string
  git_metadata: { state: 'available' | 'not_repository' | 'unavailable'; repository_root: string | null; project_prefix: string | null; branch: string | null; head_commit: string | null; dirty: boolean | null; observed_at: string }
  summary: { observed_by_kind: Record<string, number>; included_regular_files: number; excluded_regular_files: number; pruned_directories: number; included_bytes: number; language_counts: Record<string, number>; tag_counts: Record<string, number>; exclusion_reason_counts: Record<string, number>; warning_count: number }
  warnings: { code: string; message: string; locator: string | null }[]
}

export type Artifact = { id: string; kind: string; locator: string; inventory_status: 'included' | 'excluded'; size_bytes: number | null; language: string | null; tags: string[]; content_hint: string; ignore_status: string; exclusion_reasons: string[]; pruned: boolean; stable_identity_id: string | null; continuity_reason: string | null; content_fingerprint: string | null; fingerprint_status: string; fingerprint_size_bytes: number | null; fingerprint_diagnostic: string | null }
export type ArtifactPage = { items: Artifact[]; total: number; limit: number; offset: number }
export type ArtifactChange = { state: 'ADDED' | 'UNCHANGED' | 'CHANGED' | 'REMOVED' | 'UNKNOWN'; reason: string; artifact_id: string | null; previous_artifact_id: string | null; locator: string; stable_identity_id: string | null }
export type ArtifactChangePage = { items: ArtifactChange[]; total: number; limit: number; offset: number }

export function latestSnapshot(projectId: string, signal?: AbortSignal): Promise<Snapshot> { return request(`/projects/${projectId}/snapshots/latest`, { signal }) }
export function createSnapshot(projectId: string, signal?: AbortSignal): Promise<Snapshot> { return request(`/projects/${projectId}/snapshots`, { method: 'POST', body: '{}', signal }) }
export function listArtifacts(projectId: string, snapshotId: string, offset = 0, signal?: AbortSignal): Promise<ArtifactPage> { return request(`/projects/${projectId}/snapshots/${snapshotId}/artifacts?limit=100&offset=${offset}`, { signal }) }
export function listArtifactChanges(projectId: string, snapshotId: string, offset = 0, signal?: AbortSignal): Promise<ArtifactChangePage> { return request(`/projects/${projectId}/snapshots/${snapshotId}/changes?limit=100&offset=${offset}`, { signal }) }
