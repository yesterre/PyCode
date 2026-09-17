import { describe, expect, it } from 'vitest'
import type { TraceEvent } from '../../api/types'
import { isTraceEvent, llmActivities, mergeEvents, toolActivities } from './model'

const event = (sequence: number, event_type: TraceEvent['event_type'], payload: Record<string, unknown> = {}, tool_name: string | null = null): TraceEvent =>
  ({schema_version: 1, sequence, event_type, occurred_at: '2026-01-01T00:00:00Z', tool_name, payload})

describe('trace presentation', () => {
  it('accepts only the nine public event types and deduplicates sequences', () => {
    expect(isTraceEvent(event(0, 'run_started'))).toBe(true)
    expect(isTraceEvent({...event(0, 'run_started'), event_type: 'realtime_trace_gap'})).toBe(false)
    const first = mergeEvents([], event(2, 'tool_finished'))
    expect(mergeEvents(first, event(2, 'tool_finished'))).toBe(first)
    expect(mergeEvents(first, event(1, 'tool_started')).map(item => item.sequence)).toEqual([1, 2])
  })
  it('pairs LLM and tool events by purpose/name, turn, and order without mutating raw events', () => {
    const raw = [event(0, 'llm_started', {purpose: 'plan', turn_index: 0}),
      event(1, 'llm_finished', {purpose: 'plan', turn_index: 0, success: true}),
      event(2, 'tool_started', {turn_index: 0, arguments: {path: 'main.py'}}, 'read_file'),
      event(3, 'tool_finished', {turn_index: 0, outcome: 'success'}, 'read_file'),
      event(4, 'tool_started', {turn_index: 1}, 'query_graph')]
    expect(llmActivities(raw)).toHaveLength(1)
    expect(llmActivities(raw)[0].finish?.sequence).toBe(1)
    expect(toolActivities(raw)).toHaveLength(2)
    expect(toolActivities(raw)[0].finish?.sequence).toBe(3)
    expect(toolActivities(raw)[1].finish).toBeUndefined()
    expect(raw).toHaveLength(5)
  })
  it('closes a tool card when finished lacks turn_index', () => {
    const raw = [event(4, 'tool_started', {turn_index: 2}, 'retrieve_context'),
      event(5, 'tool_finished', {outcome: 'success', summary: 'Selected 6 context items.', duration_ms: 53}, 'retrieve_context')]
    const cards = toolActivities(raw)
    expect(cards).toHaveLength(1)
    expect(cards[0].start?.sequence).toBe(4)
    expect(cards[0].finish?.sequence).toBe(5)
    expect(raw[0].payload).toEqual({turn_index: 2})
  })
  it('pairs failed and sequential same-name tool calls without crossing executions', () => {
    const raw = [event(1, 'tool_started', {turn_index: 1}, 'read_file'),
      event(2, 'tool_finished', {outcome: 'failed', summary: 'Not found'}, 'read_file'),
      event(3, 'tool_started', {turn_index: 2}, 'read_file'),
      event(4, 'tool_finished', {outcome: 'success', summary: 'Read file'}, 'read_file')]
    const cards = toolActivities(raw)
    expect(cards).toHaveLength(2)
    expect(cards.map(item => [item.start?.sequence, item.finish?.sequence])).toEqual([[1, 2], [3, 4]])
  })
  it('keeps an unmatched started tool visible as running', () => {
    const cards = toolActivities([event(1, 'tool_started', {turn_index: 1}, 'read_file')])
    expect(cards).toHaveLength(1)
    expect(cards[0].finish).toBeUndefined()
  })
})
