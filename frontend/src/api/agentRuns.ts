import { apiUrl, request } from './client'
import type { AgentRun, TaskStatus, TraceHistory } from './types'

export const submitAgent = (project_id: string, task: string) =>
  request<{run_id: string; task_id: string; status: TaskStatus}>('/agent-runs', {
    method: 'POST', body: JSON.stringify({project_id, task}),
  })
export const getAgentRun = (id: string) => request<AgentRun>(`/agent-runs/${encodeURIComponent(id)}`)
export const getTrace = (id: string) => request<TraceHistory>(`/agent-runs/${encodeURIComponent(id)}/trace`)
export const eventsUrl = (id: string) => apiUrl(`/agent-runs/${encodeURIComponent(id)}/events`)
