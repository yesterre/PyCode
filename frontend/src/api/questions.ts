import { request } from './client'
import type { TaskStatus } from './types'

export type QuestionOptions = {model?: string | null; snapshot_id?: string | null; target?: string | null}
export const submitQuestion = (projectId: string, question: string, key: string, options: QuestionOptions = {}) =>
  request<{run_id: string; task_id: string; status: TaskStatus}>(`/projects/${encodeURIComponent(projectId)}/questions`, {
    method: 'POST', headers: {'Idempotency-Key': key}, body: JSON.stringify({question, ...options}),
  })
