import { request } from './client'
import type { TaskStatus } from './types'

export const submitQuestion = (projectId: string, question: string, key: string) =>
  request<{run_id: string; task_id: string; status: TaskStatus}>(`/projects/${encodeURIComponent(projectId)}/questions`, {
    method: 'POST', headers: {'Idempotency-Key': key}, body: JSON.stringify({question}),
  })
