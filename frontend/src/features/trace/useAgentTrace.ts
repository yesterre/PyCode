import { useCallback, useEffect, useRef, useState } from 'react'
import { eventsUrl } from '../../api/agentRuns'
import { authenticatedFetch } from '../../api/client'
import { sessionRevision, subscribeSession } from '../../auth/session'
import type { TraceEvent } from '../../api/types'
import { isTraceEvent, mergeEvents } from './model'
import { readSSE } from './sse'

export type ConnectionState = 'idle'|'connecting'|'live'|'reconnecting'|'gap'|'unavailable'|'historical'
export function useAgentTrace(runId: string, active: boolean, onTerminal: () => void) {
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [connection, setConnection] = useState<ConnectionState>(active ? 'connecting' : 'idle')
  const [retryToken, setRetryToken] = useState(0)
  const callback = useRef(onTerminal)
  const cursor = useRef<{runId: string; sequence: number}>({runId, sequence: -1})
  callback.current = onTerminal
  useEffect(() => {setEvents([]); cursor.current = {runId, sequence: -1}; setConnection(active ? 'connecting' : 'idle')}, [runId])
  useEffect(() => {
    if (!active) return
    const revision = sessionRevision()
    let stopped = false, attempts = 0
    let controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const stop = () => {stopped = true; controller.abort(); if (timer) clearTimeout(timer)}
    const unsubscribe = subscribeSession(() => {
      if (revision !== sessionRevision()) {stop(); setEvents([]); setConnection('unavailable')}
    })
    async function connect() {
      controller = new AbortController()
      const headerTimeout = setTimeout(() => controller.abort(), 30000)
      setConnection(attempts ? 'reconnecting' : 'connecting')
      try {
        const sequence = cursor.current.runId === runId ? cursor.current.sequence : -1
        const response = await authenticatedFetch(eventsUrl(runId), {signal: controller.signal,
          headers: {Accept: 'text/event-stream', ...(sequence >= 0 ? {'Last-Event-ID': String(sequence)} : {})}})
        clearTimeout(headerTimeout)
        if (stopped) {await response.body?.cancel(); return}
        if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
          if (response.status === 401 || response.status === 403 || response.status === 404) {
            stop(); setConnection('unavailable'); callback.current(); return
          }
          throw Error('Stream unavailable')
        }
        setConnection('live')
        await readSSE(response.body, controller.signal, frame => {
          if (stopped) return
          if (frame.event === 'realtime_trace_gap') {stop(); setEvents([]); setConnection('gap'); callback.current(); return}
          if (frame.event !== 'trace') return
          try {
            const parsed: unknown = JSON.parse(frame.data)
            if (!isTraceEvent(parsed) || frame.id !== String(parsed.sequence)) throw Error('Invalid trace frame')
            const previous = cursor.current.sequence
            if (parsed.sequence <= previous) return
            if (parsed.sequence !== previous + 1) throw Error('Sequence gap')
            cursor.current = {runId, sequence: parsed.sequence}
            setEvents(value => mergeEvents(value, parsed))
            if (parsed.event_type === 'run_completed' || parsed.event_type === 'run_failed') {
              stop(); setConnection('historical'); callback.current()
            }
          } catch {stop(); setEvents([]); setConnection('gap'); callback.current()}
        })
        if (stopped) return
        throw Error('Stream closed before terminal')
      } catch {
        if (stopped) return
        callback.current()
        if (++attempts > 3) {stop(); setConnection('unavailable'); return}
        setConnection('reconnecting'); timer = setTimeout(() => {void connect()}, Math.min(1000 * 2 ** (attempts - 1), 5000))
      } finally {clearTimeout(headerTimeout)}
    }
    void connect()
    return () => {stop(); unsubscribe()}
  }, [runId, active, retryToken])
  const retry = useCallback(() => {setConnection('connecting'); setRetryToken(value => value + 1)}, [])
  return {events, connection, retry}
}
