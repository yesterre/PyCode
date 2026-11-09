import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { clearSession, setSession } from '../../auth/session'
import { taskStorageKey, useIndexTask } from './useIndexTask'

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status})
const task = {id: 't', project_id: 'p', status: 'queued'}
const advance = (ms = 1) => act(async () => {await vi.advanceTimersByTimeAsync(ms)})
function mount(projectId = 'p') {
  sessionStorage.setItem(taskStorageKey(projectId), 't')
  const client = new QueryClient({defaultOptions: {queries: {gcTime: Infinity}, mutations: {retry: false}}})
  const wrapper = ({children}: {children: ReactNode}) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  return {...renderHook(({id}) => useIndexTask(id), {initialProps: {id: projectId}, wrapper}), client}
}
beforeEach(() => vi.useFakeTimers())
afterEach(() => {cleanup(); clearSession(); sessionStorage.clear(); vi.unstubAllGlobals(); vi.useRealTimers()})

it.each(['queued', 'running'])('continues polling a %s task every 1.5 seconds', async status => {
  const fetchMock = vi.fn(() => Promise.resolve(json({...task, status})))
  vi.stubGlobal('fetch', fetchMock)
  const {result} = mount()
  await advance()
  expect(result.current.task.data?.status).toBe(status)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  await advance(1500)
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await advance(1500)
  expect(fetchMock).toHaveBeenCalledTimes(3)
})

it.each(['completed', 'failed'])('stops polling %s tasks and refreshes related caches', async status => {
  const fetchMock = vi.fn(() => Promise.resolve(json({...task, status})))
  vi.stubGlobal('fetch', fetchMock)
  const {result, client} = mount(), invalidate = vi.spyOn(client, 'invalidateQueries')
  await advance()
  expect(result.current.task.data?.status).toBe(status)
  expect(sessionStorage.getItem(taskStorageKey('p'))).toBeNull()
  await advance(20000)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  for (const queryKey of [['project', 'p'], ['snapshot', 'p'], ['snapshots', 'p'], ['projects']])
    expect(invalidate).toHaveBeenCalledWith({queryKey})
})

