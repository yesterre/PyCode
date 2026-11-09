import { useEffect, useState, useSyncExternalStore, type FormEvent, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { getMe, login, register } from '../api/auth'
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
  const [registering, setRegistering] = useState(false)
  const [notice, setNotice] = useState('')
  useEffect(() => subscribeSession(() => {void client.cancelQueries(); client.clear(); setVerified(null)}), [client])
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (pending) return
    setPending(true); setError(null); setNotice(''); clearSession()
    try {
      if (registering) {
        const user = await register({account_name: account, password})
        setAccount(user.account_name); setPassword(''); setRegistering(false)
        setNotice('注册成功，请登录。')
        return
      }
      const result = await login({account_name: account, password})
      setPassword(''); setSession(result.access_token, result.expires_in)
      const current = sessionRevision()
      await getMe()
      if (current === sessionRevision() && accessToken()) setVerified(current)
    } catch (error) {clearSession(); setPassword(''); setError(error)}
    finally {setPending(false)}
  }
  if (!accessToken() || verified !== sessionRevision()) return <Card>
    <h1 className="mb-4 text-xl font-semibold">{registering ? '注册 PyCode' : '登录 PyCode'}</h1>
    <form onSubmit={submit} className="space-y-4">
      <Field label="账号" hint={registering ? '3–64 位英文字母、数字、下划线、点或连字符，以字母或数字开头；服务端会统一为小写。' : undefined}><input className={inputClass} autoComplete="username" required disabled={pending} value={account} onChange={e => setAccount(e.target.value)} /></Field>
      <Field label="密码" hint={registering ? '注册密码为 6–128 个字符，以服务端校验为准。' : undefined}><input className={inputClass} type="password" autoComplete={registering ? 'new-password' : 'current-password'} required disabled={pending} value={password} onChange={e => setPassword(e.target.value)} /></Field>
      {notice && <p role="status">{notice}</p>}
      {error != null && <ErrorState error={error} />}
      <Button disabled={pending} type="submit">{pending ? (registering ? '正在注册…' : '正在登录…') : registering ? '注册' : '登录'}</Button>
      <Button variant="quiet" disabled={pending} type="button" onClick={() => {setRegistering(!registering); setError(null); setNotice(''); setPassword('')}}>{registering ? '已有账号，前往登录' : '创建账号'}</Button>
    </form>
  </Card>
  return <><div className="mb-4 text-right"><Button variant="quiet" onClick={clearSession}>退出登录</Button></div>{children}</>
}
