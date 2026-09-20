import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { createProject } from '../api/projects'
import { Button, Card, ErrorState, Field, inputClass, SectionHeading } from '../components/ui'

export function CreateProjectPage() {
  const [name, setName] = useState('')
  const [repo, setRepo] = useState('')
  const [branch, setBranch] = useState('')
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const mutation = useMutation({mutationFn: () => createProject({name, repo_url: repo, branch: branch || null}),
    onSuccess: project => { void queryClient.invalidateQueries({queryKey: ['projects']}); navigate(`/projects/${project.id}`) }})
  const submit = (event: FormEvent) => {event.preventDefault(); mutation.mutate()}
  return <main className="mx-auto max-w-xl"><Link to="/" className="text-sm text-cyan-300">← 返回项目</Link><div className="mt-6"><SectionHeading title="创建项目" subtitle="登记一个公开 HTTPS 仓库，随后可在工作台中构建索引。" /></div>
    <Card><form onSubmit={submit} className="space-y-5">
      <Field label="项目名称"><input className={inputClass} required value={name} onChange={e => setName(e.target.value)} placeholder="我的服务" /></Field>
      <Field label="仓库地址" hint="请输入服务器支持的公开 HTTPS 地址。"><input className={inputClass} required type="url" value={repo} onChange={e => setRepo(e.target.value)} placeholder="https://github.com/org/repo.git" /></Field>
      <Field label="分支（可选）"><input className={inputClass} value={branch} onChange={e => setBranch(e.target.value)} placeholder="使用仓库默认分支" /></Field>
      {mutation.error && <ErrorState error={mutation.error} />}
      <Button type="submit" disabled={mutation.isPending}>{mutation.isPending ? '正在创建…' : '创建项目'}</Button>
    </form></Card>
  </main>
}
