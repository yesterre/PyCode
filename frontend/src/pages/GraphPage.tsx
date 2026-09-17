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
  return <div className="grid gap-4 lg:grid-cols-[1fr_240px]"><div ref={container} aria-label="Code graph" className="h-[520px] rounded-lg border border-slate-700 bg-slate-950" /><aside className="rounded-lg border border-slate-700 bg-slate-950/70 p-4 text-sm"><h3 className="mb-4 font-semibold">Selected node</h3>{selected ? <div className="space-y-3 break-words"><Badge>{selected.type}</Badge><p className="font-medium">{selected.name}</p><p className="text-slate-400">{selected.path ?? 'External / No repository path'}</p><p className="font-mono text-xs text-slate-500">{selected.id}</p></div> : <p className="text-slate-400">Select a node to inspect it.</p>}</aside></div>
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
  return <main className="space-y-6"><Link to={`/projects/${projectId}`} className="text-sm text-cyan-300">← Workspace</Link>
    <SectionHeading title="Code Graph" subtitle="Search a symbol or file, then inspect a bounded local graph." />
    {project.isLoading && <Spinner label="Loading project…" />}{project.error && <ErrorState error={project.error} />}
    {project.data && !snapshotId && <EmptyState title="No Graph" detail="Build the repository index to create a current snapshot." />}
    {snapshotId && <><Card><form onSubmit={submit} className="grid gap-4 sm:grid-cols-[160px_1fr_170px_auto] sm:items-end">
      <Field label="Search by"><select className={inputClass} value={mode} onChange={e => {setMode(e.target.value as 'q'|'file_path'); setSearch(null)}}><option value="q">Symbol or file</option><option value="file_path">Exact file path</option></select></Field>
      <Field label={mode === 'q' ? 'Search symbol or file' : 'File Path'}><input className={inputClass} value={input} required onChange={e => setInput(e.target.value)} placeholder={mode === 'q' ? 'main' : 'src/main.py'} /></Field>
      <Field label="Node type"><select className={inputClass} value={nodeType} onChange={e => setNodeType(e.target.value)}><option value="">All types</option><option value="file">File</option><option value="class">Class</option><option value="function">Function</option><option value="method">Method</option></select></Field>
      <Button type="submit">Search Graph</Button>
    </form></Card>
    {!search && <EmptyState title="Search to explore" detail="The full graph is not downloaded automatically." />}
    {graph.isLoading && <Spinner label="Loading local graph…" />}{graph.error && <ErrorState error={graph.error} />}
    {graph.data && <Card><SectionHeading title="Local graph" subtitle={`${graph.data.nodes.length} nodes · ${graph.data.edges.length} edges`} />{graph.data.truncated && <p className="mb-4 rounded-lg bg-amber-400/10 p-3 text-sm text-amber-200">Showing a limited local graph. Narrow the search for more specific results.</p>}{graph.data.nodes.length ? <GraphCanvas result={graph.data} /> : <EmptyState title="No graph results" />}</Card>}
    </>}
  </main>
}
