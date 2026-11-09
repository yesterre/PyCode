import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { GraphCallSites, GraphEdgeDetails, GraphKnowledgeSummary, graphElements } from './GraphPage'
import type { GraphResult } from '../api/types'

afterEach(cleanup)

it('keeps schema3 instance limitations distinct from controlled schema4 bindings', () => {
  const {rerender} = render(<GraphKnowledgeSummary graph={{snapshot_id: 'v3', nodes: [], edges: [], truncated: false, graph_schema_version: 3, graph_capabilities: ['import_bindings']}} />)
  expect(screen.getByText(/尚无局部实例绑定能力/)).toBeTruthy()
  rerender(<GraphKnowledgeSummary graph={{snapshot_id: 'v4', nodes: [], edges: [], truncated: false, graph_schema_version: 4, graph_capabilities: ['import_bindings', 'local_receiver_bindings']}} />)
  expect(screen.getByText(/实例和方法关系来自受控静态绑定/)).toBeTruthy()
  expect(screen.queryByText(/尚无局部实例绑定能力/)).toBeNull()
})

it('distinguishes v2 structural capability from v3 import binding', () => {
  const {rerender} = render(<GraphKnowledgeSummary graph={{snapshot_id: 'v2', nodes: [], edges: [], truncated: false, graph_schema_version: 2}} />)
  expect(screen.getByText(/尚无跨模块绑定能力/)).toBeTruthy()
  rerender(<GraphKnowledgeSummary graph={{snapshot_id: 'v3', nodes: [], edges: [], truncated: false, graph_schema_version: 3, graph_capabilities: ['import_bindings']}} />)
  expect(screen.getByText(/导入声明和实际符号引用分别记录/)).toBeTruthy()
  expect(screen.queryByText(/尚无跨模块绑定能力/)).toBeNull()
})

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

it.each([1, 2, 3, 4, 99])('safely displays schema %s metadata and warnings', version => {
  render(<GraphKnowledgeSummary graph={{snapshot_id: 's', nodes: [], edges: [], truncated: true,
    graph_schema_version: version, graph_capabilities: ['custom_capability'], graph_warnings: ['partial_graph'], call_sites_omitted: 7}} />)
  expect(screen.getByText(new RegExp(`Graph Schema：${version}`))).toHaveTextContent('custom_capability')
  expect(screen.getByText(/省略 Call Site：7/)).toHaveTextContent('结果截断：是')
  expect(screen.getByText('Graph 警告：partial_graph')).toBeVisible()
})

it.each(['unknown', 'ambiguous', 'legacy', 'future_resolution'])('keeps %s relationships uncertain', resolution => {
  render(<GraphEdgeDetails edge={{source: 'caller', target: 'candidate', type: 'unresolved_call', resolution,
    basis: 'binding_not_proven', site_id: 'site-1', path: 'main.py', start_line: 8, end_line: 9}} />)
  expect(screen.getByText('Source：caller')).toBeVisible()
  expect(screen.getByText('Target：candidate')).toBeVisible()
  expect(screen.getByText(/Resolution：/)).toHaveTextContent(/未确认|未经验证/)
  expect(screen.getByText('Site ID：site-1')).toBeVisible()
  expect(screen.getByText('起止行号：8–9')).toBeVisible()
})

it('displays call-site binding and flow evidence and their omissions', () => {
  render(<GraphCallSites graph={{snapshot_id: 's', nodes: [], edges: [], truncated: false, call_sites_omitted: 3,
    call_sites: [{id: 'site-1', path: 'main.py', caller_id: 'caller', target_id: null, start_line: 8, end_line: 9,
      resolution: 'ambiguous', basis: 'multiple_bindings', candidate_count: 2, binding_declarations: ['decl-1'],
      binding_chain_omitted: 4, flow_evidence: ['assign-1'], flow_evidence_omitted: 5}]}} />)
  screen.getByText(/main.py:8–9/).click()
  expect(screen.getByText('Binding Declarations：decl-1')).toBeVisible()
  expect(screen.getByText('Binding Chain Omitted：4')).toBeVisible()
  expect(screen.getByText('Flow Evidence：assign-1')).toBeVisible()
  expect(screen.getByText('Flow Evidence Omitted：5')).toBeVisible()
  expect(screen.getByText('Target：目标未解析')).toBeVisible()
  expect(screen.getByText('另有 3 个 Call Site 未展示。')).toBeVisible()
})

it('explains missing call sites in legacy responses', () => {
  render(<GraphCallSites graph={{snapshot_id: 's', nodes: [], edges: [], truncated: false}} />)
  expect(screen.getByText('该快照未提供此类信息')).toBeVisible()
})
