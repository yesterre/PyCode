import { useCallback, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getAgentRun, getTrace } from '../../api/agentRuns'
import type { TraceEvent } from '../../api/types'
import { Badge, Button, Card, EmptyState, ErrorState, SectionHeading, Spinner, StatusBadge } from '../../components/ui'
import { llmActivities, orderedEvents, toolActivities } from './model'
import { useAgentTrace } from './useAgentTrace'

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
    <Card><SectionHeading title="Plan" />{plan ? <><p className="mb-3 text-sm text-slate-300">Source: <strong>{text(plan.payload.source)}</strong> · Fallback: {String(plan.payload.fallback_used ?? false)}</p><ol className="space-y-3">{(Array.isArray(plan.payload.steps) ? plan.payload.steps : []).map((step: {step_index: number; tool: string | null; reason: string; required: boolean}) => <li key={step.step_index} className="rounded-lg bg-slate-800/80 p-3 text-sm"><div className="font-semibold">{step.step_index + 1}. {step.tool ?? 'Action'} {step.required && <Badge>required</Badge>}</div><p className="mt-1 text-slate-400">{step.reason}</p></li>)}</ol></> : <EmptyState title="No plan event" />}</Card>
    <Card><SectionHeading title="Timeline" /><div className="space-y-2">{ordered.length ? ordered.map(event => <div key={event.sequence} className="flex gap-3 border-l-2 border-cyan-500/40 py-1 pl-3 text-sm"><span className="w-8 shrink-0 font-mono text-slate-500">{event.sequence}</span><span className="min-w-0 flex-1 text-slate-200">{event.event_type.replaceAll('_', ' ')}{event.tool_name ? ` · ${event.tool_name}` : ''}</span><time className="text-xs text-slate-500">{new Date(event.occurred_at).toLocaleTimeString()}</time></div>) : <EmptyState title="Waiting for trace events" />}</div></Card>
    <Card><SectionHeading title="LLM Activities" />{llms.length ? <div className="space-y-3">{llms.map((activity, index) => <div key={index} className="rounded-lg bg-slate-800/80 p-3 text-sm"><div className="flex justify-between gap-2"><strong>{text((activity.start ?? activity.finish)?.payload.purpose) || 'LLM'}</strong><StatusBadge status={activity.finish ? (activity.finish.payload.success ? 'completed' : 'failed') : 'running'} /></div><p className="mt-2 text-slate-400">{activity.finish ? `Success: ${String(activity.finish.payload.success)} · Fallback: ${String(activity.finish.payload.fallback_used)}` : 'Running / incomplete'}</p>{Boolean(activity.finish?.payload.error) && <p className="mt-2 text-rose-300">{errorText(activity.finish?.payload.error)}</p>}</div>)}</div> : <EmptyState title="No LLM activity" />}</Card>
    <Card><SectionHeading title="Tool Calls" />{tools.length ? <div className="space-y-3">{tools.map((activity, index) => <div key={index} className="rounded-lg bg-slate-800/80 p-3 text-sm"><div className="flex justify-between gap-2"><strong>{activity.start?.tool_name ?? activity.finish?.tool_name}</strong><StatusBadge status={text(activity.finish?.payload.outcome) || 'running'} /></div>{Boolean(activity.start?.payload.arguments) && <pre className="mt-2 overflow-x-auto rounded bg-slate-950 p-2 text-xs text-slate-300">{JSON.stringify(activity.start?.payload.arguments, null, 2)}</pre>}<p className="mt-2 text-slate-400">{text(activity.finish?.payload.summary) || (activity.finish ? 'No summary' : 'Running / incomplete')}</p>{activity.finish && <p className="mt-1 text-xs text-slate-500">Duration: {numberText(activity.finish.payload.duration_ms)} ms</p>}{Boolean(activity.finish?.payload.error) && <p className="mt-2 text-rose-300">{errorText(activity.finish?.payload.error)}</p>}</div>)}</div> : <EmptyState title="No tool calls" />}</Card>
    <Card className="xl:col-span-2"><SectionHeading title="Context" subtitle="Public section identifiers, not context body." />{contexts.length ? <div className="space-y-3">{contexts.map(event => <div key={event.sequence} className="rounded-lg bg-slate-800/80 p-3 text-sm"><p className="mb-2 text-slate-400">Turn {numberText(event.payload.turn_index)}</p><div className="flex flex-wrap gap-2">{(Array.isArray(event.payload.included_sections) ? event.payload.included_sections : []).map((section: string) => <Badge key={section} tone={section === 'evidence' ? 'good' : 'neutral'}>{section === 'evidence' ? 'Evidence context included' : section}</Badge>)}</div>{Array.isArray(event.payload.skipped_sections) && event.payload.skipped_sections.length > 0 && <p className="mt-2 text-slate-400">Skipped: {event.payload.skipped_sections.join(', ')}</p>}{Array.isArray(event.payload.warnings) && event.payload.warnings.map((warning: string, index: number) => <p key={index} className="mt-2 text-amber-200">{warning}</p>)}</div>)}</div> : <EmptyState title="No context event" />}</Card>
  </div>
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
  if (run.isLoading) return <Card><Spinner label="Loading Agent run…" /></Card>
  if (run.error) return <Card><ErrorState error={run.error} /></Card>
  if (!belongs) return <Card><ErrorState error={new Error('Run does not belong to this project.')} /><p className="mt-2 text-sm text-rose-200">Run does not belong to this project.</p></Card>
  const connection = terminal(run.data?.status) && durable.data ? 'historical' : trace.connection
  const events = durable.data && (terminal(run.data?.status) || trace.connection === 'gap' || trace.connection === 'historical') ? durable.data.events : trace.events
  return <section><Card><SectionHeading title="Agent Trace" subtitle={run.data?.task} action={<div className="flex gap-2"><StatusBadge status={run.data?.status ?? 'queued'} /><StatusBadge status={connection} /></div>} />
    {trace.connection === 'gap' && <p role="alert" className="mb-3 rounded bg-amber-400/10 p-3 text-sm text-amber-200">Realtime timeline became incomplete. {terminal(run.data?.status) ? 'Showing durable trace.' : 'Waiting for durable trace.'}</p>}
    {trace.connection === 'unavailable' && <div className="mb-3 flex items-center gap-3 rounded bg-amber-400/10 p-3 text-sm text-amber-200">Realtime unavailable. Run status will continue updating. <Button variant="quiet" onClick={trace.retry}>Retry realtime connection</Button></div>}
    {trace.connection === 'reconnecting' && <p className="mb-3 text-sm text-amber-200">Reconnecting to realtime trace…</p>}
    {durable.error && <ErrorState error={durable.error} />}
    {terminal(run.data?.status) ? <div className="mt-4 rounded-lg border border-slate-700 p-4"><h3 className="mb-2 font-semibold">{run.data?.status === 'failed' ? 'Error' : 'Result'}</h3><p className="whitespace-pre-wrap text-sm leading-7 text-slate-300">{run.data?.status === 'failed' ? run.data.error_message ?? 'Run failed without an error message.' : run.data?.result ?? 'No Result'}</p></div> : <p className="text-sm text-slate-400">{run.data?.status === 'queued' ? 'Agent queued.' : 'Agent running.'}</p>}
    </Card><TraceContents events={events} />
  </section>
}

