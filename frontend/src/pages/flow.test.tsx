import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useSearchParams } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ProjectsPage } from './ProjectsPage'
import { CreateProjectPage } from './CreateProjectPage'
import { AgentPanel } from '../features/agent/AgentPanel'
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
    expect(await screen.findByText('No Projects')).toBeVisible()
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
    await userEvent.type(screen.getByLabelText('Project name'), 'Service')
    await userEvent.type(screen.getByLabelText('Repository URL'), 'https://github.com/example/service.git')
    await userEvent.type(screen.getByLabelText('Branch (optional)'), 'main')
    await userEvent.click(screen.getByRole('button', {name: 'Create Project'}))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/projects',
      expect.objectContaining({method: 'POST', body: JSON.stringify({name: 'Service',
        repo_url: 'https://github.com/example/service.git', branch: 'main'})})))
  })
})

describe('Agent modes', () => {
  it('renders synchronous question evidence and uses a file path for impact', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({project_id: 'p', answer: 'Entry is main.py',
      intent: 'location', evidence: ['main.py:1']}))
    vi.stubGlobal('fetch', fetchMock)
    render(wrapper(<AgentPanel projectId="p" onRun={vi.fn()} />))
    expect(screen.getByText('Quickly answer a specific repository question with supporting evidence.')).toBeVisible()
    await userEvent.type(screen.getByLabelText('Question'), 'Where?')
    await userEvent.click(screen.getByRole('button', {name: 'Analyze'}))
    expect(await screen.findByText('main.py:1')).toBeVisible()
    await userEvent.click(screen.getByRole('tab', {name: 'Impact Analysis'}))
    expect(screen.getByText('Analyze what may be affected when a specific file changes.')).toBeVisible()
    await userEvent.type(screen.getByLabelText('File Path'), 'main.py')
    await userEvent.click(screen.getByRole('button', {name: 'Analyze'}))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/projects/p/impact',
      expect.objectContaining({body: JSON.stringify({file_path: 'main.py'})})))
  })
  it('passes an asynchronous run ID to workspace navigation', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({run_id: 'run-1', task_id: 'task-1', status: 'queued'})))
    const onRun = vi.fn()
    render(wrapper(<AgentPanel projectId="p" onRun={onRun} />))
    await userEvent.click(screen.getByRole('tab', {name: 'Agent Task'}))
    expect(screen.getByText('Let the Agent plan and use tools for a multi-step analysis with live trace.')).toBeVisible()
    await userEvent.type(screen.getByLabelText('Agent task'), 'Inspect code')
    await userEvent.click(screen.getByRole('button', {name: 'Run Agent'}))
    await waitFor(() => expect(onRun).toHaveBeenCalledWith('run-1'))
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
  await userEvent.click(screen.getByRole('tab', {name: 'Agent Task'}))
  await userEvent.type(screen.getByLabelText('Agent task'), 'Inspect code')
  const button = screen.getByRole('button', {name: 'Run Agent'})
  fireEvent.click(button)
  fireEvent.click(button)
  await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => url === '/api/v1/agent-runs')).toHaveLength(1))
  expect(screen.getByRole('button', {name: 'Submitting...'})).toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent('Submitting Agent task...')

  await act(async () => {finishPost(response({run_id: 'run-1', task_id: 'task-1', status: 'queued'}, 202))})
  expect(await screen.findByTestId('current-run-id')).toHaveTextContent('run-1')
  expect(await screen.findByRole('button', {name: 'Agent run in progress'})).toBeDisabled()
  expect(fetchMock.mock.calls.filter(([url]) => url === '/api/v1/agent-runs')).toHaveLength(1)
})

it('re-enables Run Agent and shows ApiError when submit fails', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({detail: {code: 'unavailable', message: 'Try again.'}}, 503)))
  renderAgentHarness()
  await userEvent.click(screen.getByRole('tab', {name: 'Agent Task'}))
  await userEvent.type(screen.getByLabelText('Agent task'), 'Inspect code')
  await userEvent.click(screen.getByRole('button', {name: 'Run Agent'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('Try again.')
  expect(screen.getByRole('button', {name: 'Run Agent'})).toBeEnabled()
})

it.each(['queued', 'running'])('blocks a new Agent submit while current run is %s', async status => {
  const fetchMock = vi.fn().mockResolvedValue(response({...queuedRun, status}))
  vi.stubGlobal('fetch', fetchMock)
  renderAgentHarness('/?runId=run-1')
  await userEvent.click(screen.getByRole('tab', {name: 'Agent Task'}))
  await userEvent.type(screen.getByLabelText('Agent task'), 'Inspect code')
  expect(await screen.findByText(`Current Agent run is ${status}. Wait for it to finish before starting another.`)).toBeVisible()
  expect(screen.getByRole('button', {name: 'Agent run in progress'})).toBeDisabled()
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
