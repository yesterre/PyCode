import type { PublicRetrievalSummary, TraceEvent } from '../../api/types'
import { Card, SectionHeading } from '../../components/ui'
import { evidenceCoverage, evidenceResolution, evidenceSource } from '../agent/EvidenceDetails'

export function RetrievalDetails({events}: {events: TraceEvent[]}) {
  const raw = [...events].reverse().find(event => event.payload.retrieval)?.payload.retrieval
  // Trace is normalized by the backend's public_retrieval_summary allowlist.
  const value = raw as PublicRetrievalSummary | undefined
  if (!value || !Array.isArray(value.references) || typeof value.evidence_count !== 'number') return null
  return <Card><SectionHeading title="检索证据" />
    <p>模式：{value.mode ?? '未提供'} · 状态：{value.status} · 耗时：{value.duration_ms} ms · Evidence：{value.evidence_count}</p>
    {value.graph_schema_version != null && <p>Graph Schema：{value.graph_schema_version}</p>}
    {value.diagnostics.length > 0 && <p>检索诊断：{value.diagnostics.join('、')}</p>}
    {value.pack && <div><p>Evidence Pack：选用 {value.pack.selected_count} 条，舍弃 {value.pack.discarded_count} 条。</p>
      <p>预算估算：{value.pack.estimated_tokens} / {value.pack.budget}，不是计费 Token。</p>
      {Object.entries(value.pack.reason_counts).map(([reason, count]) => <p key={reason}>舍弃原因 {reason}：{count}</p>)}</div>}
    <ul>{value.references.map((ref, index) => <li key={index} className="my-2 break-all text-sm">
      <p>{ref.path}{ref.start_line != null && `:${ref.start_line}–${ref.end_line}`} {ref.symbol}</p>
      <p>来源：{evidenceSource(ref.source)} · Coverage：{evidenceCoverage(ref.coverage)} · Chunk：{ref.chunk_ids.length} · 省略 Chunk：{ref.chunk_ids_omitted}</p>
      {ref.source_scores?.map(score => <p key={score.source}>Source Score {score.source}：{score.score} · Rank {score.rank} · Contribution {score.contribution}</p>)}
      {ref.graph_distance != null && <p>Graph Path：公开摘要提供 {ref.graph_distance} 跳距离；未提供逐边路径。静态线索不保证运行时调用。</p>}
      {ref.relation_resolution && <p>Relation Resolution：{evidenceResolution(ref.relation_resolution)}</p>}
    </li>)}</ul>
    <p>省略检索引用：{value.references_omitted}</p>
    {value.graph_stats && <details><summary>图谱检索统计</summary>{Object.entries(value.graph_stats).map(([key, item]) => <p key={key}>{key}：{String(item)}</p>)}</details>}
  </Card>
}
