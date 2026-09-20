import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { WorkspacePage } from './WorkspacePage'
import { AnalysisHistoryPage } from './AnalysisHistoryPage'

const json = (value: unknown) => new Response(JSON.stringify(value), {
  status: 200, headers: {'Content-Type': 'application/json'},
})
function LocationProbe() {
  return <span data-testid="location">{useLocation().pathname}</span>
}
afterEach(() => {vi.unstubAllGlobals(); sessionStorage.clear()})

it('keeps History out of Workspace and navigates to the dedicated page', async () => {
  const fetchMock = vi.fn((url: string) => {
    if (url.includes('/analysis-history?')) return Promise.resolve(json({items: [{
      id: 'history-1', created_at: '2026-09-20T15:30:00Z', mode: 'repository',
      question: 'Where is request context?', answer: 'It is in src/flask/ctx.py.',
    }], has_more: false}))
    if (url.includes('/snapshots?')) return Promise.resolve(json({snapshots: []}))
    return Promise.resolve(json({
      id: 'p', name: 'Flask', repo_url: 'https://github.com/pallets/flask.git', branch: 'main',
      status: 'created', current_snapshot_id: null, created_at: '2026-09-20T00:00:00Z',
      updated_at: '2026-09-20T00:00:00Z', index_summary: null, last_error: null,
    }))
  })
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({defaultOptions: {queries: {retry: false}, mutations: {retry: false}}})
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/projects/p']}><LocationProbe /><Routes>
    <Route path="/projects/:projectId" element={<WorkspacePage />} />
    <Route path="/projects/:projectId/history" element={<AnalysisHistoryPage />} />
  </Routes></MemoryRouter></QueryClientProvider>)

  expect(await screen.findByRole('heading', {name: 'Flask'})).toBeVisible()
  expect(screen.getByText('https://github.com/pallets/flask.git')).toBeVisible()
  expect(screen.queryByText('Where is request context?')).not.toBeInTheDocument()
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/analysis-history?'))).toBe(false)

  await userEvent.click(screen.getByRole('button', {name: '查看历史记录'}))
  expect(await screen.findByTestId('location')).toHaveTextContent('/projects/p/history')
  expect(await screen.findByRole('heading', {name: '分析历史'})).toBeVisible()
  expect(screen.getByText('仓库问答')).toBeVisible()
  expect(screen.getByText('Where is request context?')).toBeVisible()
  expect(screen.getByText('It is in src/flask/ctx.py.')).toBeVisible()
  expect(screen.getByRole('link', {name: '← 返回项目'})).toHaveAttribute('href', '/projects/p')
  await waitFor(() => expect(fetchMock.mock.calls.some(
    ([url]) => String(url).includes('/analysis-history?'),
  )).toBe(true))
})
