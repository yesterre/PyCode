import { request } from './client'
import type { BackgroundTask, TaskStatus } from './types'

export const startIndex = (projectId: string) =>
  request<{task_id: string; project_id: string; status: TaskStatus}>(
    `/projects/${encodeURIComponent(projectId)}/index`, {method: 'POST'},
  )
export const getTask = (id: string) => request<BackgroundTask>(`/tasks/${encodeURIComponent(id)}`)
