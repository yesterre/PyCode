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
  return <main className="mx-auto max-w-xl"><Link to="/" className="text-sm text-cyan-300">← Projects</Link><div className="mt-6"><SectionHeading title="Create Project" subtitle="Register a public HTTPS repository. You can build its index in the workspace." /></div>
    <Card><form onSubmit={submit} className="space-y-5">
      <Field label="Project name"><input className={inputClass} required value={name} onChange={e => setName(e.target.value)} placeholder="My service" /></Field>
      <Field label="Repository URL" hint="Public HTTPS URL accepted by the server."><input className={inputClass} required type="url" value={repo} onChange={e => setRepo(e.target.value)} placeholder="https://github.com/org/repo.git" /></Field>
      <Field label="Branch (optional)"><input className={inputClass} value={branch} onChange={e => setBranch(e.target.value)} placeholder="Use repository default" /></Field>
      {mutation.error && <ErrorState error={mutation.error} />}
      <Button type="submit" disabled={mutation.isPending}>{mutation.isPending ? 'Creating…' : 'Create Project'}</Button>
    </form></Card>
  </main>
}
