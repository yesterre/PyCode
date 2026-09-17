import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useAgentTrace } from './useAgentTrace'

class FakeEventSource {
  static instances: FakeEventSource[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  listeners = new Map<string, (event: {data: string}) => void>()
  constructor(public url: string) {FakeEventSource.instances.push(this)}
  addEventListener(type: string, callback: EventListenerOrEventListenerObject) {
    this.listeners.set(type, callback as unknown as (event: {data: string}) => void)
  }
  close() {this.closed = true}
  emit(type: string, data = '') {this.listeners.get(type)?.({data})}
}

afterEach(() => {vi.unstubAllGlobals(); vi.useRealTimers(); FakeEventSource.instances = []})
const frame = (sequence: number, event_type: string) => JSON.stringify({schema_version: 1, sequence,
  event_type, occurred_at: '2026-01-01T00:00:00Z', tool_name: null,
  payload: event_type === 'run_completed' ? {stop_reason: 'done', has_result: true, trace_complete: true} : {run_type: 'agent', snapshot_id: 's'}})

describe('useAgentTrace', () => {
  it('opens, deduplicates, and closes on terminal', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const done = vi.fn()
    const {result} = renderHook(() => useAgentTrace('run-1', true, done))
    const source = FakeEventSource.instances[0]
    expect(source.url).toContain('/agent-runs/run-1/events')
    act(() => source.onopen?.())
    expect(result.current.connection).toBe('live')
    act(() => {source.emit('trace', frame(0, 'run_started')); source.emit('trace', frame(0, 'run_started'))})
    expect(result.current.events).toHaveLength(1)
    act(() => source.emit('trace', frame(1, 'run_completed')))
    expect(source.closed).toBe(true)
    expect(result.current.connection).toBe('historical')
    expect(done).toHaveBeenCalled()
  })
  it('clears realtime fragments on gap', () => {
    vi.stubGlobal('EventSource', FakeEventSource)
    const {result} = renderHook(() => useAgentTrace('run-2', true, vi.fn()))
    const source = FakeEventSource.instances[0]
    act(() => source.emit('trace', frame(0, 'run_started')))
    act(() => source.emit('realtime_trace_gap'))
    expect(source.closed).toBe(true)
    expect(result.current).toMatchObject({connection: 'gap', events: []})
  })
  it('allows native reconnect then stops after finite timeout', () => {
    vi.useFakeTimers()
    vi.stubGlobal('EventSource', FakeEventSource)
    const {result} = renderHook(() => useAgentTrace('run-3', true, vi.fn()))
    const source = FakeEventSource.instances[0]
    act(() => source.onerror?.())
    expect(result.current.connection).toBe('reconnecting')
    act(() => vi.advanceTimersByTime(30000))
    expect(source.closed).toBe(true)
    expect(result.current.connection).toBe('unavailable')
  })
})
