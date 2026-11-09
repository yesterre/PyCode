import { useEffect, useRef, useState, type FormEvent } from 'react'
import cytoscape from 'cytoscape'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { getProject } from '../api/projects'
import { queryGraph, type GraphSearch } from '../api/graph'
import type { GraphEdge, GraphNode, GraphResult } from '../api/types'
import { useSnapshotSelection } from '../features/projects/useSnapshotSelection'
import { Badge, Button, Card, EmptyState, ErrorState, Field, inputClass, SectionHeading, Spinner } from '../components/ui'

export function graphElements(graph: GraphResult): cytoscape.ElementDefinition[] {
  return [
    ...graph.nodes.map(node => ({data: {id: node.id, label: node.name, type: node.type, path: node.path}})),
    ...graph.edges.map((edge, index) => ({data: {id: `edge-${index}`, source: edge.source, target: edge.target, type: edge.type,
      resolution: edge.resolution ?? 'legacy', basis: edge.basis ?? 'legacy_graph_unverified'}})),
  ]
}

export function GraphKnowledgeSummary({graph}: {graph?: GraphResult}) {
  return <><p className="text-sm text-slate-400">图中关系来自静态分析，不保证实际运行时调用。虚线调用表示目标尚未确认。</p>
    {graph && (graph.graph_schema_version === 1 || (graph.graph_schema_version == null && graph.nodes.length > 0)) && <p role="status">旧版 Graph：保持可读，尚未获得新版作用域和调用点解析能力；不会自动原地升级。</p>}
    {graph?.graph_schema_version === 2 && !graph.graph_capabilities?.includes('import_bindings') && <p>此快照保留作用域和调用点信息，尚无跨模块绑定能力；不会自动原地升级。</p>}
    {graph?.graph_capabilities?.includes('import_bindings') && <p>跨模块关系沿快照内的导入声明解析；导入声明和实际符号引用分别记录，仍不保证运行时调用。</p>}
    {graph?.graph_schema_version === 3 && !graph.graph_capabilities?.includes('local_receiver_bindings') && <p>此快照尚无局部实例绑定能力；不会自动原地升级。</p>}
    {graph?.graph_capabilities?.includes('local_receiver_bindings') && <p>实例和方法关系来自受控静态绑定；复杂工厂、继承和动态修改可能仍无法确认。</p>}
    {graph && <div className="space-y-2 text-sm">
      <p>Graph Schema：{graph.graph_schema_version ?? '未提供'} · 能力：{graph.graph_capabilities?.join('、') || '该快照未提供能力信息'}</p>
      <p>结果截断：{graph.truncated ? '是' : '否'} · 省略 Call Site：{graph.call_sites_omitted ?? 0}</p>
      {graph.graph_warnings?.map((warning, index) => <p key={index} className="text-amber-200">Graph 警告：{warning}</p>)}
    </div>}</>
}

export const resolutionLabel = (value?: string | null) => ({static: '受控静态解析', declaration: '声明关系',
  legacy: '旧版关系，未经验证', unknown: '未知，目标未确认', ambiguous: '存在歧义，目标未确认',
  unresolved: '尚未解析'}[value ?? 'legacy'] ?? `未确认的解析状态（${value}）`)

export function GraphEdgeDetails({edge}: {edge: GraphEdge}) {
  return <div className="space-y-2 break-all text-sm">
    <p>Source：{edge.source}</p><p>Target：{edge.target}</p><p>Relation Type：{edge.type}</p>
    <p>Resolution：{resolutionLabel(edge.resolution)}（{edge.resolution ?? 'legacy'}）</p>
    <p>Basis：{edge.basis ?? '该快照未提供此类信息'}</p><p>Site ID：{edge.site_id ?? '该快照未提供此类信息'}</p>
    <p>文件路径：{edge.path ?? '该快照未提供此类信息'}</p><p>起止行号：{edge.start_line ?? '—'}–{edge.end_line ?? '—'}</p>
  </div>
}

