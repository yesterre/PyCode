import { useEffect, useState, useSyncExternalStore, type FormEvent, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { request } from '../api/client'
import { accessToken, clearSession, sessionRevision, setSession, subscribeSession } from './session'
import { Button, Card, ErrorState, Field, inputClass } from '../components/ui'

export function AuthGate({children}: {children: ReactNode}) {
  const client = useQueryClient()
  useSyncExternalStore(subscribeSession, sessionRevision)
  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
  const [pending, setPending] = useState(false)
  const [verified, setVerified] = useState<number | null>(null)
  const [error, setError] = useState<unknown>(null)
  useEffect(() => subscribeSession(() => {void client.cancelQueries(); client.clear(); setVerified(null)}), [client])
  async function login(event: FormEvent) {
    event.preventDefault()
    if (pending) return
    setPending(true); setError(null); clearSession()
    try {
      const result = await request<{access_token: string; expires_in: number}>('/auth/login', {
        method: 'POST', body: JSON.stringify({account_name: account, password}),
      })
      setPassword(''); setSession(result.access_token, result.expires_in)
      const current = sessionRevision()
      await request('/auth/me')
      if (current === sessionRevision() && accessToken()) setVerified(current)
    } catch (error) {clearSession(); setPassword(''); setError(error)}
    finally {setPending(false)}
  }
  if (!accessToken() || verified !== sessionRevision()) return <Card>
    <h1 className="mb-4 text-xl font-semibold">登录 PyCode</h1>
    <form onSubmit={login} className="space-y-4">
      <Field label="账号"><input className={inputClass} autoComplete="username" required value={account} onChange={e => setAccount(e.target.value)} /></Field>
      <Field label="密码"><input className={inputClass} type="password" autoComplete="current-password" required value={password} onChange={e => setPassword(e.target.value)} /></Field>
      {error != null && <ErrorState error={error} />}
      <Button disabled={pending} type="submit">{pending ? '正在登录…' : '登录'}</Button>
    </form>
  </Card>
  return <><div className="mb-4 text-right"><Button variant="quiet" onClick={clearSession}>退出登录</Button></div>{children}</>
}
