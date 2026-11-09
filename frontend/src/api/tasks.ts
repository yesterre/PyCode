import { request } from './client'
import type { BackgroundTask, TaskStatus } from './types'

export const startIndex = (projectId: string, key?: string) =>
  request<{task_id: string; project_id: string; status: TaskStatus}>(
    `/projects/${encodeURIComponent(projectId)}/index`, {method: 'POST', ...(key ? {headers: {'Idempotency-Key': key}} : {})},
  )
export const getTask = (id: string) => request<BackgroundTask>(`/tasks/${encodeURIComponent(id)}`)
