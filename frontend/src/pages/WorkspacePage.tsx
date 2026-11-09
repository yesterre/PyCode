import { useQuery } from '@tanstack/react-query'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { getProject } from '../api/projects'
import { ApiError } from '../api/client'
import { useSnapshotSelection } from '../features/projects/useSnapshotSelection'
import { SemanticIndexDetails } from '../features/projects/SemanticIndexDetails'
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
  const {snapshotId, snapshot, snapshots, ready, selectSnapshot} = useSnapshotSelection(projectId, project.data?.current_snapshot_id)
  const index = useIndexTask(projectId)
  if (project.isLoading) return <Spinner label="正在加载工作台…" />
  if (project.error) return <ErrorState error={project.error} />
  if (!project.data) return <EmptyState title="未找到项目" />
  const current = project.data
  const readOnly = !!current.archived_at
  const snapshotBelongs = snapshot.data?.project_id === projectId
  const taskStatus = index.task.data?.status
  return <main className="space-y-6">
    <div><Link to="/" className="text-sm text-cyan-300">← 返回项目</Link><div className="mt-5 flex flex-wrap items-center gap-3"><h1 className="text-3xl font-bold tracking-tight text-white">{current.name}</h1><StatusBadge status={current.status} /></div><p className="mt-2 break-all text-sm text-slate-400">{current.repo_url}</p></div>
    {readOnly && <p role="status">项目已归档，只读：可以查看快照、图谱和历史记录；恢复项目后可新建索引和分析。</p>}
    <div className="grid gap-5 lg:grid-cols-3">
      <Card className="lg:col-span-2"><SectionHeading title="仓库概览" subtitle="当前配置的仓库与已索引版本。" />
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"><Metric label="分支" value={current.branch ?? '仓库默认分支'} /><Metric label="Commit" value={snapshotBelongs ? snapshot.data?.commit_sha.slice(0, 12) : '—'} /><Metric label="所选快照" value={snapshotId?.slice(0, 8) ?? '尚未索引'} /><Metric label="文件数" value={snapshotBelongs ? snapshot.data?.file_count ?? '—' : '—'} /><Metric label="节点数" value={snapshotBelongs ? snapshot.data?.node_count ?? '—' : '—'} /><Metric label="边数" value={snapshotBelongs ? snapshot.data?.edge_count ?? '—' : '—'} /></div>
        {snapshot.isLoading && <div className="mt-4"><Spinner label="正在加载快照…" /></div>}{snapshot.error && <div className="mt-4"><ErrorState error={snapshot.error} /></div>}
        {snapshot.data && !snapshotBelongs && <ErrorState error={new ApiError(null, 'snapshot_mismatch', '快照不属于当前项目，已禁用依赖此快照的操作。')} />}
      </Card>
      <Card><SectionHeading title="索引" subtitle="构建或刷新当前仓库的代码索引。" />
        <div className="mb-4"><StatusBadge status={taskStatus ?? current.status} /></div>
        {taskStatus === 'queued' && <p className="mb-4 text-sm text-slate-300">索引任务已排队，正在等待 Worker。</p>}
        {taskStatus === 'running' && <div className="mb-4"><Spinner label="正在索引仓库…" /></div>}
        {taskStatus === 'completed' && <p className="mb-4 text-sm text-emerald-300">索引已就绪。</p>}
        {taskStatus === 'failed' && <p className="mb-4 text-sm text-rose-300">{index.task.data?.error_code}: {index.task.data?.error_message}</p>}
        {!taskStatus && current.status === 'failed' && current.last_error && <p className="mb-4 text-sm text-rose-300">{current.last_error}</p>}
        {index.taskError != null && <div className="mb-4"><ErrorState error={index.taskError} /></div>}
        {index.invalidTaskError != null && <p className="mb-4 text-sm text-slate-300">已清除失效的索引任务记录，可以重新构建索引。</p>}
        {index.canRecheck && <div className="mb-4"><Button variant="secondary" disabled={index.task.isFetching} onClick={index.recheck}>{index.task.isFetching ? '正在重新检查…' : '重新检查索引任务'}</Button></div>}
        {index.start.error && <div className="mb-4"><ErrorState error={index.start.error} /></div>}
        <Button onClick={() => {if (!readOnly && !index.start.isPending) index.start.mutate()}} disabled={readOnly || index.start.isPending || taskStatus === 'queued' || taskStatus === 'running' || ['preparing', 'indexing'].includes(current.status)}>{index.start.isPending ? '正在提交…' : current.current_snapshot_id ? '重新构建索引' : '构建索引'}</Button>
      </Card>
    </div>
    <div className="grid gap-5 lg:grid-cols-2">
      <Card><SectionHeading title={snapshotId === current.current_snapshot_id ? '当前快照' : '历史快照'} />{snapshot.isLoading && <Spinner />}{snapshot.error && <ErrorState error={snapshot.error} />}{snapshot.data && snapshotBelongs ? <div className="space-y-2 text-sm text-slate-300"><p><span className="text-slate-500">ID </span><code className="break-all">{snapshot.data.id}</code></p><p><span className="text-slate-500">Commit </span><code className="break-all">{snapshot.data.commit_sha}</code></p><p><span className="text-slate-500">创建时间 </span>{new Date(snapshot.data.created_at).toLocaleString('zh-CN')}</p><p>基础代码索引：<StatusBadge status={snapshot.data.status} /></p>{snapshot.data.error_message && <p>{snapshot.data.error_message}</p>}<SemanticIndexDetails value={snapshot.data.semantic_index} /></div> : !snapshotId ? <EmptyState title="尚未索引" detail="构建索引后将生成当前快照。" /> : null}</Card>
      <Card><SectionHeading title="最近快照" />{snapshots.isLoading && <Spinner />}{snapshots.error && <ErrorState error={snapshots.error} />}{snapshots.data?.snapshots.length === 0 && <EmptyState title="暂无快照" />}{snapshots.data?.snapshots.filter(item => item.project_id === projectId).map(item => <div key={item.id} className="flex items-center justify-between gap-3 border-b border-slate-800 py-2 text-sm last:border-0"><div className="min-w-0"><div className="truncate font-mono text-slate-200">{item.commit_sha.slice(0, 12)}</div><div className="text-xs text-slate-500">{new Date(item.created_at).toLocaleString('zh-CN')}</div><span>{item.id === current.current_snapshot_id ? '当前快照' : '历史快照'}</span></div><StatusBadge status={item.status} /><Button variant="quiet" aria-pressed={item.id === snapshotId} onClick={() => selectSnapshot(item.id)}>查看快照 {item.id.slice(0, 8)}</Button></div>)}</Card>
    </div>
    <AgentPanel key={projectId} projectId={projectId} snapshotId={snapshotId} disabledReason={readOnly ? '项目已归档，恢复后可新建分析。' : !ready ? '选中快照尚未就绪，暂不能提交分析。' : undefined} runId={runId}
      onRun={id => setParams(previous => {const next = new URLSearchParams(previous); next.set('runId', id); return next})}
      onClearRun={() => setParams(previous => {const next = new URLSearchParams(previous); next.delete('runId'); return next})} />
    {runId && <TracePanel projectId={projectId} runId={runId} />}
    <Card><SectionHeading title="分析历史" subtitle="查看此前对当前仓库进行的仓库问答、文件影响分析和智能体分析。" action={<Link to={`/projects/${projectId}/history`}><Button variant="secondary">查看历史记录</Button></Link>} /></Card>
    <Card><SectionHeading title="代码图谱" subtitle="围绕符号或文件查看有限范围内的代码关系。" action={<Link to={`/projects/${projectId}/graph`}><Button variant="secondary">打开代码图谱 →</Button></Link>} /><p className="text-sm text-slate-400">{current.index_summary ? `${current.index_summary.node_count} 个节点 · ${current.index_summary.edge_count} 条边` : '暂无代码图谱，请先构建索引。'}</p></Card>
  </main>
}
