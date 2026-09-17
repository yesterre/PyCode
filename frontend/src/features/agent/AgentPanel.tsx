import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { askProject, impactProject } from '../../api/projects'
import { getAgentRun, submitAgent } from '../../api/agentRuns'
import type { Analysis } from '../../api/types'
import { Badge, Button, Card, EmptyState, ErrorState, Field, inputClass, SectionHeading, Spinner } from '../../components/ui'

type Mode = 'question' | 'impact' | 'agent'
const descriptions: Record<Mode, string> = {
  question: 'Quickly answer a specific repository question with supporting evidence.',
  impact: 'Analyze what may be affected when a specific file changes.',
  agent: 'Let the Agent plan and use tools for a multi-step analysis with live trace.',
}
export function AgentPanel({projectId, runId = null, onRun}: {projectId: string; runId?: string | null; onRun: (runId: string) => void}) {
  const [mode, setMode] = useState<Mode>('question')
  const [input, setInput] = useState('')
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
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
  const mutation = useMutation({mutationFn: async ({mode, input}: {mode: Mode; input: string}) => {
    if (mode === 'question') return {kind: 'analysis' as const, value: await askProject(projectId, input)}
    if (mode === 'impact') return {kind: 'analysis' as const, value: await impactProject(projectId, input)}
    return {kind: 'run' as const, value: await submitAgent(projectId, input)}
  }, onSuccess: result => {
    if (result.kind === 'analysis') setAnalysis(result.value)
    else {setAnalysis(null); submittedRunId.current = result.value.run_id; onRun(result.value.run_id)}
  }, onError: (_error, variables) => {
    if (variables.mode === 'agent') {submitLock.current = false; setSubmitting(false)}
  }})
  function switchMode(next: Mode) {setMode(next); setInput(''); setAnalysis(null); mutation.reset()}
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
  return <Card><SectionHeading title="Analyze repository" subtitle="Choose the workflow that fits your question." />
    <div role="tablist" aria-label="Analysis mode" className="mb-5 flex flex-wrap gap-2">
      {([['question', 'Repository Question'], ['impact', 'Impact Analysis'], ['agent', 'Agent Task']] as const).map(([value, label]) => <button key={value} role="tab" aria-selected={mode === value} onClick={() => switchMode(value)} className={`rounded-lg px-3 py-2 text-sm ${mode === value ? 'bg-cyan-400 text-slate-950 font-semibold' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}>{label}</button>)}
    </div>
    <p className="mb-5 text-sm text-slate-300">{descriptions[mode]}</p>
    <form onSubmit={submit} className="space-y-4">
      {mode === 'impact' ? <Field label="File Path" hint="A path relative to the repository, such as src/main.py."><input className={inputClass} required value={input} onChange={e => setInput(e.target.value)} placeholder="src/main.py" /></Field> :
        <Field label={mode === 'agent' ? 'Agent task' : 'Question'}><textarea className={`${inputClass} min-h-24 resize-y`} required value={input} onChange={e => setInput(e.target.value)} placeholder={mode === 'agent' ? 'Explain the request flow and its dependencies' : 'Where does this repository start?'} /></Field>}
      {mutation.error && <ErrorState error={mutation.error} />}
      {mode === 'agent' && submitting && <Spinner label="Submitting Agent task..." />}
      {mode === 'agent' && activeRun && <p className="text-sm text-amber-200">Current Agent run is {run.data?.status ?? 'loading'}. Wait for it to finish before starting another.</p>}
      <Button type="submit" disabled={mutation.isPending || submitting || (mode === 'agent' && activeRun) || !input.trim()}>{mode === 'agent' ? submitting ? 'Submitting...' : activeRun ? 'Agent run in progress' : 'Run Agent' : mutation.isPending ? 'Working…' : 'Analyze'}</Button>
    </form>
    {analysis && <div className="mt-6 space-y-4 border-t border-slate-700 pt-5"><div className="flex items-center gap-2"><h3 className="font-semibold">Answer</h3><Badge>{analysis.intent}</Badge></div><p className="whitespace-pre-wrap text-sm leading-7 text-slate-200">{analysis.answer}</p><div><h4 className="mb-2 text-sm font-semibold text-slate-300">Evidence</h4>{analysis.evidence.length ? <ul className="space-y-2 text-sm text-slate-300">{analysis.evidence.map((item, index) => <li key={`${index}-${item}`} className="rounded-lg bg-slate-800/80 p-3 break-words">{item}</li>)}</ul> : <EmptyState title="No evidence returned" />}</div></div>}
  </Card>
}
