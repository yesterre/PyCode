import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getTask, startIndex } from '../../api/tasks'

export const taskStorageKey = (projectId: string) => `pycode:index-task:${projectId}`
export function useIndexTask(projectId: string) {
  const key = taskStorageKey(projectId)
  const [taskId, setTaskId] = useState(() => sessionStorage.getItem(key))
  useEffect(() => setTaskId(sessionStorage.getItem(key)), [key])
  const queryClient = useQueryClient()
  const task = useQuery({queryKey: ['task', taskId], queryFn: () => getTask(taskId!), enabled: !!taskId,
    refetchInterval: query => ['completed', 'failed'].includes(query.state.data?.status ?? '') ? false : 1500})
  useEffect(() => {
    if (!task.data || !['completed', 'failed'].includes(task.data.status)) return
    sessionStorage.removeItem(key)
    void queryClient.invalidateQueries({queryKey: ['project', projectId]})
    void queryClient.invalidateQueries({queryKey: ['snapshot', projectId]})
    void queryClient.invalidateQueries({queryKey: ['snapshots', projectId]})
  }, [task.data, key, projectId, queryClient])
  const start = useMutation({mutationFn: () => startIndex(projectId), onSuccess: result => {
    sessionStorage.setItem(key, result.task_id)
    setTaskId(result.task_id)
    void queryClient.invalidateQueries({queryKey: ['project', projectId]})
  }})
  return {taskId, task, start}
}
