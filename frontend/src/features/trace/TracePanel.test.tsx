import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { getAgentRun, getTrace } from '../../api/agentRuns'
import { TracePanel } from './TracePanel'

vi.mock('../../api/agentRuns', () => ({getAgentRun: vi.fn(), getTrace: vi.fn(),
  eventsUrl: (id: string) => `/api/v1/agent-runs/${id}/events`}))

class Source {
  static instances: Source[] = []
  listeners = new Map<string, () => void>()
  onopen = null
  onerror = null
  closed = false
  constructor() {Source.instances.push(this)}
  addEventListener(type: string, callback: EventListenerOrEventListenerObject) {
    this.listeners.set(type, callback as unknown as () => void)
  }
  close() {this.closed = true}
  emit(type: string) {this.listeners.get(type)?.()}
}
function renderPanel() {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  return render(<QueryClientProvider client={client}><TracePanel projectId="p" runId="r" /></QueryClientProvider>)
}
const baseRun = {id: 'r', project_id: 'p', task: 'Inspect code', status: 'completed', result: 'Final answer',
  error_message: null, snapshot_id: 's', run_type: 'agent', created_at: '', started_at: null,
  core_started_at: null, finished_at: null}
afterEach(() => {vi.unstubAllGlobals(); vi.clearAllMocks(); Source.instances = []})

it('restores durable Plan, Context, Tool and result for a completed run', async () => {
  vi.mocked(getAgentRun).mockResolvedValue(baseRun as never)
  vi.mocked(getTrace).mockResolvedValue({run_id: 'r', events: [
    {schema_version: 1, sequence: 0, event_type: 'plan_created', occurred_at: '2026-01-01T00:00:00Z', tool_name: null,
      payload: {source: 'rule', fallback_used: false, steps: [{step_index: 0, tool: 'read_file', reason: 'Inspect entry', required: true}]}},
    {schema_version: 1, sequence: 1, event_type: 'context_updated', occurred_at: '2026-01-01T00:00:01Z', tool_name: null,
      payload: {included_sections: ['evidence'], turn_index: 0}},
    {schema_version: 1, sequence: 2, event_type: 'tool_finished', occurred_at: '2026-01-01T00:00:02Z', tool_name: 'read_file',
      payload: {outcome: 'success', summary: 'Read main.py'}},
  ]})
  renderPanel()
  expect(await screen.findByText('Final answer')).toBeVisible()
  expect(await screen.findByText('Inspect entry')).toBeVisible()
  expect(screen.getByText('已包含证据上下文')).toBeVisible()
  expect(screen.getByText('Read main.py')).toBeVisible()
  expect(screen.getByText('历史记录', {exact: true})).toBeVisible()
  expect(screen.getByText('执行计划')).toBeVisible()
  expect(screen.getByText('工具调用')).toBeVisible()
  expect(screen.getByText('上下文')).toBeVisible()
})

it.each([
  ['success', 'Selected 6 context items.'],
  ['failed', 'Context unavailable.'],
])('renders one %s card for a started and finished tool', async (outcome, summary) => {
  vi.mocked(getAgentRun).mockResolvedValue(baseRun as never)
  vi.mocked(getTrace).mockResolvedValue({run_id: 'r', events: [
    {schema_version: 1, sequence: 1, event_type: 'tool_started', occurred_at: '2026-01-01T00:00:01Z',
      tool_name: 'retrieve_context', payload: {turn_index: 1, arguments: {intent: 'entry'}}},
    {schema_version: 1, sequence: 2, event_type: 'tool_finished', occurred_at: '2026-01-01T00:00:02Z',
      tool_name: 'retrieve_context', payload: {outcome, summary, duration_ms: 53}},
  ]} as never)
  renderPanel()
  expect(await screen.findByText(summary)).toBeVisible()
  expect(screen.getAllByText('retrieve_context', {exact: true})).toHaveLength(1)
  expect(screen.getByText('耗时：53 ms')).toBeVisible()
  expect(screen.queryByText('运行中 / 尚未完成')).not.toBeInTheDocument()
})

it('closes realtime stream and waits for durable trace after a gap', async () => {
  vi.stubGlobal('EventSource', Source)
  vi.mocked(getAgentRun).mockResolvedValue({...baseRun, status: 'running', result: null} as never)
  vi.mocked(getTrace).mockResolvedValue({run_id: 'r', events: []})
  renderPanel()
  await waitFor(() => expect(Source.instances).toHaveLength(1))
  act(() => Source.instances[0].emit('realtime_trace_gap'))
  expect(Source.instances[0].closed).toBe(true)
  expect(await screen.findByText(/正在等待持久化轨迹/)).toBeVisible()
  expect(getTrace).toHaveBeenCalledWith('r')
})
