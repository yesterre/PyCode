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
  if (project.isLoading) return <Spinner label="正在加载工作台…" />
  if (project.error) return <ErrorState error={project.error} />
  if (!project.data) return <EmptyState title="未找到项目" />
  const current = project.data
  const taskStatus = index.task.data?.status
  return <main className="space-y-6">
    <div><Link to="/" className="text-sm text-cyan-300">← 返回项目</Link><div className="mt-5 flex flex-wrap items-center gap-3"><h1 className="text-3xl font-bold tracking-tight text-white">{current.name}</h1><StatusBadge status={current.status} /></div><p className="mt-2 break-all text-sm text-slate-400">{current.repo_url}</p></div>
    <div className="grid gap-5 lg:grid-cols-3">
      <Card className="lg:col-span-2"><SectionHeading title="仓库概览" subtitle="当前配置的仓库与已索引版本。" />
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"><Metric label="分支" value={current.branch ?? '仓库默认分支'} /><Metric label="Commit" value={snapshot.data?.commit_sha.slice(0, 12) ?? '—'} /><Metric label="当前快照" value={current.current_snapshot_id?.slice(0, 8) ?? '尚未索引'} /><Metric label="文件数" value={snapshot.data?.file_count ?? '—'} /><Metric label="节点数" value={snapshot.data?.node_count ?? '—'} /><Metric label="边数" value={snapshot.data?.edge_count ?? '—'} /></div>
        {snapshot.isLoading && <div className="mt-4"><Spinner label="正在加载快照…" /></div>}{snapshot.error && <div className="mt-4"><ErrorState error={snapshot.error} /></div>}
      </Card>
      <Card><SectionHeading title="索引" subtitle="构建或刷新当前仓库的代码索引。" />
        <div className="mb-4"><StatusBadge status={taskStatus ?? current.status} /></div>
        {taskStatus === 'queued' && <p className="mb-4 text-sm text-slate-300">索引任务已排队，正在等待 Worker。</p>}
        {taskStatus === 'running' && <div className="mb-4"><Spinner label="正在索引仓库…" /></div>}
        {taskStatus === 'completed' && <p className="mb-4 text-sm text-emerald-300">索引已就绪。</p>}
        {taskStatus === 'failed' && <p className="mb-4 text-sm text-rose-300">{index.task.data?.error_code}: {index.task.data?.error_message}</p>}
        {!taskStatus && current.status === 'failed' && current.last_error && <p className="mb-4 text-sm text-rose-300">{current.last_error}</p>}
        {index.task.error && <div className="mb-4"><ErrorState error={index.task.error} /></div>}
        {index.start.error && <div className="mb-4"><ErrorState error={index.start.error} /></div>}
        <Button onClick={() => index.start.mutate()} disabled={index.start.isPending || taskStatus === 'queued' || taskStatus === 'running' || ['preparing', 'indexing'].includes(current.status)}>{index.start.isPending ? '正在提交…' : current.current_snapshot_id ? '重新构建索引' : '构建索引'}</Button>
      </Card>
    </div>
    <div className="grid gap-5 lg:grid-cols-2">
      <Card><SectionHeading title="当前快照" />{snapshot.data ? <div className="space-y-2 text-sm text-slate-300"><p><span className="text-slate-500">ID </span><code className="break-all">{snapshot.data.id}</code></p><p><span className="text-slate-500">Commit </span><code className="break-all">{snapshot.data.commit_sha}</code></p><p><span className="text-slate-500">创建时间 </span>{new Date(snapshot.data.created_at).toLocaleString('zh-CN')}</p><StatusBadge status={snapshot.data.status} /></div> : <EmptyState title="尚未索引" detail="构建索引后将生成当前快照。" />}</Card>
      <Card><SectionHeading title="最近快照" />{snapshots.isLoading && <Spinner />}{snapshots.error && <ErrorState error={snapshots.error} />}{snapshots.data?.snapshots.length === 0 && <EmptyState title="暂无快照" />}{snapshots.data?.snapshots.map(item => <div key={item.id} className="flex items-center justify-between gap-3 border-b border-slate-800 py-2 text-sm last:border-0"><div className="min-w-0"><div className="truncate font-mono text-slate-200">{item.commit_sha.slice(0, 12)}</div><div className="text-xs text-slate-500">{new Date(item.created_at).toLocaleString('zh-CN')}</div></div><StatusBadge status={item.status} /></div>)}</Card>
    </div>
    <AgentPanel projectId={projectId} runId={runId} onRun={id => setParams({runId: id})} />
    {runId && <TracePanel projectId={projectId} runId={runId} />}
    <Card><SectionHeading title="分析历史" subtitle="查看此前对当前仓库进行的仓库问答、文件影响分析和智能体分析。" action={<Link to={`/projects/${projectId}/history`}><Button variant="secondary">查看历史记录</Button></Link>} /></Card>
    <Card><SectionHeading title="代码图谱" subtitle="围绕符号或文件查看有限范围内的代码关系。" action={<Link to={`/projects/${projectId}/graph`}><Button variant="secondary">打开代码图谱 →</Button></Link>} /><p className="text-sm text-slate-400">{current.index_summary ? `${current.index_summary.node_count} 个节点 · ${current.index_summary.edge_count} 条边` : '暂无代码图谱，请先构建索引。'}</p></Card>
  </main>
}
