import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getTask, startIndex } from '../../api/tasks'
import { sessionRevision, subscribeSession } from '../../auth/session'
import { ApiError } from '../../api/client'

export const taskStorageKey = (projectId: string) => `pycode:index-task:${projectId}`
const permanentError = (error: unknown) => error instanceof ApiError && error.status != null &&
  error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429

export function useIndexTask(projectId: string) {
  const revision = useSyncExternalStore(subscribeSession, sessionRevision)
  const key = taskStorageKey(projectId)
  const [stored, setStored] = useState(() => ({projectId, revision, taskId: sessionStorage.getItem(key)}))
  const taskId = stored.projectId === projectId && stored.revision === revision ? stored.taskId : sessionStorage.getItem(key)
  const [invalid, setInvalid] = useState<{projectId: string; revision: number; error: unknown} | null>(null)
  useEffect(() => setStored({projectId, revision, taskId: sessionStorage.getItem(key)}), [key, projectId, revision])
  const attempt = useRef<{projectId: string; revision: number; key: string; pending: boolean} | null>(null)
  const queryClient = useQueryClient()
  const task = useQuery({queryKey: ['task', taskId, projectId, revision], queryFn: () => getTask(taskId!), enabled: !!taskId,
    retry: (count, error) => !permanentError(error) && count < 2,
    retryDelay: count => Math.min(1000 * 2 ** count, 8000),
    refetchOnWindowFocus: false, refetchOnReconnect: false, retryOnMount: false,
    refetchInterval: query => query.state.status === 'error' || query.state.fetchFailureCount > 0 ||
      ['completed', 'failed'].includes(query.state.data?.status ?? '') ? false : 1500})
  useEffect(() => {
    if (!taskId || !task.isError || !permanentError(task.error) || revision !== sessionRevision()) return
    if (sessionStorage.getItem(key) === taskId) sessionStorage.removeItem(key)
    setInvalid({projectId, revision, error: task.error})
    setStored({projectId, revision, taskId: sessionStorage.getItem(key)})
  }, [taskId, task.isError, task.error, key, projectId, revision])
  useEffect(() => {
    if (!task.data || !['completed', 'failed'].includes(task.data.status) || revision !== sessionRevision()) return
    if (sessionStorage.getItem(key) === taskId) sessionStorage.removeItem(key)
    void queryClient.invalidateQueries({queryKey: ['project', projectId]})
    void queryClient.invalidateQueries({queryKey: ['snapshot', projectId]})
    void queryClient.invalidateQueries({queryKey: ['snapshots', projectId]})
    void queryClient.invalidateQueries({queryKey: ['projects']})
  }, [task.data, taskId, key, projectId, queryClient, revision])
  const start = useMutation({mutationFn: async () => {
    const revision = sessionRevision()
    if (attempt.current?.projectId !== projectId || attempt.current.revision !== revision)
      attempt.current = {projectId, revision, key: crypto.randomUUID(), pending: false}
    const current = attempt.current
    if (current.pending) throw new Error('索引正在提交。')
    current.pending = true
    try {
      const result = await startIndex(projectId, current.key)
      if (revision !== sessionRevision()) throw new DOMException('Session changed', 'AbortError')
      if (attempt.current === current) attempt.current = null
      return {...result, revision}
    } finally {current.pending = false}
  }, onSuccess: result => {
    if (result.revision !== sessionRevision()) return
    sessionStorage.setItem(taskStorageKey(result.project_id), result.task_id)
    setStored({projectId: result.project_id, revision: result.revision, taskId: result.task_id})
    setInvalid(null)
    void queryClient.invalidateQueries({queryKey: ['project', result.project_id]})
  }})
  const invalidTaskError = !taskId && invalid?.projectId === projectId && invalid.revision === revision ? invalid.error : null
  const canRecheck = !!taskId && task.isError && !permanentError(task.error)
  const recheck = () => {
    if (taskId && revision === sessionRevision() && sessionStorage.getItem(key) === taskId) void task.refetch()
  }
  return {taskId, task, start, taskError: task.error ?? invalidTaskError, invalidTaskError, canRecheck, recheck}
}
