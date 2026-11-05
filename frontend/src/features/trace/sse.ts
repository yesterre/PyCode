export type SSEFrame = {event: string; data: string; id: string | null}

export async function readSSE(body: ReadableStream<Uint8Array>, signal: AbortSignal,
                              consume: (frame: SSEFrame) => void) {
  const reader = body.getReader(), decoder = new TextDecoder()
  let buffer = '', event = '', data: string[] = [], id: string | null = null, dataLength = 0
  const abort = () => {void reader.cancel().catch(() => {})}
  signal.addEventListener('abort', abort, {once: true})
  try {
    while (!signal.aborted) {
      const {value, done} = await reader.read()
      if (done) break // An incomplete frame is never accepted as a full event.
      buffer += decoder.decode(value, {stream: true})
      if (buffer.length > 1048576) throw Error('SSE frame exceeds limit')
      let newline: number
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline).replace(/\r$/, '')
        buffer = buffer.slice(newline + 1)
        if (!line) {
          if (data.length) consume({event: event || 'message', data: data.join('\n'), id})
          event = ''; data = []; id = null; dataLength = 0
        } else if (!line.startsWith(':')) {
          const colon = line.indexOf(':')
          const field = colon < 0 ? line : line.slice(0, colon)
          const content = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '')
          if (field === 'event') event = content
          if (field === 'data') {data.push(content); dataLength += content.length + 1}
          if (field === 'id' && !content.includes('\0')) id = content
          if (dataLength > 1048576) throw Error('SSE frame exceeds limit')
        }
        if (signal.aborted) return
      }
    }
  } finally {signal.removeEventListener('abort', abort); await reader.cancel().catch(() => {}); reader.releaseLock()}
}
