import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { analysisHistoryKey } from '../../api/projects'
import { getAgentRun } from '../../api/agentRuns'
import { submitQuestion } from '../../api/questions'
import { Button, Card, ErrorState, Field, inputClass, SectionHeading, Spinner } from '../../components/ui'
import { statusLabel } from '../../presentation'

export function AgentPanel({projectId, runId = null, onRun}: {projectId: string; runId?: string | null; onRun: (runId: string) => void}) {
  const client = useQueryClient()
  const [input, setInput] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const lock = useRef(false)
  const submittedRun = useRef<string | null>(null)
  const attempt = useRef<{question: string; key: string} | null>(null)
  const run = useQuery({queryKey: ['run', runId], queryFn: () => getAgentRun(runId!), enabled: !!runId})
  const active = !!runId && run.data?.status !== 'completed' && run.data?.status !== 'failed'
  useEffect(() => {
    if (runId && submittedRun.current === runId) {
      lock.current = false; submittedRun.current = null; setSubmitting(false)
    }
  }, [runId])
  useEffect(() => {
    if (run.data?.status === 'completed' && run.data.result != null)
      void client.invalidateQueries({queryKey: analysisHistoryKey(projectId)})
  }, [projectId, client, run.data?.result, run.data?.status])
  const mutation = useMutation({mutationFn: ({question, key}: {question: string; key: string}) => submitQuestion(projectId, question, key),
    onSuccess: result => {attempt.current = null; submittedRun.current = result.run_id; onRun(result.run_id)},
    onError: () => {lock.current = false; setSubmitting(false)},
  })
  function submit(event: FormEvent) {
    event.preventDefault()
    const question = input.trim()
    if (!question || lock.current || active || mutation.isPending) return
    lock.current = true; setSubmitting(true)
    if (attempt.current?.question !== question) attempt.current = {question, key: crypto.randomUUID()}
    mutation.mutate(attempt.current)
  }
  return <Card><SectionHeading title="分析仓库" subtitle="描述问题，系统会选择适合的分析方式。" />
    <form onSubmit={submit} className="space-y-4">
      <Field label="提问"><textarea className={`${inputClass} min-h-24 resize-y`} maxLength={8000} required value={input} onChange={e => setInput(e.target.value)} placeholder="这个仓库的入口在哪里？修改 src/main.py 会影响哪里？" /></Field>
      {mutation.error && <ErrorState error={mutation.error} />}
      {submitting && <Spinner label="正在提交分析…" />}
      {active && <p className="text-sm text-amber-200">当前分析状态：{statusLabel(run.data?.status ?? 'running')}。请等待本次分析结束后再提交。</p>}
      <Button type="submit" disabled={submitting || active || !input.trim()}>{submitting ? '正在提交…' : active ? '分析进行中' : '开始分析'}</Button>
    </form>
  </Card>
}
