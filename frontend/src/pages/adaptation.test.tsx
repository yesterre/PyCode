import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { ProjectsPage } from './ProjectsPage'
import { WorkspacePage } from './WorkspacePage'
import { GraphPage } from './GraphPage'
import { AgentPanel, validTarget } from '../features/agent/AgentPanel'
import { useIndexTask } from '../features/projects/useIndexTask'

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status})
const project = {id: 'p', name: 'Demo', repo_url: 'https://example.com/demo.git', branch: 'main', status: 'ready',
  current_snapshot_id: 's', created_at: '2026-11-09T00:00:00Z', updated_at: '2026-11-09T00:00:00Z',
  index_summary: null, last_error: null, archived_at: null}
const snapshot = {id: 's', project_id: 'p', commit_sha: 'a'.repeat(40), status: 'ready',
  file_count: 1, node_count: 2, edge_count: 1, created_at: project.created_at, updated_at: project.updated_at,
  error_message: null, semantic_index: {status: 'disabled', provider: null, model: null, dimension: null,
    artifact: null, built_at: null, fingerprint: null, error_code: null, error_message: null, versions: []}}
const oldSnapshot = {...snapshot, id: 'old', commit_sha: 'b'.repeat(40)}
function mount(children: React.ReactNode, route = '/') {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}})
  const result = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[route]}>{children}</MemoryRouter></QueryClientProvider>)
  return {...result, client}
}
afterEach(() => {vi.unstubAllGlobals(); sessionStorage.clear()})

it('archives, restores, separates caches and refreshes project details', async () => {
  let archived = false
  const fetchMock = vi.fn((url: string, options?: RequestInit) => {
    if (options?.method === 'POST') {
      archived = url.endsWith('/archive')
      return Promise.resolve(json({...project, archived_at: archived ? project.created_at : null}))
    }
    const wantArchived = new URL(url, 'http://test').searchParams.get('archive_status') === 'archived'
    return Promise.resolve(json({projects: wantArchived === archived ? [{...project, archived_at: archived ? project.created_at : null}] : [], has_more: false}))
  })
  vi.stubGlobal('fetch', fetchMock)
  const {client} = mount(<ProjectsPage />)
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  const archive = await screen.findByRole('button', {name: '归档项目'})
  expect(archive.closest('a')).toBeNull()
  await userEvent.click(archive)
  expect(await screen.findByText('暂无项目')).toBeVisible()
  expect(client.getQueryData(['project', 'p'])).toMatchObject({archived_at: project.created_at})
  expect(invalidate).toHaveBeenCalledWith({queryKey: ['projects']})
  expect(invalidate).toHaveBeenCalledWith({queryKey: ['project', 'p']})
  await userEvent.click(screen.getByRole('button', {name: '已归档项目'}))
  expect(await screen.findByText('已归档 · 只读')).toBeVisible()
  await userEvent.click(screen.getByRole('button', {name: '恢复项目'}))
  await waitFor(() => expect(client.getQueryData(['project', 'p'])).toMatchObject({archived_at: null}))
  await userEvent.click(screen.getByRole('button', {name: '正常项目'}))
  expect(await screen.findByRole('button', {name: '归档项目'})).toBeVisible()
  expect(client.getQueryCache().findAll({queryKey: ['projects']})).toHaveLength(2)
})

it.each([false, true])('shows lifecycle errors without discarding the project (archived=%s)', async archived => {
  vi.stubGlobal('fetch', vi.fn((_: string, options?: RequestInit) => Promise.resolve(options?.method === 'POST'
    ? json({detail: {code: 'project_busy', message: 'Project is busy.'}}, 409)
    : json({projects: [{...project, archived_at: archived ? project.created_at : null}], has_more: false}))))
  mount(<ProjectsPage />)
  const label = archived ? '恢复项目' : '归档项目'
  await userEvent.click(await screen.findByRole('button', {name: label}))
  expect(await screen.findByRole('alert')).toHaveTextContent('Project is busy.')
  expect(screen.getByRole('link', {name: /Demo/})).toBeVisible()
  expect(screen.getByRole('button', {name: label})).toBeEnabled()
})

