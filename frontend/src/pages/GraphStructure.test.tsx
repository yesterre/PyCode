import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { GraphKnowledgeSummary, graphElements } from './GraphPage'
import type { GraphResult } from '../api/types'

afterEach(cleanup)

it('keeps legacy capability limits explicit without claiming an upgrade', () => {
  render(<GraphKnowledgeSummary graph={{snapshot_id: 'old', nodes: [{id: 'f', type: 'file', name: 'a.py', path: 'a.py'}], edges: [], truncated: false}} />)
  expect(screen.getByRole('status').textContent).toContain('不会自动原地升级')
  expect(screen.getByText(/不保证实际运行时调用/)).toBeTruthy()
})

it('does not infer a version from the compatible empty-result shape', () => {
  render(<GraphKnowledgeSummary graph={{snapshot_id: 'empty', nodes: [], edges: [], truncated: false}} />)
  expect(screen.queryByRole('status')).toBeNull()
})

it('shows static uncertainty for v2 and retains call resolution metadata', () => {
  const graph: GraphResult = {snapshot_id: 'new', graph_schema_version: 2, truncated: false,
    nodes: [{id: 'f', type: 'function', name: 'f', path: 'a.py', start_line: 1, end_line: 3},
      {id: 'u', type: 'external', name: 'unknown', path: null}],
    edges: [{source: 'f', target: 'u', type: 'unresolved_call', resolution: 'unknown', basis: 'receiver_binding_deferred'}]}
  render(<GraphKnowledgeSummary graph={graph} />)
  expect(screen.queryByRole('status')).toBeNull()
  expect(screen.getByText(/虚线调用表示目标尚未确认/)).toBeTruthy()
  expect(graphElements(graph)[2].data).toMatchObject({type: 'unresolved_call', resolution: 'unknown', basis: 'receiver_binding_deferred'})
})
