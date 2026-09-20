import { Link, useParams } from 'react-router-dom'
import { AnalysisHistory } from '../features/history/AnalysisHistory'

export function AnalysisHistoryPage() {
  const {projectId = ''} = useParams()
  return <main className="space-y-6">
    <div>
      <Link to={`/projects/${projectId}`} className="text-sm text-cyan-300">← 返回项目</Link>
      <h1 className="mt-5 text-3xl font-bold tracking-tight text-white">分析历史</h1>
      <p className="mt-2 text-sm text-slate-400">查看此前针对当前仓库完成的分析和回答。</p>
    </div>
    <AnalysisHistory projectId={projectId} />
  </main>
}
