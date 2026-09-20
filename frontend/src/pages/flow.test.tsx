import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useSearchParams } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ProjectsPage } from './ProjectsPage'
import { CreateProjectPage } from './CreateProjectPage'
import { AgentPanel } from '../features/agent/AgentPanel'
import { analysisHistoryKey } from '../api/projects'
import { useIndexTask, taskStorageKey } from '../features/projects/useIndexTask'

const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status,
  headers: {'Content-Type': 'application/json'}})
function wrapper(children: React.ReactNode) {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}})
  return <QueryClientProvider client={client}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>
}
afterEach(() => {vi.unstubAllGlobals(); sessionStorage.clear()})

describe('project UI', () => {
  it('shows the empty Projects state from the API', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({projects: [], has_more: false})))
    render(wrapper(<ProjectsPage />))
    expect(await screen.findByText('暂无项目')).toBeVisible()
  })
  it('shows a public error when Projects cannot load', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({detail: {code: 'database_unavailable', message: 'Database unavailable.'}}, 503)))
    render(wrapper(<ProjectsPage />))
    expect(await screen.findByRole('alert')).toHaveTextContent('Database unavailable.')
  })
  it('submits the Create Project form with the configured branch', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({id: 'p1', name: 'Service'}))
    vi.stubGlobal('fetch', fetchMock)
    render(wrapper(<CreateProjectPage />))
    await userEvent.type(screen.getByLabelText('项目名称'), 'Service')
    await userEvent.type(screen.getByLabelText('仓库地址'), 'https://github.com/example/service.git')
    await userEvent.type(screen.getByLabelText('分支（可选）'), 'main')
    await userEvent.click(screen.getByRole('button', {name: '创建项目'}))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/projects',
      expect.objectContaining({method: 'POST', body: JSON.stringify({name: 'Service',
        repo_url: 'https://github.com/example/service.git', branch: 'main'})})))
  })
})

describe('Agent modes', () => {
  it('renders synchronous question evidence and uses a file path for impact', async () => {
    const fetchMock = vi.fn((url: string) => Promise.resolve(response({project_id: 'p', answer: 'Entry is main.py',
      intent: url.endsWith('/impact') ? 'impact' : 'location', evidence: url.endsWith('/impact')
        ? ['src/main.py', 'func:src/main.py:main'] : ['main.py:1', 'file:main.py']})))
    vi.stubGlobal('fetch', fetchMock)
    render(wrapper(<AgentPanel projectId="p" onRun={vi.fn()} />))
    expect(screen.getByText('针对当前仓库快速提问，并基于代码提供回答和相关证据。')).toBeVisible()
    await userEvent.type(screen.getByLabelText('提问'), 'Where?')
    await userEvent.click(screen.getByRole('button', {name: '开始分析'}))
    expect(await screen.findByText('证据（2）')).toBeVisible()
    expect(screen.queryByText('main.py:1')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', {name: '展开证据'}))
    expect(screen.getByText('main.py:1')).toBeVisible()
    expect(screen.getByText('file:main.py')).toBeVisible()
    await userEvent.click(screen.getByRole('button', {name: '收起证据'}))
    expect(screen.queryByText('main.py:1')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', {name: '文件影响分析'}))
    expect(screen.getByText('分析指定文件发生变化后可能影响的代码和依赖关系。')).toBeVisible()
    await userEvent.type(screen.getByLabelText('文件路径'), 'main.py')
    await userEvent.click(screen.getByRole('button', {name: '开始分析'}))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/projects/p/impact',
      expect.objectContaining({body: JSON.stringify({file_path: 'main.py'})})))
    expect(await screen.findByText('证据（2）')).toBeVisible()
    expect(screen.queryByText('src/main.py')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', {name: '展开证据'}))
    expect(screen.getByText('src/main.py')).toBeVisible()
    expect(screen.getByText('func:src/main.py:main')).toBeVisible()
  })
  it('passes an asynchronous run ID to workspace navigation', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({run_id: 'run-1', task_id: 'task-1', status: 'queued'})))
    const onRun = vi.fn()
    render(wrapper(<AgentPanel projectId="p" onRun={onRun} />))
    await userEvent.click(screen.getByRole('tab', {name: '智能体分析'}))
    expect(screen.getByText('由智能体制定计划、调用工具并进行多步骤仓库分析。')).toBeVisible()
    await userEvent.type(screen.getByLabelText('分析任务'), 'Inspect code')
    await userEvent.click(screen.getByRole('button', {name: '开始智能体分析'}))
    await waitFor(() => expect(onRun).toHaveBeenCalledWith('run-1'))
  })
  it('refreshes History after Repository Question and Impact succeed', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(response({
      project_id: 'p', answer: 'Answer', intent: 'analysis', evidence: [],
    })))
    vi.stubGlobal('fetch', fetchMock)
    const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}})
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    render(<QueryClientProvider client={client}><MemoryRouter><AgentPanel projectId="p" onRun={vi.fn()} /></MemoryRouter></QueryClientProvider>)
    await userEvent.type(screen.getByLabelText('提问'), 'Where?')
    await userEvent.click(screen.getByRole('button', {name: '开始分析'}))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({queryKey: analysisHistoryKey('p')}))
    invalidate.mockClear()
    await userEvent.click(screen.getByRole('tab', {name: '文件影响分析'}))
    await userEvent.type(screen.getByLabelText('文件路径'), 'main.py')
    await userEvent.click(screen.getByRole('button', {name: '开始分析'}))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({queryKey: analysisHistoryKey('p')}))
  })

  it('refreshes History after an Agent run has a successful result', async () => {
    const client = new QueryClient({defaultOptions: {queries: {retry: false, staleTime: Infinity}}})
    client.setQueryData(['run', 'run-1'], {
      id: 'run-1', project_id: 'p', snapshot_id: null, task: 'Inspect code', run_type: 'agent',
      status: 'completed', result: 'Done', error_message: null, created_at: '2026-09-20T00:00:00Z',
      started_at: null, core_started_at: null, finished_at: null,
    })
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    render(<QueryClientProvider client={client}><MemoryRouter><AgentPanel projectId="p" runId="run-1" onRun={vi.fn()} /></MemoryRouter></QueryClientProvider>)
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({queryKey: analysisHistoryKey('p')}))
  })
})

