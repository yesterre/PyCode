import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { listProjects } from '../api/projects'
import { Badge, Button, Card, EmptyState, ErrorState, SectionHeading, Spinner, StatusBadge } from '../components/ui'

export function ProjectsPage() {
  const [offset, setOffset] = useState(0)
  const {data, isLoading, error} = useQuery({queryKey: ['projects', offset], queryFn: () => listProjects(50, offset)})
  return <main>
    <SectionHeading title="项目" subtitle="查看仓库及其最新索引状态。"
      action={<Link to="/projects/new"><Button>创建项目</Button></Link>} />
    {isLoading && <Spinner label="正在加载项目…" />}
    {error && <ErrorState error={error} />}
    {data && data.projects.length === 0 && offset === 0 && <EmptyState title="暂无项目" detail="创建项目后即可开始分析仓库。" />}
    {data && data.projects.length === 0 && offset > 0 && <EmptyState title="没有更多项目" />}
    <div className="grid gap-4 md:grid-cols-2">
      {data?.projects.map(project => <Link key={project.id} to={`/projects/${project.id}`} className="block no-underline"><Card className="h-full transition hover:border-cyan-400/70 hover:bg-slate-900"><div className="flex items-start justify-between gap-3"><h2 className="text-lg font-semibold text-white">{project.name}</h2><StatusBadge status={project.status} /></div><p className="mt-2 break-all text-sm text-slate-400">{project.repo_url}</p><div className="mt-5 flex flex-wrap gap-2"><Badge>{project.branch ?? '默认分支'}</Badge><Badge>{project.current_snapshot_id ? `快照 ${project.current_snapshot_id.slice(0, 8)}` : '尚未索引'}</Badge></div><p className="mt-5 text-xs text-slate-500">更新时间：{new Date(project.updated_at).toLocaleString('zh-CN')}</p></Card></Link>)}
    </div>
    {data && (offset > 0 || data.has_more) && <div className="mt-6 flex gap-3"><Button variant="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button><Button variant="secondary" disabled={!data.has_more} onClick={() => setOffset(offset + 50)}>下一页</Button></div>}
  </main>
}
