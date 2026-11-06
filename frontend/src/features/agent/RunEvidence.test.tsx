import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import type { AgentRun } from '../../api/types'
import { RunEvidence } from './RunEvidence'

const run: AgentRun = {id: 'r', project_id: 'p', snapshot_id: 's',task: 'task',run_type: 'agent',status: 'completed',
  result: 'original answer',error_message: null,created_at: '',started_at: null,core_started_at: null,finished_at: null,
  result_metadata: {schema_version: 1,binding: {project_id:'p',snapshot_id:'s',commit_sha:'a'.repeat(40)},
    outcome:'completed',quality:'partial',intent:null,references_omitted:2,diagnostics:[],references:[{
      path:'main.py',symbol:'work',start_line:1,end_line:5,chunk_ids:[],chunk_ids_omitted:3,
      source:'graph',coverage:'complete_symbol',relation_resolution:'static'}]}}

it('keeps old null metadata silent and original consumer compatible', () => {
  const {container} = render(<RunEvidence run={{...run,result_metadata:null}} />)
  expect(container).toBeEmptyDOMElement()
})
it('shows snapshot references and explicit omissions without runtime claims', () => {
  render(<RunEvidence run={run} />)
  expect(screen.getByText(/main.py:1–5/)).toBeVisible()
  expect(screen.getByText(/不保证运行时调用/)).toBeVisible()
  expect(screen.getByText(/2 条证据引用/)).toBeVisible()
  expect(screen.getByText(/3 个 Chunk/)).toBeVisible()
  expect(screen.getByText(/此状态不评价回答正确性/)).toBeVisible()
})
it('hides unsupported or foreign metadata without displaying private references', () => {
  render(<RunEvidence run={{...run,result_metadata:{...run.result_metadata!,schema_version:99} as never}} />)
  expect(screen.getByRole('status')).toHaveTextContent('原始文本结果仍保留')
  expect(screen.queryByText(/main.py/)).not.toBeInTheDocument()
})
it('displays failed evidence as supporting references rather than an answer', () => {
  render(<RunEvidence run={{...run,status:'failed',result:null,
    result_metadata:{...run.result_metadata!,outcome:'failed',diagnostics:['execution_failed']}}} />)
  expect(screen.getByText(/不能替代最终答案/)).toBeVisible()
})

it('shows v2 actual context coverage and distinguishes static and memory sources',()=>{
  render(<RunEvidence run={{...run,result_metadata:{...run.result_metadata!,schema_version:2,
    context:{schema_version:1,binding:run.result_metadata!.binding,selected_count:2,discarded_count:3,
      estimated_tokens:3000,budget:8192,references:[{source:'static_graph'},{source:'memory_hint'}]},
    verification:{schema_version:1,status:'limited',warnings:['relation_unknown'],natural_language_verified:false}} as never}} />)
  expect(screen.getByText(/实际选用 2 条上下文单元，舍弃 3 条/)).toBeVisible()
  expect(screen.getByText(/不是计费 Token/)).toBeVisible()
  expect(screen.getByText(/记忆仅作为提示/)).toBeVisible()
  expect(screen.getByText(/不证明自然语言结论正确/)).toBeVisible()
})

it('rejects a v2 context with foreign snapshot before showing source references',()=>{
  render(<RunEvidence run={{...run,result_metadata:{...run.result_metadata!,schema_version:2,
    context:{schema_version:1,binding:{project_id:'p',snapshot_id:'foreign'}},
    verification:{schema_version:1}} as never}} />)
  expect(screen.getByRole('status')).toHaveTextContent('原始文本结果仍保留')
  expect(screen.queryByText(/main.py/)).not.toBeInTheDocument()
})
