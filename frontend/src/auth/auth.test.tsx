import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import { AuthGate } from './AuthGate'
import { accessToken, clearSession, setSession } from './session'
import { request } from '../api/client'

afterEach(()=>{clearSession();vi.unstubAllGlobals();vi.useRealTimers()})
const response=(data: unknown,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}})

it('logs in, verifies me with Bearer, and removes private cached data on logout', async()=>{
  const client=new QueryClient({defaultOptions:{queries:{retry:false}}})
  const fetchMock=vi.fn().mockResolvedValueOnce(response({access_token:'test-only',expires_in:900})).mockResolvedValueOnce(response({id:'a',account_name:'alice'}))
  vi.stubGlobal('fetch',fetchMock)
  render(<QueryClientProvider client={client}><AuthGate><p>Private content</p></AuthGate></QueryClientProvider>)
  await userEvent.type(screen.getByLabelText('账号'),'alice')
  await userEvent.type(screen.getByLabelText('密码'),'test password')
  await userEvent.click(screen.getByRole('button',{name:'登录'}))
  expect(await screen.findByText('Private content')).toBeVisible()
  expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/auth/me')
  expect(new Headers(fetchMock.mock.calls[1][1].headers).get('Authorization')).toBe('Bearer test-only')
  client.setQueryData(['private'],'PRIVATE')
  sessionStorage.setItem('pycode:index-task:p','task-a')
  await userEvent.click(screen.getByRole('button',{name:'退出登录'}))
  expect(screen.queryByText('Private content')).not.toBeInTheDocument()
  expect(client.getQueryData(['private'])).toBeUndefined()
  expect(sessionStorage.getItem('pycode:index-task:p')).toBeNull()
  expect(accessToken()).toBeNull()
  expect(JSON.stringify(localStorage)).not.toContain('test-only')
})

it('expires tokens and clears authentication on 401 without putting credentials in URLs',async()=>{
  vi.useFakeTimers();setSession('expired',1)
  vi.advanceTimersByTime(1001);expect(accessToken()).toBeNull();vi.useRealTimers()
  setSession('user-a',900)
  const fetchMock=vi.fn().mockResolvedValue(response({detail:{code:'unauthenticated',message:'Authentication failed.'}},401))
  vi.stubGlobal('fetch',fetchMock)
  await expect(request('/projects')).rejects.toMatchObject({status:401})
  expect(accessToken()).toBeNull()
  expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/projects')
})

it('rejects late responses after a user change',async()=>{
  setSession('a',900)
  let complete!: (response:Response)=>void
  vi.stubGlobal('fetch',vi.fn().mockImplementation(()=>new Promise(resolve=>{complete=resolve})))
  const promise=request('/projects'); const rejected=expect(promise).rejects.toBeDefined()
  setSession('b',900);complete(response({projects:['PRIVATE_A']}))
  await rejected
  expect(accessToken()).toBe('b')
})
