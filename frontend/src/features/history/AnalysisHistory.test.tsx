import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { AnalysisHistory } from './AnalysisHistory'

const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: {'Content-Type': 'application/json'},
})
const item = (id: string, mode: 'repository'|'impact'|'agent', question: string, answer: string) => ({
  id, mode, question, answer, created_at: '2026-09-20T15:30:00Z',
})
function view() {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  return render(<QueryClientProvider client={client}><AnalysisHistory projectId="project-1" /></QueryClientProvider>)
}
afterEach(() => vi.unstubAllGlobals())

it('shows loading and then the empty state', async () => {
  let finish!: (value: Response) => void
  vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => {finish = resolve})))
  view()
  expect(screen.getByRole('status')).toHaveTextContent('正在加载历史记录…')
  finish(response({items: [], has_more: false}))
  expect(await screen.findByText('暂无分析历史')).toBeVisible()
})

it('renders every mode, questions and answers, and expands a long answer', async () => {
  const longAnswer = `${'Long answer section. '.repeat(30)}final text`
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({items: [
    item('1', 'repository', 'Where is routing?', 'Routing answer'),
    item('2', 'impact', 'src/main.py', 'Impact answer'),
    item('3', 'agent', 'Explain the flow', longAnswer),
  ], has_more: false})))
  view()
  expect(await screen.findByText('仓库问答')).toBeVisible()
  expect(screen.getByText('文件影响分析')).toBeVisible()
  expect(screen.getByText('智能体分析')).toBeVisible()
  expect(screen.getByText('Where is routing?')).toBeVisible()
  expect(screen.getByText('Routing answer')).toBeVisible()
  expect(screen.getAllByLabelText('我的提问')).toHaveLength(3)
  expect(screen.getAllByLabelText('PyCode 回答')).toHaveLength(3)
  expect(screen.queryByText(longAnswer)).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', {name: '展开回答'}))
  expect(screen.getByText(longAnswer)).toBeVisible()
  await userEvent.click(screen.getByRole('button', {name: '收起回答'}))
  expect(screen.queryByText(longAnswer)).not.toBeInTheDocument()
})

it('loads more and appends unique records without replacing the first page', async () => {
  const fetchMock = vi.fn((url: string) => Promise.resolve(url.includes('offset=1')
    ? response({items: [item('2', 'agent', 'Second question', 'Second answer'),
      item('1', 'repository', 'Duplicate question', 'Duplicate answer')], has_more: false})
    : response({items: [item('1', 'repository', 'First question', 'First answer')], has_more: true})))
  vi.stubGlobal('fetch', fetchMock)
  view()
  expect(await screen.findByText('First question')).toBeVisible()
  await userEvent.click(screen.getByRole('button', {name: '加载更多'}))
  expect(await screen.findByText('Second question')).toBeVisible()
  expect(screen.getByText('First question')).toBeVisible()
  expect(screen.queryByText('Duplicate question')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', {name: '加载更多'})).not.toBeInTheDocument()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    '/api/v1/projects/project-1/analysis-history?limit=20&offset=1',
    expect.anything(),
  ))
})

it('shows the isolated API error state', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({
    detail: {code: 'database_unavailable', message: 'Database unavailable.'},
  }, 503)))
  view()
  expect(await screen.findByRole('alert')).toHaveTextContent('历史记录加载失败')
})
