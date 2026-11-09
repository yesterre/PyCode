import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { archiveProject, listProjects, restoreProject, type ArchiveStatus } from '../api/projects'
import type { Project } from '../api/types'
import { Badge, Button, Card, EmptyState, ErrorState, SectionHeading, Spinner, StatusBadge } from '../components/ui'

export function ProjectsPage() {
  const [offset, setOffset] = useState(0)
  const [archiveStatus, setArchiveStatus] = useState<ArchiveStatus>('active')
  const client = useQueryClient()
  const lifecycleLock = useRef(false)
  const lifecycle = useMutation({mutationFn: (project: Project) => project.archived_at ? restoreProject(project.id) : archiveProject(project.id),
    onSuccess: async project => {
      client.setQueryData(['project', project.id], project)
      await Promise.all([client.invalidateQueries({queryKey: ['projects']}), client.invalidateQueries({queryKey: ['project', project.id]})])
    }, onSettled: () => {lifecycleLock.current = false}})
  const {data, isLoading, error} = useQuery({queryKey: ['projects', archiveStatus, offset], queryFn: () => listProjects(50, offset, archiveStatus)})
  return <main>
    <SectionHeading title="项目" subtitle="查看仓库及其最新索引状态。"
      action={<Link to="/projects/new"><Button>创建项目</Button></Link>} />
    <div className="mb-4 flex gap-3">{(['active', 'archived'] as const).map(status => <Button key={status} variant="secondary" disabled={lifecycle.isPending} aria-pressed={archiveStatus === status} onClick={() => {setArchiveStatus(status); setOffset(0); lifecycle.reset()}}>{status === 'active' ? '正常项目' : '已归档项目'}</Button>)}</div>
    {lifecycle.error && <ErrorState error={lifecycle.error} />}
    {lifecycle.isSuccess && <p role="status">项目状态已更新。</p>}
    {isLoading && <Spinner label="正在加载项目…" />}
    {error && <ErrorState error={error} />}
    {data && data.projects.length === 0 && offset === 0 && <EmptyState title="暂无项目" detail="创建项目后即可开始分析仓库。" />}
    {data && data.projects.length === 0 && offset > 0 && <EmptyState title="没有更多项目" />}
    <div className="grid gap-4 md:grid-cols-2">
      {data?.projects.map(project => <Card key={project.id} className="h-full transition hover:border-cyan-400/70 hover:bg-slate-900"><Link to={`/projects/${project.id}`} className="block no-underline"><div className="flex items-start justify-between gap-3"><h2 className="text-lg font-semibold text-white">{project.name}</h2><StatusBadge status={project.status} /></div><p className="mt-2 break-all text-sm text-slate-400">{project.repo_url}</p><div className="mt-5 flex flex-wrap gap-2"><Badge>{project.branch ?? '默认分支'}</Badge><Badge>{project.current_snapshot_id ? `快照 ${project.current_snapshot_id.slice(0, 8)}` : '尚未索引'}</Badge><Badge>{project.archived_at ? '已归档 · 只读' : '正常项目'}</Badge></div><p className="mt-5 text-xs text-slate-500">更新时间：{new Date(project.updated_at).toLocaleString('zh-CN')}</p>{project.archived_at && <p className="mt-2 text-xs text-slate-400">归档时间：{new Date(project.archived_at).toLocaleString('zh-CN')}</p>}</Link><div className="mt-4"><Button variant="secondary" disabled={lifecycle.isPending} onClick={() => {if (lifecycleLock.current) return; lifecycleLock.current = true; lifecycle.mutate(project)}}>{lifecycle.isPending && lifecycle.variables?.id === project.id ? '正在更新…' : project.archived_at ? '恢复项目' : '归档项目'}</Button></div></Card>)}
    </div>
    {data && (offset > 0 || data.has_more) && <div className="mt-6 flex gap-3"><Button variant="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button><Button variant="secondary" disabled={!data.has_more} onClick={() => setOffset(offset + 50)}>下一页</Button></div>}
  </main>
}
