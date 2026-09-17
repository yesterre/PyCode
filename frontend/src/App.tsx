import { Suspense, lazy } from 'react'
import { Link, Route, Routes } from 'react-router-dom'
import { PageShell } from './components/ui'
import { ProjectsPage } from './pages/ProjectsPage'
import { CreateProjectPage } from './pages/CreateProjectPage'
import { WorkspacePage } from './pages/WorkspacePage'
const GraphPage = lazy(() => import('./pages/GraphPage').then(module => ({default: module.GraphPage})))

export default function App() {
  return <PageShell>
    <header className="mb-8 flex items-center justify-between border-b border-slate-700/70 pb-5">
      <Link to="/" className="flex items-center gap-3 text-xl font-bold tracking-tight text-white no-underline"><span className="flex size-10 items-center justify-center rounded-xl bg-cyan-400 font-black text-slate-950">P</span>PyCode <span className="hidden text-sm font-medium text-slate-400 sm:inline">Workspace</span></Link>
      <Link to="/" className="text-sm text-slate-300 hover:text-cyan-300">Projects</Link>
    </header>
    <Routes>
      <Route path="/" element={<ProjectsPage />} />
      <Route path="/projects/new" element={<CreateProjectPage />} />
      <Route path="/projects/:projectId" element={<WorkspacePage />} />
      <Route path="/projects/:projectId/graph" element={<Suspense fallback={<div className="text-slate-400">Loading graph…</div>}><GraphPage /></Suspense>} />
      <Route path="*" element={<div className="py-20 text-center text-slate-300">Page not found. <Link className="text-cyan-300" to="/">View projects</Link></div>} />
    </Routes>
  </PageShell>
}
