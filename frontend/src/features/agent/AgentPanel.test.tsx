import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useSearchParams } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { AgentPanel } from './AgentPanel'
import { WorkspacePage } from '../../pages/WorkspacePage'
import { clearSession } from '../../auth/session'

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status})
const run = {id: 'r', project_id: 'p', status: 'running', task: 'Inspect', result: null}
function Harness() {
  const [params, setParams] = useSearchParams()
  return <><AgentPanel projectId="p" runId={params.get('runId')} onRun={vi.fn()} onClearRun={() =>
    setParams(previous => {const next = new URLSearchParams(previous); next.delete('runId'); return next})} />
    <output aria-label="查询参数">{params.toString()}</output></>
}
function mount() {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}})
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/?runId=r&view=trace&filter=a&filter=b']}><Harness /></MemoryRouter></QueryClientProvider>)
  return client
}
afterEach(() => {cleanup(); clearSession(); vi.unstubAllGlobals()})

it('locks during the initial run lookup, then respects the confirmed active run', async () => {
  let finish!: (response: Response) => void
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => {finish = resolve})))
  mount()
  await userEvent.type(screen.getByLabelText('提问'), 'New question')
  expect(screen.getByRole('button', {name: '正在检查分析…'})).toBeDisabled()
  finish(json(run))
  expect(await screen.findByRole('button', {name: '分析进行中'})).toBeDisabled()
  expect(screen.queryByRole('button', {name: '清除当前 Run 选择'})).not.toBeInTheDocument()
})

it.each(['queued', 'running', 'completed', 'failed'])('handles the confirmed %s state', async status => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json({...run, status}))))
  mount()
  await userEvent.type(screen.getByLabelText('提问'), 'New question')
  if (status === 'queued' || status === 'running') {
    expect(await screen.findByRole('button', {name: '分析进行中'})).toBeDisabled()
  } else {
    await waitFor(() => expect(screen.getByRole('button', {name: '开始分析'})).toBeEnabled())
  }
})

it.each([404, 422])('clears an invalid Run (%s) without losing other query parameters', async status => {
  const fetchMock = vi.fn(() => Promise.resolve(json({detail: {code: 'invalid_run', message: 'Run unavailable.'}}, status)))
  vi.stubGlobal('fetch', fetchMock)
  mount()
  expect(await screen.findByRole('alert')).toHaveTextContent('Run unavailable.')
  await userEvent.click(screen.getByRole('button', {name: '清除当前 Run 选择'}))
  expect(screen.getByLabelText('查询参数')).toHaveTextContent('view=trace&filter=a&filter=b')
  expect(screen.getByLabelText('查询参数')).not.toHaveTextContent('runId')
  await userEvent.type(screen.getByLabelText('提问'), 'Recovered question')
  expect(screen.getByRole('button', {name: '开始分析'})).toBeEnabled()
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

it('does not display or lock on a foreign project Run or invalidate its history', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json({...run, project_id: 'foreign',
    task: 'PRIVATE_TASK', result: 'PRIVATE_ANSWER', status: 'completed'}))))
  const client = mount(), invalidate = vi.spyOn(client, 'invalidateQueries')
  expect(await screen.findByRole('alert')).toHaveTextContent('不属于当前项目')
  expect(screen.queryByText('PRIVATE_TASK')).not.toBeInTheDocument()
  expect(screen.queryByText('PRIVATE_ANSWER')).not.toBeInTheDocument()
  expect(invalidate).not.toHaveBeenCalled()
  await userEvent.type(screen.getByLabelText('提问'), 'Local question')
  expect(screen.getByRole('button', {name: '开始分析'})).toBeEnabled()
  await userEvent.click(screen.getByRole('button', {name: '清除当前 Run 选择'}))
  expect(screen.getByLabelText('查询参数')).not.toHaveTextContent('runId')
})

it('recovers transient network failures through manual rechecking', async () => {
  const fetchMock = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(json(run))
  vi.stubGlobal('fetch', fetchMock)
  mount()
  expect(await screen.findByRole('alert')).toHaveTextContent('无法连接服务器')
  await userEvent.type(screen.getByLabelText('提问'), 'Question')
  expect(screen.getByRole('button', {name: '开始分析'})).toBeEnabled()
  await userEvent.click(screen.getByRole('button', {name: '重新检查 Run'}))
  expect(await screen.findByRole('button', {name: '分析进行中'})).toBeDisabled()
  expect(fetchMock).toHaveBeenCalledTimes(2)
})

it('allows exiting a cached active Run after a failed status refresh', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
  const client = mount()
  client.setQueryData(['run', 'r'], run)
  await screen.findByRole('alert')
  await userEvent.type(screen.getByLabelText('提问'), 'Question')
  expect(screen.getByRole('button', {name: '分析进行中'})).toBeDisabled()
  await userEvent.click(screen.getByRole('button', {name: '清除当前 Run 选择'}))
  expect(screen.getByRole('button', {name: '开始分析'})).toBeEnabled()
})

it('Workspace preserves unrelated URL parameters when clearing and selecting a Run', async () => {
  const project = {id: 'p', name: 'Demo', repo_url: 'https://example.com/repo', current_snapshot_id: 's',
    status: 'ready', archived_at: null, index_summary: null}
  const snapshot = {id: 's', project_id: 'p', status: 'ready', commit_sha: 'a'.repeat(40),
    created_at: '2026-11-09T00:00:00Z'}
  const fetchMock = vi.fn((url: string, options?: RequestInit) => {
    if (options?.method === 'POST') return Promise.resolve(json({run_id: 'new-run', task_id: 't', status: 'queued'}))
    if (url.endsWith('/agent-runs/r')) return Promise.resolve(json({detail: {code: 'not_found', message: 'Missing run.'}}, 404))
    if (url.endsWith('/agent-runs/new-run')) return Promise.resolve(json({...run, id: 'new-run', status: 'completed', result: 'Done'}))
    if (url.endsWith('/trace')) return Promise.resolve(json({run_id: 'new-run', events: []}))
    if (url.endsWith('/snapshots?limit=20')) return Promise.resolve(json({snapshots: [snapshot]}))
    return Promise.resolve(json(url.endsWith('/snapshots/s') ? snapshot : project))
  })
  vi.stubGlobal('fetch', fetchMock)
  function WorkspaceHarness() {
    const [params] = useSearchParams()
    return <><WorkspacePage /><output aria-label="Workspace 查询参数">{params.toString()}</output></>
  }
  render(<QueryClientProvider client={new QueryClient({defaultOptions: {queries: {retry: false}}})}>
    <MemoryRouter initialEntries={['/projects/p?runId=r&view=trace&filter=a&filter=b']}>
      <Routes><Route path="/projects/:projectId" element={<WorkspaceHarness />} /></Routes>
    </MemoryRouter></QueryClientProvider>)
  await userEvent.click(await screen.findByRole('button', {name: '清除当前 Run 选择'}))
  expect(screen.getByLabelText('Workspace 查询参数')).toHaveTextContent('view=trace&filter=a&filter=b')
  await userEvent.type(screen.getByLabelText('提问'), 'Question')
  await userEvent.click(screen.getByRole('button', {name: '开始分析'}))
  await waitFor(() => expect(screen.getByLabelText('Workspace 查询参数')).toHaveTextContent('view=trace&filter=a&filter=b&runId=new-run'))
})
