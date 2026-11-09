import { afterEach, expect, it, vi } from 'vitest'
import { archiveProject, listProjects, restoreProject } from './projects'
import { getSnapshot, listSnapshots } from './snapshots'
import { submitQuestion } from './questions'
import { startIndex } from './tasks'
import { register } from './auth'
import { queryGraph } from './graph'

afterEach(() => vi.unstubAllGlobals())
it('matches lifecycle, snapshot, question, registration and graph contracts', async () => {
  const fetchMock = vi.fn(() => Promise.resolve(new Response('{}', {status: 200})))
  vi.stubGlobal('fetch', fetchMock)
  await listProjects(50, 100, 'archived')
  await listProjects(50, 0, 'all')
  await archiveProject('p/1'); await restoreProject('p/1')
  await listSnapshots('p/1'); await getSnapshot('p/1', 's/2')
  await submitQuestion('p/1', 'Inspect', 'question-key', {target: 'src/a.py', snapshot_id: 's', model: 'configured-model'})
  await startIndex('p/1', 'index-key')
  await register({account_name: 'alice', password: 'test password'})
  await queryGraph('p/1', 's/2', {file_path: 'src/a b.py', node_type: 'method', limit: 100})
  expect(fetchMock.mock.calls.map(call => (call as unknown[])[0])).toEqual([
    '/api/v1/projects?limit=50&offset=100&archive_status=archived', '/api/v1/projects?limit=50&offset=0&archive_status=all',
    '/api/v1/projects/p%2F1/archive', '/api/v1/projects/p%2F1/restore',
    '/api/v1/projects/p%2F1/snapshots?limit=20', '/api/v1/projects/p%2F1/snapshots/s%2F2',
    '/api/v1/projects/p%2F1/questions', '/api/v1/projects/p%2F1/index', '/api/v1/auth/register',
    '/api/v1/projects/p%2F1/snapshots/s%2F2/graph?file_path=src%2Fa+b.py&node_type=method&limit=100',
  ])
  const calls = fetchMock.mock.calls as unknown as [string, RequestInit][]
  expect(calls[2][1].method).toBe('POST'); expect(calls[3][1].method).toBe('POST')
  expect(JSON.parse(String(calls[6][1].body))).toEqual({question: 'Inspect', target: 'src/a.py', snapshot_id: 's', model: 'configured-model'})
  expect(new Headers(calls[6][1].headers).get('Idempotency-Key')).toBe('question-key')
  expect(new Headers(calls[7][1].headers).get('Idempotency-Key')).toBe('index-key')
  expect(JSON.parse(String(calls[8][1].body))).toEqual({account_name: 'alice', password: 'test password'})
})
