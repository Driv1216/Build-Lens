export type Project = {
  id: string
  name: string
  root_path: string
  created_at: string
  updated_at: string
}

export type CreateProjectInput = Pick<Project, 'name' | 'root_path'>

export function listProjects(signal?: AbortSignal): Promise<Project[]> {
  return request<Project[]>('/projects', signal ? { signal } : undefined)
}

export function createProject(input: CreateProjectInput): Promise<Project> {
  return request<Project>('/projects', { method: 'POST', body: JSON.stringify(input) })
}
import { request } from './client'
