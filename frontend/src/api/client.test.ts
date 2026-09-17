import { describe, expect, it, vi } from 'vitest'
import { ApiError, apiUrl, parseError, request } from './client'

describe('API client', () => {
  it('builds same-origin API URLs by default', () => {
    expect(apiUrl('/projects')).toBe('/api/v1/projects')
  })
  it('parses public business errors', async () => {
    const error = await parseError(new Response(JSON.stringify({detail: {code: 'project_not_ready', message: 'Build index.'}}), {status: 409}))
    expect(error).toMatchObject({status: 409, code: 'project_not_ready', message: 'Build index.'})
  })
  it('parses FastAPI validation errors', async () => {
    const error = await parseError(new Response(JSON.stringify({detail: [{loc: ['body', 'name'], msg: 'Required'}]}), {status: 422}))
    expect(error).toMatchObject({code: 'validation_error', fields: ['body.name: Required']})
  })
  it('handles non-JSON and network failures', async () => {
    expect((await parseError(new Response('bad gateway', {status: 502}))).code).toBe('http_error')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
    await expect(request('/projects')).rejects.toMatchObject({code: 'network_error', status: null})
    vi.unstubAllGlobals()
  })
  it('exposes ApiError as an Error', () => {
    expect(new ApiError(404, 'missing', 'Not found')).toBeInstanceOf(Error)
  })
})
