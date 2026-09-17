import { useCallback, useEffect, useRef, useState } from 'react'
import { eventsUrl } from '../../api/agentRuns'
import type { TraceEvent } from '../../api/types'
import { isTraceEvent, mergeEvents } from './model'

export type ConnectionState = 'idle'|'connecting'|'live'|'reconnecting'|'gap'|'unavailable'|'historical'
export function useAgentTrace(runId: string, active: boolean, onTerminal: () => void) {
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [connection, setConnection] = useState<ConnectionState>(active ? 'connecting' : 'idle')
  const [retryToken, setRetryToken] = useState(0)
  const callback = useRef(onTerminal)
  callback.current = onTerminal
  useEffect(() => {setEvents([]); setConnection(active ? 'connecting' : 'idle')}, [runId])
  useEffect(() => {
    if (!active) return
    let source: EventSource | null = new EventSource(eventsUrl(runId))
    let timeout: ReturnType<typeof setTimeout> | undefined
    let stopped = false
    setConnection('connecting')
    const close = () => {if (timeout) clearTimeout(timeout); source?.close(); source = null}
    source.onopen = () => {if (timeout) clearTimeout(timeout); timeout = undefined; setConnection('live')}
    source.onerror = () => {
      if (stopped) return
      setConnection('reconnecting')
      if (!timeout) timeout = setTimeout(() => {
        stopped = true; close(); setConnection('unavailable'); callback.current()
      }, 30000)
      callback.current()
    }
    source.addEventListener('trace', event => {
      try {
        const parsed: unknown = JSON.parse((event as MessageEvent).data)
        if (!isTraceEvent(parsed)) throw Error('Invalid trace event')
        setEvents(previous => mergeEvents(previous, parsed))
        if (parsed.event_type === 'run_completed' || parsed.event_type === 'run_failed') {
          stopped = true; close(); setConnection('historical'); callback.current()
        }
      } catch {
        stopped = true; close(); setEvents([]); setConnection('gap'); callback.current()
      }
    })
    source.addEventListener('realtime_trace_gap', () => {
      stopped = true; close(); setEvents([]); setConnection('gap'); callback.current()
    })
    return () => {stopped = true; close()}
  }, [runId, active, retryToken])
  const retry = useCallback(() => {setConnection('connecting'); setRetryToken(value => value + 1)}, [])
  return {events, connection, retry}
}
