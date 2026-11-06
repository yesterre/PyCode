import { useCallback, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getAgentRun, getTrace } from '../../api/agentRuns'
import type { TraceEvent, AgentRun, QuestionPreflight, HarnessBudget, HarnessExecution, ToolExecution, HarnessContext, EvidenceVerification } from '../../api/types'
import { Badge, Button, Card, EmptyState, ErrorState, SectionHeading, Spinner, StatusBadge } from '../../components/ui'
import { llmActivities, orderedEvents, toolActivities } from './model'
import { useAgentTrace } from './useAgentTrace'
import { RunEvidence } from '../agent/RunEvidence'
import { traceEventLabel } from '../../presentation'

const terminal = (status?: string) => status === 'completed' || status === 'failed'
const text = (value: unknown) => typeof value === 'string' ? value : ''
const numberText = (value: unknown) => typeof value === 'number' ? String(value) : '—'
const errorText = (value: unknown) => {
  if (!value || typeof value !== 'object') return ''
  const item = value as {category?: string; code?: string; message?: string}
  return [item.category, item.code, item.message].filter(Boolean).join(' · ')
}
function TraceContents({events}: {events: TraceEvent[]}) {
  const ordered = orderedEvents(events)
  const plan = ordered.find(event => event.event_type === 'plan_created')
  const contexts = ordered.filter(event => event.event_type === 'context_updated')
  const llms = llmActivities(ordered)
  const tools = toolActivities(ordered)
  return <div className="mt-5 grid gap-5 xl:grid-cols-2">
    <BudgetProgress events={ordered} />
    <ExecutionProgress events={ordered} />
    <ContextProgress events={ordered} />
    <Card><SectionHeading title="执行计划" />{plan ? <><p className="mb-3 text-sm text-slate-300">来源：<strong>{text(plan.payload.source)}</strong> · 回退：{String(plan.payload.fallback_used ?? false)}</p><ol className="space-y-3">{(Array.isArray(plan.payload.steps) ? plan.payload.steps : []).map((step: {step_index: number; tool: string | null; reason: string; required: boolean}) => <li key={step.step_index} className="rounded-lg bg-slate-800/80 p-3 text-sm"><div className="font-semibold">{step.step_index + 1}. {step.tool ?? '操作'} {step.required && <Badge>必需</Badge>}</div><p className="mt-1 text-slate-400">{step.reason}</p></li>)}</ol></> : <EmptyState title="暂无计划事件" />}</Card>
    <Card><SectionHeading title="时间线" /><div className="space-y-2">{ordered.length ? ordered.map(event => <div key={event.sequence} className="flex gap-3 border-l-2 border-cyan-500/40 py-1 pl-3 text-sm"><span className="w-8 shrink-0 font-mono text-slate-500">{event.sequence}</span><span className="min-w-0 flex-1 text-slate-200">{traceEventLabel(event.event_type)}{event.tool_name ? ` · ${event.tool_name}` : ''}</span><time className="text-xs text-slate-500">{new Date(event.occurred_at).toLocaleTimeString('zh-CN')}</time></div>) : <EmptyState title="正在等待轨迹事件" />}</div></Card>
    <Card><SectionHeading title="模型调用" />{llms.length ? <div className="space-y-3">{llms.map((activity, index) => <div key={index} className="rounded-lg bg-slate-800/80 p-3 text-sm"><div className="flex justify-between gap-2"><strong>{text((activity.start ?? activity.finish)?.payload.purpose) || 'LLM'}</strong><StatusBadge status={activity.finish ? (activity.finish.payload.success ? 'completed' : 'failed') : 'running'} /></div><p className="mt-2 text-slate-400">{activity.finish ? `成功：${String(activity.finish.payload.success)} · 回退：${String(activity.finish.payload.fallback_used)}` : '运行中 / 尚未完成'}</p>{Boolean(activity.finish?.payload.error) && <p className="mt-2 text-rose-300">{errorText(activity.finish?.payload.error)}</p>}</div>)}</div> : <EmptyState title="暂无模型调用" />}</Card>
    <Card><SectionHeading title="工具调用" />{tools.length ? <div className="space-y-3">{tools.map((activity, index) => <div key={index} className="rounded-lg bg-slate-800/80 p-3 text-sm"><div className="flex justify-between gap-2"><strong>{activity.start?.tool_name ?? activity.finish?.tool_name}</strong><StatusBadge status={text(activity.finish?.payload.outcome) || 'running'} /></div>{Boolean(activity.start?.payload.arguments) && <pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-2 text-xs text-slate-300">{JSON.stringify(activity.start?.payload.arguments, null, 2)}</pre>}<p className="mt-2 text-slate-400">{text(activity.finish?.payload.summary) || (activity.finish ? '无摘要' : '运行中 / 尚未完成')}</p><ToolExecutionInfo value={activity.finish?.payload.execution} />{activity.finish && <p className="mt-1 text-xs text-slate-500">耗时：{numberText(activity.finish.payload.duration_ms)} ms</p>}{Boolean(activity.finish?.payload.error) && <p className="mt-2 text-rose-300">{errorText(activity.finish?.payload.error)}</p>}</div>)}</div> : <EmptyState title="暂无工具调用" />}</Card>
    <Card className="xl:col-span-2"><SectionHeading title="上下文" subtitle="仅展示公开区段标识，不展示上下文正文。" />{contexts.length ? <div className="space-y-3">{contexts.map(event => <div key={event.sequence} className="rounded-lg bg-slate-800/80 p-3 text-sm"><p className="mb-2 text-slate-400">轮次 {numberText(event.payload.turn_index)}</p><div className="flex flex-wrap gap-2">{(Array.isArray(event.payload.included_sections) ? event.payload.included_sections : []).map((section: string) => <Badge key={section} tone={section === 'evidence' ? 'good' : 'neutral'}>{section === 'evidence' ? '已包含证据上下文' : section}</Badge>)}</div>{Array.isArray(event.payload.skipped_sections) && event.payload.skipped_sections.length > 0 && <p className="mt-2 text-slate-400">已跳过：{event.payload.skipped_sections.join(', ')}</p>}{Array.isArray(event.payload.warnings) && event.payload.warnings.map((warning: string, index: number) => <p key={index} className="mt-2 text-amber-200">{warning}</p>)}</div>)}</div> : <EmptyState title="暂无上下文事件" />}</Card>
  </div>
}

export function QuestionProgress({run, events}: {run: AgentRun; events: TraceEvent[]}) {
  const stored = run.execution_metadata?.schema_version === 3 ? run.execution_metadata.preflight : null
  const latest = [...events].reverse().find(event => event.event_type === 'context_updated' && event.payload.question_preflight)
  const value = stored ?? latest?.payload.question_preflight as QuestionPreflight | undefined
  if (!value || !['sufficient','insufficient','failed'].includes(value.status)) return null
  const reasons: Record<string,string> = {evidence_empty:'没有可用证据',source_unavailable:'代码来源不可用',
    partial_coverage:'代码覆盖不完整',budget_limited:'部分证据因预算或数量限制舍弃',
    relations_unconfirmed:'静态关系不足以确认',preflight_failed:'检索预检失败'}
  return <div className="mb-3 rounded-lg border border-slate-700 p-3 text-sm" role="status">
    <p>{value.status === 'insufficient' ? '首轮证据不足，已升级为智能体分析，并保留首轮证据与原 Snapshot。' :
      value.status === 'sufficient' ? '首轮证据可用，继续快速回答。' : '首轮证据预检失败，本次运行未通过升级重试。'}</p>
    <p>{value.reasons?.map(reason => reasons[reason]).filter(Boolean).join('；')}</p>
    {typeof value.selected_count === 'number' && typeof value.discarded_count === 'number' && <p>选用 {value.selected_count} 条证据，舍弃 {value.discarded_count} 条。</p>}
    {value.budget != null && <p>证据预算估算：{value.estimated_tokens} / {value.budget}，不代表实际模型用量。</p>}
  </div>
}

export function ContextProgress({events}: {events:TraceEvent[]}) {
  const latest=[...events].reverse().find(event=>event.payload.harness_context)
  const context=latest?.payload.harness_context as HarnessContext|undefined
  const verification=latest?.payload.verification as EvidenceVerification|undefined
  if (!context || context.schema_version!==1) return null
  const reasons:Record<string,string>={context_budget:'总体输入预算',tool_output_limit:'单次工具输出上限',
    duplicate:'重复证据',invalid_evidence:'证据校验失败',auxiliary_context:'辅助上下文'}
  return <Card><SectionHeading title="上下文与证据覆盖" /><div role="status" className="space-y-1 text-sm">
    <p>输入估算 {context.estimated_tokens} / {context.budget}；不是计费 Token。</p>
    <p>选用 {context.selected_count} 条，舍弃 {context.discarded_count} 条。</p>
    {Object.entries(context.reason_counts).map(([reason,count])=><p key={reason}>{reasons[reason]??'其他限制'}：{count}</p>)}
    {context.estimated_tokens>context.budget && <p>最小输入仍超过预算，本次输入未交给模型。</p>}
    {context.references_omitted>0 && <p>另有 {context.references_omitted} 条选用引用未展示于此摘要。</p>}
    {verification && <p>{verification.status==='rejected'?'证据校验被拒绝。':'仅检查引用与来源，不证明回答正确。'}</p>}
  </div></Card>
}

export function ToolExecutionInfo({value}: {value: unknown}) {
  const execution = value as ToolExecution | undefined
  if (!execution || execution.schema_version !== 1) return null
  const labels:Record<ToolExecution['category'],string> = {success:'成功',empty:'无匹配结果',
    unknown_tool:'未知工具',invalid_arguments:'参数错误',permission_denied:'权限拒绝',not_found:'目标不存在',
    timeout:'超时',internal_error:'内部错误',transient_io:'瞬时读取错误',test_failed:'测试未通过',failed:'失败'}
  return <p className="mt-1 text-xs text-slate-400">{labels[execution.category]} · 执行尝试 {execution.attempts}
    {execution.attempts > 1 && ' · 已有限重试'}{execution.truncated && ' · 工具输出已截断'}</p>
}

export function ExecutionProgress({events}: {events: TraceEvent[]}) {
  const value = [...events].reverse().find(event => event.payload.harness_execution)?.payload.harness_execution as HarnessExecution | undefined
  if (!value || value.schema_version !== 1) return null
  const outcomes = {complete:'执行完成',partial:'执行部分完成',failed:'执行失败'}
  return <Card><SectionHeading title="工具执行结果" /><div role="status" className="space-y-1 text-sm">
    <p>{outcomes[value.outcome]}；完成 {value.completed_tools}，失败 {value.failed_tools}，空结果 {value.empty_tools}。</p>
    <p>执行尝试 {value.handler_attempts}；有限重试工具 {value.retried_tools}。</p>
    {value.stop_reason === 'no_progress' && <p>未取得新进展，已停止重复操作。</p>}
    {value.required_failed > 0 && <p>必需工具证据尚未取得，不能将答案视为执行成功。</p>}
    {value.outcome === 'partial' && <p>已取得的证据仍可查看；部分完成不代表回答已验证。</p>}
  </div></Card>
}

export function BudgetProgress({events}: {events: TraceEvent[]}) {
  const value = [...events].reverse().find(event => event.payload.harness_budget)?.payload.harness_budget as HarnessBudget | undefined
  if (!value || value.schema_version !== 1) return null
  const reasons: Record<string,string> = {max_turns:'达到轮次上限',budget_turns:'达到轮次预算',
    budget_tools:'达到工具调用预算',budget_models:'达到模型调用预算',budget_embeddings:'达到查询向量预算',
    budget_time:'达到运行时长预算',execution_lost:'执行所有权已丢失',budget_context:'达到整体上下文预算',evidence_invalid:'证据校验被拒绝'}
  return <Card><SectionHeading title="执行预算" /><div role="status" className="space-y-1 text-sm">
    <p>轮次 {value.usage.turns} / {value.limits.max_turns} · 工具 {value.usage.tool_calls} / {value.limits.max_tool_calls}</p>
    <p>模型调用 {value.usage.model_calls} / {value.limits.max_model_calls} · 查询向量 {value.usage.query_embeddings} / {value.limits.max_query_embeddings}</p>
    <p>耗时 {(value.elapsed_ms / 1000).toFixed(2)} / {value.limits.max_runtime_seconds} 秒</p>
    {value.stop_reason && <p>{reasons[value.stop_reason] ?? '执行已停止'}，已取得的证据仍可查看。</p>}
    <p className="text-slate-400">统计为逻辑调用次数，不代表实际计费请求、Token 或完整上下文用量。</p>
  </div></Card>
}

export function TracePanel({projectId, runId}: {projectId: string; runId: string}) {
  const queryClient = useQueryClient()
  const run = useQuery({queryKey: ['run', runId], queryFn: () => getAgentRun(runId),
    refetchInterval: query => terminal(query.state.data?.status) ? false : 4000})
  const belongs = run.data?.project_id === projectId
  const onTerminal = useCallback(() => {void queryClient.invalidateQueries({queryKey: ['run', runId]})}, [queryClient, runId])
  const trace = useAgentTrace(runId, !!belongs && !terminal(run.data?.status), onTerminal)
  const durable = useQuery({queryKey: ['trace', runId], queryFn: () => getTrace(runId),
    enabled: !!belongs && (terminal(run.data?.status) || trace.connection === 'gap' || trace.connection === 'historical'),
    refetchInterval: () => terminal(run.data?.status) || trace.connection !== 'gap' ? false : 4000})
  useEffect(() => {
    if (terminal(run.data?.status)) void queryClient.invalidateQueries({queryKey: ['trace', runId]})
  }, [run.data?.status, queryClient, runId])
  if (run.isLoading) return <Card><Spinner label="正在加载分析…" /></Card>
  if (run.error) return <Card><ErrorState error={run.error} /></Card>
  if (!belongs) return <Card><ErrorState error={new Error('该运行记录不属于当前项目。')} /></Card>
  const connection = terminal(run.data?.status) && durable.data ? 'historical' : trace.connection
  const events = durable.data && (terminal(run.data?.status) || trace.connection === 'gap' || trace.connection === 'historical') ? durable.data.events : trace.events
  return <section><Card><SectionHeading title="分析执行轨迹" subtitle={run.data?.task} action={<div className="flex gap-2"><StatusBadge status={run.data?.status ?? 'queued'} /><StatusBadge status={connection} /></div>} />
    {run.data?.execution_metadata && run.data.execution_metadata.schema_version !== 1 && <p className="mb-3 text-sm text-slate-300">
      执行方式：{run.data.execution_metadata.strategy === 'fast' ? '快速回答' : '智能体分析'} · 意图：{run.data.execution_metadata.intent}
      <span className="ml-2 text-xs text-slate-400">{run.data.execution_metadata.reasons.join(' · ')}</span>
    </p>}
    <QuestionProgress run={run.data!} events={events} />
    {trace.connection === 'gap' && <p role="alert" className="mb-3 rounded bg-amber-400/10 p-3 text-sm text-amber-200">实时轨迹数据不完整。{terminal(run.data?.status) ? '当前展示持久化轨迹。' : '正在等待持久化轨迹。'}</p>}
    {trace.connection === 'unavailable' && <div className="mb-3 flex items-center gap-3 rounded bg-amber-400/10 p-3 text-sm text-amber-200">实时连接暂时不可用，运行状态仍会继续更新。<Button variant="quiet" onClick={trace.retry}>重试实时连接</Button></div>}
    {trace.connection === 'reconnecting' && <p className="mb-3 text-sm text-amber-200">正在重新连接实时轨迹…</p>}
    {durable.error && <ErrorState error={durable.error} />}
    {terminal(run.data?.status) ? <div className="mt-4 rounded-lg border border-slate-700 p-4"><h3 className="mb-2 font-semibold">{run.data?.status === 'failed' ? '错误' : '分析结果'}</h3><p className="whitespace-pre-wrap text-sm leading-7 text-slate-300">{run.data?.status === 'failed' ? run.data.error_message ?? '运行失败，但没有错误信息。' : run.data?.result ?? '暂无结果'}</p></div> : <p className="text-sm text-slate-400">{run.data?.status === 'queued' ? '分析正在排队。' : '分析正在运行。'}</p>}
    {terminal(run.data?.status) && <RunEvidence run={run.data!} />}
    </Card><TraceContents events={events} />
  </section>
}

