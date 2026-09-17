import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { listProjects } from '../api/projects'
import { Badge, Button, Card, EmptyState, ErrorState, SectionHeading, Spinner, StatusBadge } from '../components/ui'

export function ProjectsPage() {
  const [offset, setOffset] = useState(0)
  const {data, isLoading, error} = useQuery({queryKey: ['projects', offset], queryFn: () => listProjects(50, offset)})
  return <main>
    <SectionHeading title="Projects" subtitle="Repositories and their latest indexing state."
      action={<Link to="/projects/new"><Button>Create Project</Button></Link>} />
    {isLoading && <Spinner label="Loading projects…" />}
    {error && <ErrorState error={error} />}
    {data && data.projects.length === 0 && offset === 0 && <EmptyState title="No Projects" detail="Create a project to start exploring a repository." />}
    {data && data.projects.length === 0 && offset > 0 && <EmptyState title="No more projects" />}
    <div className="grid gap-4 md:grid-cols-2">
      {data?.projects.map(project => <Link key={project.id} to={`/projects/${project.id}`} className="block no-underline"><Card className="h-full transition hover:border-cyan-400/70 hover:bg-slate-900"><div className="flex items-start justify-between gap-3"><h2 className="text-lg font-semibold text-white">{project.name}</h2><StatusBadge status={project.status} /></div><p className="mt-2 break-all text-sm text-slate-400">{project.repo_url}</p><div className="mt-5 flex flex-wrap gap-2"><Badge>{project.branch ?? 'default branch'}</Badge><Badge>{project.current_snapshot_id ? `Snapshot ${project.current_snapshot_id.slice(0, 8)}` : 'Not indexed yet'}</Badge></div><p className="mt-5 text-xs text-slate-500">Updated {new Date(project.updated_at).toLocaleString()}</p></Card></Link>)}
    </div>
    {data && (offset > 0 || data.has_more) && <div className="mt-6 flex gap-3"><Button variant="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</Button><Button variant="secondary" disabled={!data.has_more} onClick={() => setOffset(offset + 50)}>Next</Button></div>}
  </main>
}
