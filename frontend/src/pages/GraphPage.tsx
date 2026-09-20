import { useEffect, useRef, useState, type FormEvent } from 'react'
import cytoscape from 'cytoscape'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { getProject } from '../api/projects'
import { queryGraph, type GraphSearch } from '../api/graph'
import type { GraphNode, GraphResult } from '../api/types'
import { Badge, Button, Card, EmptyState, ErrorState, Field, inputClass, SectionHeading, Spinner } from '../components/ui'

export function graphElements(graph: GraphResult): cytoscape.ElementDefinition[] {
  return [
    ...graph.nodes.map(node => ({data: {id: node.id, label: node.name, type: node.type, path: node.path}})),
    ...graph.edges.map((edge, index) => ({data: {id: `edge-${index}`, source: edge.source, target: edge.target, type: edge.type}})),
  ]
}

function GraphCanvas({result}: {result: GraphResult}) {
  const container = useRef<HTMLDivElement>(null)
  const [selected, setSelected] = useState<GraphNode | null>(null)
  useEffect(() => {
    if (!container.current || result.nodes.length === 0) return
    const graph = cytoscape({container: container.current, elements: graphElements(result),
      layout: {name: 'breadthfirst', animate: false, fit: true, padding: 30},
      style: [
        {selector: 'node', style: {'background-color': '#38bdf8', label: 'data(label)', color: '#e2e8f0',
          'text-outline-color': '#10151d', 'text-outline-width': 2, 'font-size': 11, width: 25, height: 25}},
        {selector: 'node[type = "file"]', style: {'background-color': '#34d399', shape: 'round-rectangle'}},
        {selector: 'node[type = "class"]', style: {'background-color': '#c084fc'}},
        {selector: 'node[type = "external"]', style: {'background-color': '#fbbf24'}},
        {selector: 'edge', style: {width: 1.5, 'line-color': '#64748b', 'target-arrow-color': '#64748b',
          'target-arrow-shape': 'triangle', 'curve-style': 'bezier'}},
        {selector: ':selected', style: {'border-width': 3, 'border-color': '#ffffff'}},
      ]})
    graph.on('tap', 'node', event => {
      const id: string = event.target.id()
      setSelected(result.nodes.find(node => node.id === id) ?? null)
    })
    return () => {graph.destroy()}
  }, [result])
  return <div className="grid gap-4 lg:grid-cols-[1fr_240px]"><div ref={container} aria-label="代码图谱" className="h-[520px] rounded-lg border border-slate-700 bg-slate-950" /><aside className="rounded-lg border border-slate-700 bg-slate-950/70 p-4 text-sm"><h3 className="mb-4 font-semibold">已选节点</h3>{selected ? <div className="space-y-3 break-words"><Badge>{selected.type}</Badge><p className="font-medium">{selected.name}</p><p className="text-slate-400">{selected.path ?? '外部节点 / 无仓库路径'}</p><p className="font-mono text-xs text-slate-500">{selected.id}</p></div> : <p className="text-slate-400">选择节点后可查看详情。</p>}</aside></div>
}

export function GraphPage() {
  const {projectId = ''} = useParams()
  const project = useQuery({queryKey: ['project', projectId], queryFn: () => getProject(projectId)})
  const [mode, setMode] = useState<'q'|'file_path'>('q')
  const [input, setInput] = useState('')
  const [nodeType, setNodeType] = useState('')
  const [search, setSearch] = useState<GraphSearch | null>(null)
  const snapshotId = project.data?.current_snapshot_id
  const graph = useQuery({queryKey: ['graph', projectId, snapshotId, search],
    queryFn: () => queryGraph(projectId, snapshotId!, search!), enabled: !!snapshotId && !!search})
  function submit(event: FormEvent) {
    event.preventDefault()
    const value = input.trim()
    if (!value) return
    setSearch({[mode]: value, ...(nodeType ? {node_type: nodeType} : {}), limit: 100})
  }
  return <main className="space-y-6"><Link to={`/projects/${projectId}`} className="text-sm text-cyan-300">← 返回项目</Link>
    <SectionHeading title="代码图谱" subtitle="搜索符号或文件，查看有限范围内的局部关系图。" />
    {project.isLoading && <Spinner label="正在加载项目…" />}{project.error && <ErrorState error={project.error} />}
    {project.data && !snapshotId && <EmptyState title="暂无代码图谱" detail="请先构建仓库索引并生成当前快照。" />}
    {snapshotId && <><Card><form onSubmit={submit} className="grid gap-4 sm:grid-cols-[160px_1fr_170px_auto] sm:items-end">
      <Field label="搜索方式"><select className={inputClass} value={mode} onChange={e => {setMode(e.target.value as 'q'|'file_path'); setSearch(null)}}><option value="q">符号或文件</option><option value="file_path">精确文件路径</option></select></Field>
      <Field label={mode === 'q' ? '搜索符号或文件' : '文件路径'}><input className={inputClass} value={input} required onChange={e => setInput(e.target.value)} placeholder={mode === 'q' ? 'main' : 'src/main.py'} /></Field>
      <Field label="节点类型"><select className={inputClass} value={nodeType} onChange={e => setNodeType(e.target.value)}><option value="">全部类型</option><option value="file">文件</option><option value="class">类</option><option value="function">函数</option><option value="method">方法</option></select></Field>
      <Button type="submit">搜索图谱</Button>
    </form></Card>
    {!search && <EmptyState title="搜索后查看图谱" detail="页面不会自动下载完整图谱。" />}
    {graph.isLoading && <Spinner label="正在加载局部图谱…" />}{graph.error && <ErrorState error={graph.error} />}
    {graph.data && <Card><SectionHeading title="局部图谱" subtitle={`${graph.data.nodes.length} 个节点 · ${graph.data.edges.length} 条边`} />{graph.data.truncated && <p className="mb-4 rounded-lg bg-amber-400/10 p-3 text-sm text-amber-200">当前仅展示有限范围，请缩小搜索条件以获得更精确的结果。</p>}{graph.data.nodes.length ? <GraphCanvas result={graph.data} /> : <EmptyState title="没有图谱结果" />}</Card>}
    </>}
  </main>
}