export function GraphCallSites({graph}: {graph: GraphResult}) {
  const missing = '该快照未提供此类信息'
  return <Card><SectionHeading title="Call Site 详情" />
    {!graph.call_sites ? <p>{missing}</p> : graph.call_sites.length === 0 ? <p>当前局部查询没有 Call Site。</p> :
      graph.call_sites.map(site => <details key={site.id} className="my-3 break-all border-t border-slate-700 pt-3 text-sm">
        <summary>{site.path}:{site.start_line}–{site.end_line} · {site.id}</summary>
        <p>Caller：{site.caller_id}</p><p>Target：{site.target_id ?? '目标未解析'}</p>
        <p>Resolution：{resolutionLabel(site.resolution)}（{site.resolution}）</p><p>Basis：{site.basis}</p>
        <p>Candidate Count：{site.candidate_count}</p>
        <p>Binding Declarations：{site.binding_declarations?.join('、') || (site.binding_declarations ? '无' : missing)}</p>
        <p>Binding Chain Omitted：{site.binding_chain_omitted ?? missing}</p>
        <p>Flow Evidence：{site.flow_evidence?.join('、') || (site.flow_evidence ? '无' : missing)}</p>
        <p>Flow Evidence Omitted：{site.flow_evidence_omitted ?? missing}</p>
      </details>)}
    <p>另有 {graph.call_sites_omitted ?? 0} 个 Call Site 未展示。</p>
  </Card>
}

function GraphCanvas({result}: {result: GraphResult}) {
  const container = useRef<HTMLDivElement>(null)
  const [selected, setSelected] = useState<GraphNode | null>(null)
  const [selectedEdge, setSelectedEdge] = useState<number | null>(null)
  useEffect(() => {
    setSelected(null); setSelectedEdge(null)
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
        {selector: 'edge[type = "unresolved_call"]', style: {'line-style': 'dashed', 'line-color': '#fbbf24'}},
        {selector: ':selected', style: {'border-width': 3, 'border-color': '#ffffff'}},
      ]})
    graph.on('tap', 'node', event => {
      const id: string = event.target.id()
      setSelected(result.nodes.find(node => node.id === id) ?? null)
      setSelectedEdge(null)
    })
    graph.on('tap', 'edge', event => {
      setSelectedEdge(Number(String(event.target.id()).replace('edge-', ''))); setSelected(null)
    })
    return () => {graph.destroy()}
  }, [result])
  return <div className="grid gap-4 lg:grid-cols-[1fr_240px]"><div ref={container} aria-label="代码图谱" className="h-[520px] rounded-lg border border-slate-700 bg-slate-950" /><aside className="rounded-lg border border-slate-700 bg-slate-950/70 p-4 text-sm"><h3 className="mb-4 font-semibold">已选节点 / 关系</h3>
    <Field label="选择关系"><select className={inputClass} value={selectedEdge ?? ''} onChange={event => {setSelected(null); setSelectedEdge(event.target.value === '' ? null : Number(event.target.value))}}><option value="">选择一条关系</option>{result.edges.map((edge, index) => <option key={index} value={index}>{edge.source} → {edge.target} · {edge.type}</option>)}</select></Field>
    {selectedEdge != null && result.edges[selectedEdge] ? <GraphEdgeDetails edge={result.edges[selectedEdge]} /> : selected ? <div className="space-y-3 break-words"><Badge>{selected.type}</Badge><p className="font-medium">{selected.name}</p><p className="text-slate-400">{selected.path ?? '外部节点 / 无仓库路径'}</p>{selected.start_line != null && <p>定义行 {selected.start_line}–{selected.end_line}</p>}<p className="font-mono text-xs text-slate-500">{selected.id}</p></div> : <p className="text-slate-400">选择节点或关系后可查看详情。</p>}</aside></div>
}

