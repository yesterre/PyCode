// Credentials live only in memory, never URLs or browser storage.
let token: string | null = null
let expiresAt = 0
let revision = 0
let timer: ReturnType<typeof setTimeout> | undefined
const listeners = new Set<() => void>()
const requests = new Set<AbortController>()
export const subscribeSession = (listener: () => void) => {listeners.add(listener); return () => {listeners.delete(listener)}}
export const sessionRevision = () => revision
export function clearSession() {
  token = null; expiresAt = 0; revision++
  if (timer) clearTimeout(timer)
  for (const controller of requests) controller.abort()
  requests.clear()
  for (let i = sessionStorage.length - 1; i >= 0; i--) {
    const key = sessionStorage.key(i)
    if (key?.startsWith('pycode:')) sessionStorage.removeItem(key)
  }
  listeners.forEach(listener => listener())
}
export function setSession(value: string, expiresIn: number) {
  clearSession()
  if (!value || !Number.isFinite(expiresIn) || expiresIn <= 0) return
  token = value; expiresAt = Date.now() + expiresIn * 1000; revision++
  timer = setTimeout(clearSession, Math.min(expiresIn * 1000, 2147483647))
  listeners.forEach(listener => listener())
}
export function accessToken() {
  if (token && Date.now() >= expiresAt) clearSession()
  return token
}
export function trackRequest(controller: AbortController) {
  requests.add(controller)
  return () => {requests.delete(controller)}
}
