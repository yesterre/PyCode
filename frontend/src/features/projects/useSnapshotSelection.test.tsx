import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { useSnapshotSelection } from './useSnapshotSelection'

afterEach(() => vi.unstubAllGlobals())
it('resets to the current snapshot of another project without querying the previous project snapshot', async () => {
  const fetchMock = vi.fn((url: string) => {
    const parts = url.split('/')
    const projectId = parts[4], id = parts[6]
    return Promise.resolve(new Response(JSON.stringify(id ? {id, project_id: projectId, status: 'ready'} : {snapshots: []})))
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  const wrapper = ({children}: {children: ReactNode}) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const {result, rerender} = renderHook(({projectId, currentId}) => useSnapshotSelection(projectId, currentId),
    {initialProps: {projectId: 'first', currentId: 'current-first'}, wrapper})
  await waitFor(() => expect(result.current.ready).toBe(true))
  act(() => result.current.selectSnapshot('historical-first'))
  await waitFor(() => expect(result.current.snapshot.data?.id).toBe('historical-first'))
  rerender({projectId: 'second', currentId: 'current-second'})
  expect(result.current.snapshotId).toBe('current-second')
  await waitFor(() => expect(result.current.ready).toBe(true))
  expect(fetchMock.mock.calls.map(([url]) => url)).not.toContain('/api/v1/projects/second/snapshots/historical-first')
})

it('does not make a foreign snapshot usable even if it is marked ready', async () => {
  vi.stubGlobal('fetch', vi.fn((url: string) => Promise.resolve(new Response(JSON.stringify(url.endsWith('/snapshots/s')
    ? {id: 's', project_id: 'foreign', status: 'ready'} : {snapshots: []})))))
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  const {result} = renderHook(() => useSnapshotSelection('p', 's'), {wrapper: ({children}: {children: ReactNode}) =>
    <QueryClientProvider client={client}>{children}</QueryClientProvider>})
  await waitFor(() => expect(result.current.snapshot.isSuccess).toBe(true))
  expect(result.current.ready).toBe(false)
})