it.each([400, 404, 410, 422])('stops permanently invalid tasks (%s) and clears only the matching record', async status => {
  const fetchMock = vi.fn(() => Promise.resolve(json({detail: {code: 'not_found', message: 'Task unavailable.'}}, status)))
  vi.stubGlobal('fetch', fetchMock)
  sessionStorage.setItem(taskStorageKey('other'), 'other-task')
  const {result} = mount()
  await advance()
  expect(result.current.taskId).toBeNull()
  expect(result.current.invalidTaskError).toMatchObject({status})
  expect(sessionStorage.getItem(taskStorageKey('p'))).toBeNull()
  expect(sessionStorage.getItem(taskStorageKey('other'))).toBe('other-task')
  await advance(30000)
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it.each(['network', 503, 429, 408])('uses bounded retries with backoff for %s, then allows manual recovery', async failure => {
  let recover = false
  const fetchMock = vi.fn(() => recover ? Promise.resolve(json({...task, status: 'running'})) : failure === 'network'
    ? Promise.reject(new Error('offline')) : Promise.resolve(json({detail: {code: 'unavailable', message: 'Try later.'}}, Number(failure))))
  vi.stubGlobal('fetch', fetchMock)
  const {result} = mount()
  await advance()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  await advance(900)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  await advance(100)
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await advance(1900)
  expect(fetchMock).toHaveBeenCalledTimes(2)
  await advance(200)
  expect(fetchMock).toHaveBeenCalledTimes(3)
  expect(result.current.canRecheck).toBe(true)
  expect(sessionStorage.getItem(taskStorageKey('p'))).toBe('t')
  await advance(60000)
  expect(fetchMock).toHaveBeenCalledTimes(3)
  recover = true
  act(() => result.current.recheck())
  await advance()
  expect(result.current.task.data?.status).toBe('running')
  expect(result.current.task.isError).toBe(false)
  await advance(1500)
  expect(fetchMock).toHaveBeenCalledTimes(5)
})

it('also stops interval polling when an already running task encounters network failure', async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(json({...task, status: 'running'})).mockRejectedValue(new Error('offline'))
  vi.stubGlobal('fetch', fetchMock)
  const {result} = mount()
  await advance()
  await advance(1500)
  await advance(3100)
  expect(fetchMock).toHaveBeenCalledTimes(4)
  expect(result.current.canRecheck).toBe(true)
  expect(result.current.task.data?.status).toBe('running')
  await advance(60000)
  expect(fetchMock).toHaveBeenCalledTimes(4)
})

it('does not let a late 404 from a previous project erase the current project record', async () => {
  let finish!: (response: Response) => void
  const fetchMock = vi.fn((url: string) => url.endsWith('/tasks/t') ? new Promise<Response>(resolve => {finish = resolve})
    : Promise.resolve(json({id: 'new-task', project_id: 'other', status: 'running'})))
  vi.stubGlobal('fetch', fetchMock)
  const {result, rerender} = mount()
  sessionStorage.setItem(taskStorageKey('other'), 'new-task')
  rerender({id: 'other'})
  await advance()
  finish(json({detail: {code: 'not_found', message: 'Old missing task.'}}, 404))
  await advance()
  expect(result.current.taskId).toBe('new-task')
  expect(sessionStorage.getItem(taskStorageKey('other'))).toBe('new-task')
})

it('does not erase a replacement task in the same project on a late 404', async () => {
  let finish!: (response: Response) => void
  vi.stubGlobal('fetch', vi.fn((url: string) => url.endsWith('/tasks/t') ? new Promise<Response>(resolve => {finish = resolve})
    : Promise.resolve(json({...task, id: 'replacement', status: 'running'}))))
  const {result} = mount()
  sessionStorage.setItem(taskStorageKey('p'), 'replacement')
  finish(json({detail: {code: 'not_found', message: 'Missing.'}}, 404))
  await advance()
  expect(result.current.taskId).toBe('replacement')
  expect(sessionStorage.getItem(taskStorageKey('p'))).toBe('replacement')
})

it('isolates project query results even if their stored task IDs happen to match', async () => {
  let finish!: (response: Response) => void
  const fetchMock = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => {finish = resolve}))
    .mockImplementation(() => Promise.resolve(json({...task, project_id: 'other', status: 'running'})))
  vi.stubGlobal('fetch', fetchMock)
  const {result, rerender} = mount()
  sessionStorage.setItem(taskStorageKey('other'), 't')
  rerender({id: 'other'})
  await advance()
  finish(json({detail: {code: 'not_found', message: 'Previous project.'}}, 404))
  await advance()
  expect(result.current.task.data?.project_id).toBe('other')
  expect(result.current.invalidTaskError).toBeNull()
  expect(sessionStorage.getItem(taskStorageKey('other'))).toBe('t')
})

it('does not remove a replacement record when an older task completes', async () => {
  let finish!: (response: Response) => void
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => {finish = resolve})))
  mount()
  sessionStorage.setItem(taskStorageKey('p'), 'replacement')
  finish(json({...task, status: 'completed'}))
  await advance()
  expect(sessionStorage.getItem(taskStorageKey('p'))).toBe('replacement')
})

it('ignores a late error from a previous Session, even when task IDs are reused', async () => {
  setSession('session-a', 3600)
  let finish!: (response: Response) => void
  const fetchMock = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => {finish = resolve}))
    .mockImplementation(() => Promise.resolve(json({...task, status: 'running'})))
  vi.stubGlobal('fetch', fetchMock)
  const {result} = mount()
  act(() => {setSession('session-b', 3600); sessionStorage.setItem(taskStorageKey('p'), 't')})
  await advance()
  finish(json({detail: {code: 'not_found', message: 'Old Session task.'}}, 404))
  await advance()
  expect(result.current.task.data?.status).toBe('running')
  expect(result.current.invalidTaskError).toBeNull()
  expect(sessionStorage.getItem(taskStorageKey('p'))).toBe('t')
})