export function GraphPage() {
  const {projectId = ''} = useParams()
  const project = useQuery({queryKey: ['project', projectId], queryFn: () => getProject(projectId)})
  const [mode, setMode] = useState<'q'|'file_path'>('q')
  const [input, setInput] = useState('')
  const [nodeType, setNodeType] = useState('')
  const [submitted, setSubmitted] = useState<{projectId: string; snapshotId: string; search: GraphSearch} | null>(null)
  const {snapshotId, snapshot, snapshots, ready, selectSnapshot} = useSnapshotSelection(projectId, project.data?.current_snapshot_id)
  const search = submitted?.projectId === projectId && submitted.snapshotId === snapshotId ? submitted.search : null
  const graph = useQuery({queryKey: ['graph', projectId, snapshotId, search],
    queryFn: () => queryGraph(projectId, snapshotId!, search!), enabled: ready && !!search})
  function submit(event: FormEvent) {
    event.preventDefault()
    const value = input.trim()
    if (!value || !ready || !snapshotId) return
    setSubmitted({projectId, snapshotId, search: {[mode]: value, ...(nodeType ? {node_type: nodeType} : {}), limit: 100}})
  }
  return <main className="space-y-6"><Link to={`/projects/${projectId}`} className="text-sm text-cyan-300">← 返回项目</Link>
    <SectionHeading title="代码图谱" subtitle="搜索符号或文件，查看有限范围内的局部关系图。" />
    <GraphKnowledgeSummary graph={graph.data} />
    {project.isLoading && <Spinner label="正在加载项目…" />}{project.error && <ErrorState error={project.error} />}
    {snapshots.error && <ErrorState error={snapshots.error} />}{snapshot.error && <ErrorState error={snapshot.error} />}
    {snapshot.isLoading && <Spinner label="正在加载快照…" />}
    <Field label="Graph 快照"><select className={inputClass} value={snapshotId ?? ''} onChange={event => {selectSnapshot(event.target.value); setSubmitted(null)}}>
      {!snapshotId && <option value="">暂无快照</option>}
      {project.data?.current_snapshot_id && <option value={project.data.current_snapshot_id}>当前快照 {project.data.current_snapshot_id.slice(0, 8)}</option>}
      {snapshots.data?.snapshots.filter(item => item.project_id === projectId && item.id !== project.data?.current_snapshot_id).map(item => <option key={item.id} value={item.id} disabled={item.status !== 'ready'}>历史快照 {item.id.slice(0, 8)} · {item.commit_sha.slice(0, 12)} · {item.status}</option>)}
    </select></Field>
    {snapshotId && !ready && <p role="status">所选快照尚未就绪，暂不能查询图谱。</p>}
    {project.data && !snapshotId && <EmptyState title="暂无代码图谱" detail="请先构建仓库索引并生成当前快照。" />}
    {snapshotId && <><Card><form onSubmit={submit} className="grid gap-4 sm:grid-cols-[160px_1fr_170px_auto] sm:items-end">
      <Field label="搜索方式"><select className={inputClass} value={mode} onChange={e => {setMode(e.target.value as 'q'|'file_path'); setSubmitted(null)}}><option value="q">符号或文件</option><option value="file_path">精确文件路径</option></select></Field>
      <Field label={mode === 'q' ? '搜索符号或文件' : '文件路径'}><input className={inputClass} value={input} required onChange={e => setInput(e.target.value)} placeholder={mode === 'q' ? 'main' : 'src/main.py'} /></Field>
      <Field label="节点类型"><select className={inputClass} value={nodeType} onChange={e => setNodeType(e.target.value)}><option value="">全部类型</option><option value="file">文件</option><option value="class">类</option><option value="function">函数</option><option value="method">方法</option></select></Field>
      <Button type="submit" disabled={!ready || graph.isFetching}>搜索图谱</Button>
    </form></Card>
    {!search && <EmptyState title="搜索后查看图谱" detail="页面不会自动下载完整图谱。" />}
    {graph.isLoading && <Spinner label="正在加载局部图谱…" />}{graph.error && <ErrorState error={graph.error} />}
    {ready && graph.data && <><Card><SectionHeading title="局部图谱" subtitle={`${graph.data.nodes.length} 个节点 · ${graph.data.edges.length} 条边`} />{graph.data.truncated && <p className="mb-4 rounded-lg bg-amber-400/10 p-3 text-sm text-amber-200">当前仅展示有限范围，请缩小搜索条件以获得更精确的结果。</p>}{graph.data.nodes.length ? <GraphCanvas result={graph.data} /> : <EmptyState title="没有图谱结果" />}</Card><GraphCallSites graph={graph.data} /></>}
    </>}
  </main>
}
