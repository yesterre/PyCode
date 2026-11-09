import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import type { HarnessContext, PublicRetrievalSummary, TraceEvent } from '../../api/types'
import { HarnessContextDetails } from '../agent/EvidenceDetails'
import { RetrievalDetails } from './RetrievalDetails'

it('displays only the real public retrieval projection including score and distance', () => {
  const retrieval: PublicRetrievalSummary = {strategy: 'impact', status: 'degraded', binding: null, mode: 'hybrid',
    duration_ms: 12.5, evidence_count: 1, references_omitted: 2, diagnostics: ['vector_unavailable'], graph_schema_version: 4,
    pack: {budget: 8192, estimated_tokens: 300, estimator: 'utf8_bytes_upper_bound_v1', selected_count: 1,
      discarded_count: 3, reason_counts: {token_budget: 3}}, references: [{path: 'main.py', symbol: 'work',
      start_line: 1, end_line: 5, chunk_ids: [], chunk_ids_omitted: 2, source: 'hybrid', coverage: 'complete_symbol',
      graph_distance: 2, relation_resolution: 'static', source_scores: [{source: 'keyword', score: 0.7, rank: 1, contribution: 0.3}]}]}
  const event: TraceEvent = {schema_version: 1, sequence: 1, event_type: 'context_updated', occurred_at: '',
    tool_name: null, payload: {retrieval: {...retrieval, internal_context: 'PRIVATE_CONTEXT_BODY'}}}
  render(<RetrievalDetails events={[event]} />)
  expect(screen.getByText(/模式：hybrid/)).toHaveTextContent('耗时：12.5 ms')
  expect(screen.getByText(/选用 1 条，舍弃 3 条/)).toBeVisible()
  expect(screen.getByText('舍弃原因 token_budget：3')).toBeVisible()
  expect(screen.getByText(/Source Score keyword：0.7/)).toBeVisible()
  expect(screen.getByText(/公开摘要提供 2 跳距离；未提供逐边路径/)).toBeVisible()
  expect(screen.getByText('省略检索引用：2')).toBeVisible()
  expect(screen.queryByText('PRIVATE_CONTEXT_BODY')).not.toBeInTheDocument()
})

it('shows public context references and decision reasons without internal context bodies', () => {
  const context: HarnessContext = {schema_version: 1, binding: null, phase: 'summary', delivered_to_model: true,
    budget: 8192, estimated_tokens: 300, estimator: 'utf8_bytes_upper_bound_v1', tool_output_chars: 4000,
    selected_count: 1, discarded_count: 1, reason_counts: {context_budget: 1}, references_omitted: 2,
    decisions_omitted: 3, pack_digests: [], references: [{id: 'ref', source: 'snapshot_source', path: 'main.py',
      symbol: 'work', start_line: 1, end_line: 5, coverage: 'partial'}],
    decisions: [{id: 'drop', reason: 'context_budget', reference: {source: 'static_graph', path: 'extra.py'}}]}
  render(<HarnessContextDetails context={context} />)
  screen.getByText('公开上下文引用与决策').click()
  expect(screen.getByLabelText('选中上下文引用')).toHaveTextContent('main.py work:1–5 · 来源：snapshot_source · Coverage：部分行范围')
  expect(screen.getByLabelText('上下文决策')).toHaveTextContent('原因：context_budget')
  expect(screen.getByText('省略选中引用：2')).toBeVisible()
  expect(screen.getByText('省略决策：3')).toBeVisible()
})
