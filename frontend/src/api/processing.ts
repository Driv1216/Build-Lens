import { request } from './client'
export type ProcessingDiagnostic={code:string;message:string;severity:'warning'|'error'}
export type ProcessingRun={quarantined:boolean;superseded:boolean;run_id:string;snapshot_id:string;revision:number;started_at:string;completed_at:string;outcome:'complete'|'partial'|'failed';structural_analysis_id:string|null;coverage:Record<string,number>;diagnostics:ProcessingDiagnostic[];processing_contract:Record<string,unknown>}
export type ProcessingStatus={freshness_reasons:string[];latest_attempt:ProcessingRun|null;active_run:ProcessingRun|null;active_freshness:'fresh'|'stale'|'none'}
export type ArtifactOutcome={execution_mode:string;id:string;artifact_id:string;locator:string;outcome:string;encoding:string|null;chunk_count:number;diagnostics:ProcessingDiagnostic[]}
export type Chunk={locator:string;run_id:string;id:string;stable_key:string;ordinal:number;kind:string;content_hash:string;span:{start_line:number;end_line:number};content?:string|null}
export type Page<T>={run_id:string;total:number;limit:number;offset:number;items:T[]}
const base=(projectId:string,snapshotId:string)=>`/projects/${projectId}/snapshots/${snapshotId}/processing`
export const processArtifacts=(projectId:string,snapshotId:string)=>request<ProcessingStatus>(base(projectId,snapshotId),{method:'POST',body:'{}'})
export const getProcessing=(projectId:string,snapshotId:string)=>request<ProcessingStatus>(base(projectId,snapshotId))
export const processingArtifacts=(projectId:string,snapshotId:string)=>request<Page<ArtifactOutcome>>(`${base(projectId,snapshotId)}/artifacts?limit=100`)
export const processingChunks=(projectId:string,snapshotId:string)=>request<Page<Chunk>>(`${base(projectId,snapshotId)}/chunks?limit=100`)
export const processingChunk=(projectId:string,snapshotId:string,id:string)=>request<Chunk>(`${base(projectId,snapshotId)}/chunks/${id}`)