it('keeps lifecycle controls locked while a mutation is pending', async () => {
  let complete!: (value: Response) => void
  const pending = new Promise<Response>(resolve => {complete = resolve})
  const fetchMock = vi.fn((_: string, options?: RequestInit) => options?.method === 'POST'
    ? pending : Promise.resolve(json({projects: [project], has_more: false})))
  vi.stubGlobal('fetch', fetchMock)
  mount(<ProjectsPage />)
  const button = await screen.findByRole('button', {name: '归档项目'})
  fireEvent.click(button); fireEvent.click(button)
  expect(await screen.findByRole('button', {name: '正在更新…'})).toBeDisabled()
  expect(screen.getByRole('button', {name: '已归档项目'})).toBeDisabled()
  expect(fetchMock.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
  await act(async () => {complete(json({...project, archived_at: project.created_at}))})
  await waitFor(() => expect(screen.getByRole('button', {name: '已归档项目'})).toBeEnabled())
})

it('resets pagination when changing archive filters', async () => {
  const fetchMock = vi.fn(() => Promise.resolve(json({projects: [project], has_more: true})))
  vi.stubGlobal('fetch', fetchMock)
  mount(<ProjectsPage />)
  await userEvent.click(await screen.findByRole('button', {name: '下一页'}))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/projects?limit=50&offset=50&archive_status=active', expect.anything()))
  await userEvent.click(screen.getByRole('button', {name: '已归档项目'}))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/projects?limit=50&offset=0&archive_status=archived', expect.anything()))
})

function mockWorkspace(archived = false, historicalStatus = 'ready') {
  const fetchMock = vi.fn((url: string, options?: RequestInit) => {
    if (options?.method === 'POST') return Promise.resolve(json({run_id: 'r', task_id: 't', status: 'queued'}))
    if (url.endsWith('/snapshots?limit=20')) return Promise.resolve(json({snapshots: [snapshot, {...oldSnapshot, status: historicalStatus}]}))
    if (url.endsWith('/snapshots/old')) return Promise.resolve(json({...oldSnapshot, status: historicalStatus}))
    if (url.endsWith('/snapshots/s')) return Promise.resolve(json(snapshot))
    if (url.includes('/graph?')) return Promise.resolve(json({snapshot_id: 'old', nodes: [], edges: [], truncated: false}))
    return Promise.resolve(json({...project, archived_at: archived ? project.created_at : null}))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

it('selects historical snapshot details and binds the question to that snapshot and target', async () => {
  const fetchMock = mockWorkspace()
  mount(<Routes><Route path="/projects/:projectId" element={<WorkspacePage />} /></Routes>, '/projects/p')
  await userEvent.click(await screen.findByRole('button', {name: '查看快照 old'}))
  expect(await screen.findByRole('heading', {name: '历史快照'})).toBeVisible()
  await screen.findByText('b'.repeat(40))
  await userEvent.type(screen.getByLabelText('提问'), 'Inspect this version')
  await userEvent.type(screen.getByLabelText('分析目标（可选）'), 'src/a.py')
  await userEvent.click(screen.getByRole('button', {name: '开始分析'}))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/projects/p/questions', expect.objectContaining({
    body: JSON.stringify({question: 'Inspect this version', target: 'src/a.py', snapshot_id: 'old'}),
  })))
})

it('keeps archived workspaces read only while preserving history and graph entries', async () => {
  const fetchMock = mockWorkspace(true)
  mount(<Routes><Route path="/projects/:projectId" element={<WorkspacePage />} /></Routes>, '/projects/p')
  expect(await screen.findByText(/项目已归档，只读/)).toBeVisible()
  expect(screen.getByRole('button', {name: '重新构建索引'})).toBeDisabled()
  await userEvent.type(screen.getByLabelText('提问'), 'Inspect')
  expect(screen.getByRole('button', {name: '开始分析'})).toBeDisabled()
  expect(screen.getByRole('link', {name: '查看历史记录'})).toBeVisible()
  expect(screen.getByRole('link', {name: /打开代码图谱/})).toBeVisible()
  expect(fetchMock.mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
})

it('blocks questions for non-ready historical snapshots', async () => {
  mockWorkspace(false, 'failed')
  mount(<Routes><Route path="/projects/:projectId" element={<WorkspacePage />} /></Routes>, '/projects/p')
  await userEvent.click(await screen.findByRole('button', {name: '查看快照 old'}))
  await screen.findByText('b'.repeat(40))
  await userEvent.type(screen.getByLabelText('提问'), 'Inspect')
  expect(screen.getByRole('button', {name: '开始分析'})).toBeDisabled()
})

it('queries historical graph with exactly one encoded search mode', async () => {
  const fetchMock = mockWorkspace()
  mount(<Routes><Route path="/projects/:projectId/graph" element={<GraphPage />} /></Routes>, '/projects/p/graph')
  await screen.findByRole('option', {name: /历史快照 old/})
  await userEvent.selectOptions(screen.getByLabelText('Graph 快照'), 'old')
  await waitFor(() => expect(screen.getByRole('button', {name: '搜索图谱'})).toBeEnabled())
  await userEvent.selectOptions(screen.getByLabelText('搜索方式'), 'file_path')
  await userEvent.type(screen.getByLabelText('文件路径'), 'src/a b.py')
  await userEvent.click(screen.getByRole('button', {name: '搜索图谱'}))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/projects/p/snapshots/old/graph?file_path=src%2Fa+b.py&limit=100', expect.anything()))
})

it('reuses question keys after failure but changes them when the payload changes', async () => {
  const fetchMock = vi.fn().mockRejectedValue(new Error('offline'))
  vi.stubGlobal('fetch', fetchMock)
  mount(<AgentPanel projectId="p" snapshotId="s" onRun={vi.fn()} />)
  await userEvent.type(screen.getByLabelText('提问'), 'Inspect')
  await userEvent.click(screen.getByRole('button', {name: '开始分析'}))
  await screen.findByRole('alert')
  await userEvent.click(screen.getByRole('button', {name: '开始分析'}))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  const keyAt = (index: number) => new Headers(fetchMock.mock.calls[index][1].headers).get('Idempotency-Key')
  expect(keyAt(1)).toBe(keyAt(0))
  await userEvent.type(screen.getByLabelText('分析目标（可选）'), 'main.py')
  await userEvent.click(screen.getByRole('button', {name: '开始分析'}))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3))
  expect(keyAt(2)).not.toBe(keyAt(1))
})

