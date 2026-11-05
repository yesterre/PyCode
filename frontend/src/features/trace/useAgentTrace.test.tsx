import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearSession, setSession } from '../../auth/session'
import { useAgentTrace } from './useAgentTrace'
import { readSSE } from './sse'

const frame = (sequence: number, event_type = 'run_started') => `id: ${sequence}\nevent: trace\ndata: ${JSON.stringify({schema_version: 1, sequence,
  event_type, occurred_at: '2026-01-01T00:00:00Z', tool_name: null, payload: {}})}\n\n`
function stream() {
  let sink!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({start(controller) {sink = controller}})
  return {response: new Response(body, {headers: {'Content-Type': 'text/event-stream'}}),
    send: (text: string) => sink.enqueue(new TextEncoder().encode(text)), end: () => sink.close()}
}
afterEach(() => {clearSession(); vi.unstubAllGlobals(); vi.useRealTimers()})

describe('authenticated useAgentTrace', () => {
  it('consumes expected native stream cancellation rejection without an unhandled promise', async () => {
    const controller = new AbortController()
    const body = new ReadableStream<Uint8Array>({cancel() {return Promise.reject(new DOMException('Aborted','AbortError'))}})
    const reading = readSSE(body,controller.signal,vi.fn())
    controller.abort()
    await expect(reading).resolves.toBeUndefined()
  })
  it('opens with Bearer, handles chunk boundaries, deduplicates and closes at terminal', async () => {
    setSession('test-only', 900)
    const source = stream(), fetchMock = vi.fn().mockResolvedValue(source.response)
    vi.stubGlobal('fetch', fetchMock)
    const done = vi.fn(), {result} = renderHook(() => useAgentTrace('run-1', true, done))
    await waitFor(() => expect(result.current.connection).toBe('live'))
    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/agent-runs/run-1/events')
    expect(new Headers(options.headers).get('Authorization')).toBe('Bearer test-only')
    expect(url).not.toContain('test-only')
    const first = frame(0)
    await act(async () => {source.send(first.slice(0, 15)); source.send(first.slice(15)); source.send(first)})
    expect(result.current.events).toHaveLength(1)
    await act(async () => source.send(frame(1, 'run_completed')))
    expect(result.current.connection).toBe('historical'); expect(done).toHaveBeenCalledTimes(1)
  })
  it.each(['event: realtime_trace_gap\ndata: {}\n\n', frame(3)])('clears fragments on explicit or implicit gap', async gap => {
    const source = stream(); vi.stubGlobal('fetch', vi.fn().mockResolvedValue(source.response))
    const done = vi.fn(), {result} = renderHook(() => useAgentTrace('run-2', true, done))
    await waitFor(() => expect(result.current.connection).toBe('live'))
    await act(async () => {source.send(frame(0)); source.send(gap)})
    expect(result.current).toMatchObject({connection: 'gap', events: []}); expect(done).toHaveBeenCalled()
  })
  it('reconnects with Last-Event-ID then stops after finite retries', async () => {
    vi.useFakeTimers()
    const first = stream(), second = stream()
    const fetchMock = vi.fn().mockResolvedValueOnce(first.response).mockResolvedValueOnce(second.response).mockRejectedValue(Error('offline'))
    vi.stubGlobal('fetch', fetchMock)
    const {result} = renderHook(() => useAgentTrace('run-3', true, vi.fn()))
    await act(async () => {await Promise.resolve(); first.send(frame(0)); first.end()})
    await act(async () => {await vi.advanceTimersByTimeAsync(1000)})
    expect(new Headers(fetchMock.mock.calls[1][1].headers).get('Last-Event-ID')).toBe('0')
    await act(async () => second.end())
    await act(async () => {await vi.advanceTimersByTimeAsync(30000)})
    expect(result.current.connection).toBe('unavailable')
  })
  it('cancels the old stream on run change and logout, without mixing events', async () => {
    setSession('user-a',900)
    const a=stream(), b=stream(); const fetchMock=vi.fn().mockResolvedValueOnce(a.response).mockResolvedValueOnce(b.response)
    vi.stubGlobal('fetch',fetchMock)
    const {result,rerender}=renderHook(({id})=>useAgentTrace(id,true,vi.fn()),{initialProps:{id:'a'}})
    await waitFor(()=>expect(result.current.connection).toBe('live'))
    await act(async()=>a.send(frame(0)))
    rerender({id:'b'})
    await waitFor(()=>expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(result.current.events).toEqual([])
    await act(async()=>{clearSession()})
    expect(fetchMock.mock.calls[1][1].signal.aborted).toBe(true)
    expect(result.current.events).toEqual([])
  })
})
