import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getTask, startIndex } from '../../api/tasks'
import { sessionRevision } from '../../auth/session'

export const taskStorageKey = (projectId: string) => `pycode:index-task:${projectId}`
export function useIndexTask(projectId: string) {
  const key = taskStorageKey(projectId)
  const [stored, setStored] = useState(() => ({projectId, taskId: sessionStorage.getItem(key)}))
  const taskId = stored.projectId === projectId ? stored.taskId : sessionStorage.getItem(key)
  useEffect(() => setStored({projectId, taskId: sessionStorage.getItem(key)}), [key, projectId])
  const attempt = useRef<{projectId: string; revision: number; key: string; pending: boolean} | null>(null)
  const queryClient = useQueryClient()
  const task = useQuery({queryKey: ['task', taskId], queryFn: () => getTask(taskId!), enabled: !!taskId,
    refetchInterval: query => ['completed', 'failed'].includes(query.state.data?.status ?? '') ? false : 1500})
  useEffect(() => {
    if (!task.data || !['completed', 'failed'].includes(task.data.status)) return
    sessionStorage.removeItem(key)
    void queryClient.invalidateQueries({queryKey: ['project', projectId]})
    void queryClient.invalidateQueries({queryKey: ['snapshot', projectId]})
    void queryClient.invalidateQueries({queryKey: ['snapshots', projectId]})
    void queryClient.invalidateQueries({queryKey: ['projects']})
  }, [task.data, key, projectId, queryClient])
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
    setStored({projectId: result.project_id, taskId: result.task_id})
    void queryClient.invalidateQueries({queryKey: ['project', result.project_id]})
  }})
  return {taskId, task, start}
}