function AgentHarness() {
  const [params, setParams] = useSearchParams()
  return <><AgentPanel projectId="p" runId={params.get('runId')} onRun={id => setParams({runId: id})} />
    <span data-testid="current-run-id">{params.get('runId') ?? ''}</span></>
}
function renderAgentHarness(initial = '/') {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}})
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[initial]}><AgentHarness /></MemoryRouter></QueryClientProvider>)
}
const queuedRun = {id: 'run-1', project_id: 'p', status: 'queued', task: 'Inspect code'}

it('shows immediate submitting feedback and sends one POST for a double click', async () => {
  let finishPost!: (value: Response) => void
  const pendingPost = new Promise<Response>(resolve => {finishPost = resolve})
  const fetchMock = vi.fn((url: string, options?: RequestInit) =>
    url === '/api/v1/agent-runs' && options?.method === 'POST'
      ? pendingPost : Promise.resolve(response(queuedRun)))
  vi.stubGlobal('fetch', fetchMock)
  renderAgentHarness()
  await userEvent.click(screen.getByRole('tab', {name: '智能体分析'}))
  await userEvent.type(screen.getByLabelText('分析任务'), 'Inspect code')
  const button = screen.getByRole('button', {name: '开始智能体分析'})
  fireEvent.click(button)
  fireEvent.click(button)
  await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => url === '/api/v1/agent-runs')).toHaveLength(1))
  expect(screen.getByRole('button', {name: '正在提交…'})).toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent('正在提交智能体分析…')

  await act(async () => {finishPost(response({run_id: 'run-1', task_id: 'task-1', status: 'queued'}, 202))})
  expect(await screen.findByTestId('current-run-id')).toHaveTextContent('run-1')
  expect(await screen.findByRole('button', {name: '智能体分析进行中'})).toBeDisabled()
  expect(fetchMock.mock.calls.filter(([url]) => url === '/api/v1/agent-runs')).toHaveLength(1)
})

it('re-enables Run Agent and shows ApiError when submit fails', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({detail: {code: 'unavailable', message: 'Try again.'}}, 503)))
  renderAgentHarness()
  await userEvent.click(screen.getByRole('tab', {name: '智能体分析'}))
  await userEvent.type(screen.getByLabelText('分析任务'), 'Inspect code')
  await userEvent.click(screen.getByRole('button', {name: '开始智能体分析'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('Try again.')
  expect(screen.getByRole('button', {name: '开始智能体分析'})).toBeEnabled()
})

it.each(['queued', 'running'])('blocks a new Agent submit while current run is %s', async status => {
  const fetchMock = vi.fn().mockResolvedValue(response({...queuedRun, status}))
  vi.stubGlobal('fetch', fetchMock)
  renderAgentHarness('/?runId=run-1')
  await userEvent.click(screen.getByRole('tab', {name: '智能体分析'}))
  await userEvent.type(screen.getByLabelText('分析任务'), 'Inspect code')
  expect(await screen.findByText(/当前智能体分析状态：/)).toBeVisible()
  expect(screen.getByRole('button', {name: '智能体分析进行中'})).toBeDisabled()
  expect(fetchMock.mock.calls.filter(([url]) => url === '/api/v1/agent-runs')).toHaveLength(0)
})

function IndexProbe() {
  const index = useIndexTask('p')
  return <div><span>{index.taskId ?? 'no task'}</span><span>{index.task.data?.status ?? 'loading'}</span><button onClick={() => index.start.mutate()}>Start</button></div>
}
it('recovers the exact index task from sessionStorage and clears it at terminal', async () => {
  sessionStorage.setItem(taskStorageKey('p'), 'task-1')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({id: 'task-1', status: 'completed'})))
  render(wrapper(<IndexProbe />))
  expect(screen.getByText('task-1')).toBeVisible()
  expect(await screen.findByText('completed')).toBeVisible()
  await waitFor(() => expect(sessionStorage.getItem(taskStorageKey('p'))).toBeNull())
})
