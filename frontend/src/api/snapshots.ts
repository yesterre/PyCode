import { request } from './client'
import type { Snapshot } from './types'

export const listSnapshots = (projectId: string) =>
  request<{snapshots: Snapshot[]}>(`/projects/${encodeURIComponent(projectId)}/snapshots?limit=20`)
export const getSnapshot = (projectId: string, snapshotId: string) =>
  request<Snapshot>(`/projects/${encodeURIComponent(projectId)}/snapshots/${encodeURIComponent(snapshotId)}`)