it.each(['/etc/passwd', 'C:/private/a.py', '\\\\server\\a.py', '../a.py', 'a/../b.py', 'a:b', 'a//b'])('rejects unsafe target %s', value => {
  expect(validTarget(value)).toBe(false)
})
it('accepts relative paths and symbols', () => {
  expect(validTarget('src/main.py')).toBe(true); expect(validTarget('Service.method')).toBe(true)
})

it('shows a target validation error and sends no request for absolute paths', async () => {
  const fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  mount(<AgentPanel projectId="p" snapshotId="s" onRun={vi.fn()} />)
  await userEvent.type(screen.getByLabelText('提问'), 'Inspect')
  await userEvent.type(screen.getByLabelText('分析目标（可选）'), 'C:/private.py')
  await userEvent.click(screen.getByRole('button', {name: '开始分析'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('仓库相对路径或符号')
  expect(fetchMock).not.toHaveBeenCalled()
})

it('blocks graph queries for a failed current snapshot', async () => {
  const fetchMock = vi.fn((url: string) => Promise.resolve(json(url.endsWith('/snapshots?limit=20')
    ? {snapshots: [{...snapshot, status: 'failed'}]} : url.endsWith('/snapshots/s') ? {...snapshot, status: 'failed'} : project)))
  vi.stubGlobal('fetch', fetchMock)
  mount(<Routes><Route path="/projects/:projectId/graph" element={<GraphPage />} /></Routes>, '/projects/p/graph')
  await screen.findByLabelText('搜索符号或文件')
  await userEvent.type(screen.getByLabelText('搜索符号或文件'), 'main')
  expect(screen.getByRole('button', {name: '搜索图谱'})).toBeDisabled()
  expect(fetchMock.mock.calls.some(([url]) => url.includes('/graph?'))).toBe(false)
})

function IndexProbe({projectId}: {projectId: string}) {
  const index = useIndexTask(projectId)
  return <><button onClick={() => index.start.mutate()} disabled={index.start.isPending}>Index</button>
    {index.start.error && <span>Index failed</span>}{index.start.isSuccess && <span>Index accepted</span>}</>
}
it('reuses an index key for retries and creates fresh keys after success and for another project', async () => {
  let count = 0
  const fetchMock = vi.fn((url: string) => {
    if (url.includes('/tasks/')) return Promise.resolve(json({id: 't', status: 'completed'}))
    if (++count === 1) return Promise.reject(new Error('offline'))
    return Promise.resolve(json({task_id: 't', project_id: 'p', status: 'queued'}))
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}})
  const {rerender} = render(<QueryClientProvider client={client}><IndexProbe projectId="p" /></QueryClientProvider>)
  await userEvent.click(screen.getByRole('button', {name: 'Index'}))
  await screen.findByText('Index failed')
  await userEvent.click(screen.getByRole('button', {name: 'Index'}))
  await screen.findByText('Index accepted')
  await userEvent.click(screen.getByRole('button', {name: 'Index'}))
  await waitFor(() => expect(count).toBe(3))
  rerender(<QueryClientProvider client={client}><IndexProbe projectId="other" /></QueryClientProvider>)
  await userEvent.click(screen.getByRole('button', {name: 'Index'}))
  await waitFor(() => expect(count).toBe(4))
  const posts = fetchMock.mock.calls.filter(([url]) => url.includes('/index')) as unknown as [string, RequestInit][]
  const keys = posts.map(([, options]) => new Headers(options.headers).get('Idempotency-Key'))
  expect(keys[0]).toBeTruthy(); expect(keys[1]).toBe(keys[0]); expect(keys[2]).not.toBe(keys[1]); expect(keys[3]).not.toBe(keys[2])
  expect(posts[3][0]).toBe('/api/v1/projects/other/index')
})
