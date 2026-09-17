import { request } from './client'
import type { GraphResult } from './types'

export type GraphSearch = {q?: string; file_path?: string; node_type?: string; limit?: number}
export function queryGraph(projectId: string, snapshotId: string, search: GraphSearch) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(search)) if (value !== undefined && value !== '') params.set(key, String(value))
  return request<GraphResult>(`/projects/${encodeURIComponent(projectId)}/snapshots/${encodeURIComponent(snapshotId)}/graph?${params}`)
}
