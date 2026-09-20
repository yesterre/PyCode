import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { analysisHistoryKey, askProject, impactProject } from '../../api/projects'
import { getAgentRun, submitAgent } from '../../api/agentRuns'
import type { Analysis } from '../../api/types'
import { Badge, Button, Card, ErrorState, Field, inputClass, SectionHeading, Spinner } from '../../components/ui'
import { statusLabel } from '../../presentation'

type Mode = 'question' | 'impact' | 'agent'
const descriptions: Record<Mode, string> = {
  question: '针对当前仓库快速提问，并基于代码提供回答和相关证据。',
  impact: '分析指定文件发生变化后可能影响的代码和依赖关系。',
  agent: '由智能体制定计划、调用工具并进行多步骤仓库分析。',
}
export function AgentPanel({projectId, runId = null, onRun}: {projectId: string; runId?: string | null; onRun: (runId: string) => void}) {
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<Mode>('question')
  const [input, setInput] = useState('')
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [evidenceExpanded, setEvidenceExpanded] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const submitLock = useRef(false)
  const submittedRunId = useRef<string | null>(null)
  const run = useQuery({queryKey: ['run', runId], queryFn: () => getAgentRun(runId!), enabled: !!runId})
  const activeRun = !!runId && run.data?.status !== 'completed' && run.data?.status !== 'failed'
  useEffect(() => {
    if (runId && submittedRunId.current === runId) {
      submitLock.current = false
      submittedRunId.current = null
      setSubmitting(false)
    }
  }, [runId])
  useEffect(() => {
    if (run.data?.status === 'completed' && run.data.result != null) {
      void queryClient.invalidateQueries({queryKey: analysisHistoryKey(projectId)})
    }
  }, [projectId, queryClient, run.data?.result, run.data?.status])
  const mutation = useMutation({mutationFn: async ({mode, input}: {mode: Mode; input: string}) => {
    if (mode === 'question') return {kind: 'analysis' as const, value: await askProject(projectId, input)}
    if (mode === 'impact') return {kind: 'analysis' as const, value: await impactProject(projectId, input)}
    return {kind: 'run' as const, value: await submitAgent(projectId, input)}
  }, onSuccess: result => {
    if (result.kind === 'analysis') {
      setAnalysis(result.value)
      setEvidenceExpanded(false)
      void queryClient.invalidateQueries({queryKey: analysisHistoryKey(projectId)})
    }
    else {setAnalysis(null); submittedRunId.current = result.value.run_id; onRun(result.value.run_id)}
  }, onError: (_error, variables) => {
    if (variables.mode === 'agent') {submitLock.current = false; setSubmitting(false)}
  }})
  function switchMode(next: Mode) {setMode(next); setInput(''); setAnalysis(null); setEvidenceExpanded(false); mutation.reset()}
  function submit(event: FormEvent) {
    event.preventDefault()
    if (!input.trim() || mutation.isPending) return
    if (mode === 'agent') {
      if (submitLock.current || activeRun) return
      submitLock.current = true
      setSubmitting(true)
    }
    mutation.mutate({mode, input: input.trim()})
  }
  return <Card><SectionHeading title="分析仓库" subtitle="选择适合本次问题的分析方式。" />
    <div role="tablist" aria-label="分析方式" className="mb-5 flex flex-wrap gap-2">
      {([['question', '仓库问答'], ['impact', '文件影响分析'], ['agent', '智能体分析']] as const).map(([value, label]) => <button key={value} role="tab" aria-selected={mode === value} onClick={() => switchMode(value)} className={`rounded-lg px-3 py-2 text-sm ${mode === value ? 'bg-cyan-400 text-slate-950 font-semibold' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}>{label}</button>)}
    </div>
    <p className="mb-5 text-sm text-slate-300">{descriptions[mode]}</p>
    <form onSubmit={submit} className="space-y-4">
      {mode === 'impact' ? <Field label="文件路径" hint="请输入仓库内的相对路径，例如 src/main.py。"><input className={inputClass} required value={input} onChange={e => setInput(e.target.value)} placeholder="src/main.py" /></Field> :
        <Field label={mode === 'agent' ? '分析任务' : '提问'}><textarea className={`${inputClass} min-h-24 resize-y`} required value={input} onChange={e => setInput(e.target.value)} placeholder={mode === 'agent' ? '说明请求流程及其依赖关系' : '这个仓库的入口在哪里？'} /></Field>}
      {mutation.error && <ErrorState error={mutation.error} />}
      {mode === 'agent' && submitting && <Spinner label="正在提交智能体分析…" />}
      {mode === 'agent' && activeRun && <p className="text-sm text-amber-200">当前智能体分析状态：{statusLabel(run.data?.status ?? 'running')}。请等待本次分析结束后再开始新的任务。</p>}
      <Button type="submit" disabled={mutation.isPending || submitting || (mode === 'agent' && activeRun) || !input.trim()}>{mode === 'agent' ? submitting ? '正在提交…' : activeRun ? '智能体分析进行中' : '开始智能体分析' : mutation.isPending ? '正在分析…' : '开始分析'}</Button>
    </form>
    {analysis && <div className="mt-6 space-y-4 border-t border-slate-700 pt-5"><div className="flex items-center gap-2"><h3 className="font-semibold">回答</h3><Badge>{analysis.intent}</Badge></div><p className="whitespace-pre-wrap text-sm leading-7 text-slate-200">{analysis.answer}</p>{analysis.evidence.length > 0 && <div className="border-t border-slate-800 pt-3"><div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-sm font-semibold text-slate-300">证据（{analysis.evidence.length}）</h4><Button variant="quiet" aria-expanded={evidenceExpanded} onClick={() => setEvidenceExpanded(value => !value)}>{evidenceExpanded ? '收起证据' : '展开证据'}</Button></div>{evidenceExpanded && <ul className="mt-3 space-y-2 text-sm text-slate-300">{analysis.evidence.map((item, index) => <li key={`${index}-${item}`} className="break-words rounded-lg bg-slate-800/80 px-3 py-2 font-mono text-xs">{item}</li>)}</ul>}</div>}</div>}
  </Card>
}
