import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { getAgentRun, getTrace } from '../../api/agentRuns'
import { TracePanel, QuestionProgress, BudgetProgress } from './TracePanel'

vi.mock('../../api/agentRuns', () => ({getAgentRun: vi.fn(), getTrace: vi.fn(),
  eventsUrl: (id: string) => `/api/v1/agent-runs/${id}/events`}))

function renderPanel() {
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}})
  return render(<QueryClientProvider client={client}><TracePanel projectId="p" runId="r" /></QueryClientProvider>)
}
const baseRun = {id: 'r', project_id: 'p', task: 'Inspect code', status: 'completed', result: 'Final answer',
  error_message: null, snapshot_id: 's', run_type: 'agent', created_at: '', started_at: null,
  core_started_at: null, finished_at: null}
afterEach(() => {vi.unstubAllGlobals(); vi.clearAllMocks()})

it('shows bounded execution counts and stop reason from a terminal replay event',()=>{
  const value={schema_version:1,limits:{max_turns:8,max_tool_calls:8,max_model_calls:2,max_query_embeddings:1,
    max_runtime_seconds:300,context_tokens:131072,tool_output_chars:32768},
    usage:{turns:1,tool_calls:1,model_calls:2,query_embeddings:1},elapsed_ms:2500,
    stop_reason:'budget_models',context_output_enforcement:'reserved_for_a05c',transport_attempts:'not_observed'}
  render(<BudgetProgress events={[{schema_version:1,sequence:2,event_type:'run_failed',
    occurred_at:'2026-01-01T00:00:00Z',tool_name:null,payload:{harness_budget:value}}]} />)
  expect(screen.getByRole('status')).toHaveTextContent('模型调用 2 / 2')
  expect(screen.getByRole('status')).toHaveTextContent('达到模型调用预算')
  expect(screen.getByRole('status')).toHaveTextContent('不代表实际计费请求')
})

it('keeps old traces without execution budgets readable',()=>{
  const {container}=render(<BudgetProgress events={[]} />)
  expect(container).toBeEmptyDOMElement()
})

it.each(['sufficient','insufficient','failed'] as const)('shows safe persisted preflight %s and explicit coverage', status => {
  const run={...baseRun,execution_metadata:{schema_version:3,initial_strategy:'fast',strategy:status==='insufficient'?'agent':'fast',
    preflight:{status,reasons:status==='insufficient'?['budget_limited']:[],pack_digest:'a'.repeat(64),
      selected_count:1,discarded_count:2,budget:4096,estimated_tokens:2000}}}
  render(<QuestionProgress run={run as never} events={[]} />)
  const value=screen.getByRole('status')
  expect(value).toHaveTextContent(status==='insufficient'?'已升级为智能体分析':status==='sufficient'?'继续快速回答':'未通过升级重试')
  expect(value).toHaveTextContent('舍弃 2 条')
  expect(value).toHaveTextContent('不代表实际模型用量')
})

it('shows upgrade immediately from authenticated trace before the next Run poll',()=>{
  render(<QuestionProgress run={baseRun as never} events={[{schema_version:1,sequence:1,event_type:'context_updated',
    occurred_at:'2026-01-01T00:00:00Z',tool_name:null,payload:{question_preflight:{status:'insufficient',reasons:['evidence_empty'],
      selected_count:0,discarded_count:0,budget:4096,estimated_tokens:100}}}]} />)
  expect(screen.getByRole('status')).toHaveTextContent('已升级为智能体分析')
  expect(screen.getByRole('status')).toHaveTextContent('没有可用证据')
})

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
  let sink!: ReadableStreamDefaultController<Uint8Array>
  let cancelled = false
  const body = new ReadableStream<Uint8Array>({start(controller) {sink=controller}, cancel() {cancelled=true}})
  const fetchMock = vi.fn().mockResolvedValue(new Response(body, {headers: {'Content-Type':'text/event-stream'}}))
  vi.stubGlobal('fetch', fetchMock)
  vi.mocked(getAgentRun).mockResolvedValue({...baseRun, status: 'running', result: null} as never)
  vi.mocked(getTrace).mockResolvedValue({run_id: 'r', events: []})
  renderPanel()
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
  await act(async () => sink.enqueue(new TextEncoder().encode('event: realtime_trace_gap\ndata: {}\n\n')))
  expect(cancelled).toBe(true)
  expect(await screen.findByText(/正在等待持久化轨迹/)).toBeVisible()
  expect(getTrace).toHaveBeenCalledWith('r')
})
