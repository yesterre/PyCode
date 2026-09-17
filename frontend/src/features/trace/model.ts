import type { TraceEvent } from '../../api/types'

const TYPES = new Set(['run_started', 'plan_created', 'context_updated', 'llm_started', 'llm_finished',
  'tool_started', 'tool_finished', 'run_completed', 'run_failed'])
export function isTraceEvent(value: unknown): value is TraceEvent {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<TraceEvent>
  return item.schema_version === 1 && Number.isInteger(item.sequence) && (item.sequence ?? -1) >= 0 &&
    typeof item.event_type === 'string' && TYPES.has(item.event_type) &&
    typeof item.occurred_at === 'string' && (item.tool_name === null || typeof item.tool_name === 'string') &&
    !!item.payload && typeof item.payload === 'object' && !Array.isArray(item.payload)
}
export function mergeEvents(current: TraceEvent[], incoming: TraceEvent): TraceEvent[] {
  if (current.some(event => event.sequence === incoming.sequence)) return current
  return [...current, incoming].sort((a, b) => a.sequence - b.sequence)
}
export type Activity = {start?: TraceEvent; finish?: TraceEvent; key: string}
function pair(events: TraceEvent[], kind: 'llm'|'tool'): Activity[] {
  const output: Activity[] = []
  for (const event of orderedEvents(events)) {
    const started = event.event_type === `${kind}_started`
    const finished = event.event_type === `${kind}_finished`
    if (!started && !finished) continue
    const discriminator = kind === 'llm' ? String(event.payload.purpose ?? '') : String(event.tool_name ?? '')
    const turn = event.payload.turn_index
    const key = `${discriminator}:${String(turn ?? '')}`
    if (started) output.push({start: event, key})
    else {
      const open = output.filter(item => item.start && !item.finish &&
        (kind === 'tool' ? item.start.tool_name === event.tool_name : item.key === key))
      const target = kind === 'tool'
        ? (turn == null ? open[0] : open.find(item => item.start?.payload.turn_index === turn) ?? open[0])
        : open.at(-1)
      if (target) target.finish = event
      else output.push({finish: event, key})
    }
  }
  return output
}
export const llmActivities = (events: TraceEvent[]) => pair(events, 'llm')
export const toolActivities = (events: TraceEvent[]) => pair(events, 'tool')
export const orderedEvents = (events: TraceEvent[]) => [...events].sort((a, b) => a.sequence - b.sequence)
