import { useQuery } from '@tanstack/react-query'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { getProject } from '../api/projects'
import { getSnapshot, listSnapshots } from '../api/snapshots'
import { useIndexTask } from '../features/projects/useIndexTask'
import { AgentPanel } from '../features/agent/AgentPanel'
import { TracePanel } from '../features/trace/TracePanel'
import { Button, Card, EmptyState, ErrorState, Metric, SectionHeading, Spinner, StatusBadge } from '../components/ui'

export function WorkspacePage() {
  const {projectId = ''} = useParams()
  const [params, setParams] = useSearchParams()
  const runId = params.get('runId')
  const project = useQuery({queryKey: ['project', projectId], queryFn: () => getProject(projectId),
    refetchInterval: query => ['preparing', 'indexing'].includes(query.state.data?.status ?? '') ? 2000 : false})
  const snapshotId = project.data?.current_snapshot_id
  const snapshot = useQuery({queryKey: ['snapshot', projectId, snapshotId],
    queryFn: () => getSnapshot(projectId, snapshotId!), enabled: !!snapshotId})
  const snapshots = useQuery({queryKey: ['snapshots', projectId], queryFn: () => listSnapshots(projectId)})
  const index = useIndexTask(projectId)
  if (project.isLoading) return <Spinner label="Loading workspace…" />
  if (project.error) return <ErrorState error={project.error} />
  if (!project.data) return <EmptyState title="Project not found" />
  const current = project.data
  const taskStatus = index.task.data?.status
  return <main className="space-y-6">
    <div><Link to="/" className="text-sm text-cyan-300">← Projects</Link><div className="mt-5 flex flex-wrap items-center gap-3"><h1 className="text-3xl font-bold tracking-tight text-white">{current.name}</h1><StatusBadge status={current.status} /></div><p className="mt-2 break-all text-sm text-slate-400">{current.repo_url}</p></div>
    <div className="grid gap-5 lg:grid-cols-3">
      <Card className="lg:col-span-2"><SectionHeading title="Repository overview" subtitle="The configured repository and current indexed revision." />
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"><Metric label="Branch" value={current.branch ?? 'Repository default'} /><Metric label="Commit" value={snapshot.data?.commit_sha.slice(0, 12) ?? '—'} /><Metric label="Current snapshot" value={current.current_snapshot_id?.slice(0, 8) ?? 'Not indexed yet'} /><Metric label="Files" value={snapshot.data?.file_count ?? '—'} /><Metric label="Nodes" value={snapshot.data?.node_count ?? '—'} /><Metric label="Edges" value={snapshot.data?.edge_count ?? '—'} /></div>
        {snapshot.isLoading && <div className="mt-4"><Spinner label="Loading snapshot…" /></div>}{snapshot.error && <div className="mt-4"><ErrorState error={snapshot.error} /></div>}
      </Card>
      <Card><SectionHeading title="Index" subtitle="Build or refresh this repository's code index." />
        <div className="mb-4"><StatusBadge status={taskStatus ?? current.status} /></div>
        {taskStatus === 'queued' && <p className="mb-4 text-sm text-slate-300">Index queued. Waiting for a worker.</p>}
        {taskStatus === 'running' && <div className="mb-4"><Spinner label="Indexing repository…" /></div>}
        {taskStatus === 'completed' && <p className="mb-4 text-sm text-emerald-300">Index ready.</p>}
        {taskStatus === 'failed' && <p className="mb-4 text-sm text-rose-300">{index.task.data?.error_code}: {index.task.data?.error_message}</p>}
        {!taskStatus && current.status === 'failed' && current.last_error && <p className="mb-4 text-sm text-rose-300">{current.last_error}</p>}
        {index.task.error && <div className="mb-4"><ErrorState error={index.task.error} /></div>}
        {index.start.error && <div className="mb-4"><ErrorState error={index.start.error} /></div>}
        <Button onClick={() => index.start.mutate()} disabled={index.start.isPending || taskStatus === 'queued' || taskStatus === 'running' || ['preparing', 'indexing'].includes(current.status)}>{index.start.isPending ? 'Submitting…' : current.current_snapshot_id ? 'Reindex' : 'Build Index'}</Button>
      </Card>
    </div>
    <div className="grid gap-5 lg:grid-cols-2">
      <Card><SectionHeading title="Current Snapshot" />{snapshot.data ? <div className="space-y-2 text-sm text-slate-300"><p><span className="text-slate-500">ID </span><code className="break-all">{snapshot.data.id}</code></p><p><span className="text-slate-500">Commit </span><code className="break-all">{snapshot.data.commit_sha}</code></p><p><span className="text-slate-500">Created </span>{new Date(snapshot.data.created_at).toLocaleString()}</p><StatusBadge status={snapshot.data.status} /></div> : <EmptyState title="Not indexed yet" detail="Build the index to create a current snapshot." />}</Card>
      <Card><SectionHeading title="Recent Snapshots" />{snapshots.isLoading && <Spinner />}{snapshots.error && <ErrorState error={snapshots.error} />}{snapshots.data?.snapshots.length === 0 && <EmptyState title="No Snapshot" />}{snapshots.data?.snapshots.map(item => <div key={item.id} className="flex items-center justify-between gap-3 border-b border-slate-800 py-2 text-sm last:border-0"><div className="min-w-0"><div className="truncate font-mono text-slate-200">{item.commit_sha.slice(0, 12)}</div><div className="text-xs text-slate-500">{new Date(item.created_at).toLocaleString()}</div></div><StatusBadge status={item.status} /></div>)}</Card>
    </div>
    <AgentPanel projectId={projectId} runId={runId} onRun={id => setParams({runId: id})} />
    {runId && <TracePanel projectId={projectId} runId={runId} />}
    <Card><SectionHeading title="Code Graph" subtitle="Explore a limited graph around a symbol or file." action={<Link to={`/projects/${projectId}/graph`}><Button variant="secondary">Open Graph →</Button></Link>} /><p className="text-sm text-slate-400">{current.index_summary ? `${current.index_summary.node_count} nodes · ${current.index_summary.edge_count} edges` : 'No Graph yet. Build the index first.'}</p></Card>
  </main>
}
