import { request } from './client'
import type { Analysis, Project } from './types'

export const listProjects = (limit = 50, offset = 0) =>
  request<{projects: Project[]; has_more: boolean}>(`/projects?limit=${limit}&offset=${offset}`)
export const getProject = (id: string) => request<Project>(`/projects/${encodeURIComponent(id)}`)
export const createProject = (body: {name: string; repo_url: string; branch: string | null}) =>
  request<Project>('/projects', { method: 'POST', body: JSON.stringify(body) })
export const askProject = (id: string, question: string) =>
  request<Analysis>(`/projects/${encodeURIComponent(id)}/ask`, {
    method: 'POST', body: JSON.stringify({question}),
  })
export const impactProject = (id: string, file_path: string) =>
  request<Analysis>(`/projects/${encodeURIComponent(id)}/impact`, {
    method: 'POST', body: JSON.stringify({file_path}),
  })
