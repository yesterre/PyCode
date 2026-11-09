import type { ContextReference, HarnessContext, RetrievalSource } from '../../api/types'

const sources: Record<RetrievalSource, string> = {exact_path: '精确路径', exact_symbol: '精确符号', keyword: '关键词',
  graph: '静态图谱', entry: '入口线索', vector: '向量检索', hybrid: '混合检索'}
const coverage: Record<string, string> = {complete_symbol: '完整符号', complete_file: '完整文件',
  partial: '部分行范围', unavailable: '覆盖不可用'}
const resolutions: Record<string, string> = {static: '受控静态关系', declaration: '声明关系',
  legacy: '旧版关系，未经验证', unknown: '未知，关系未确认', ambiguous: '存在歧义，关系未确认'}
export const evidenceSource = (value: string) => sources[value as RetrievalSource] ?? value
export const evidenceCoverage = (value: string) => coverage[value] ?? `未知覆盖（${value}）`
export const evidenceResolution = (value: string) => resolutions[value] ?? `未确认关系（${value}）`

function ContextReferenceDetails({value}: {value: Partial<ContextReference>}) {
  return <span className="break-all">{value.path ?? '无文件路径'} {value.symbol}
    {value.start_line != null && `:${value.start_line}–${value.end_line ?? '—'}`} · 来源：{value.source ?? '未提供'}
    {value.coverage && ` · Coverage：${evidenceCoverage(value.coverage)}`}
    {value.chunk_ids && ` · Chunk 引用：${value.chunk_ids.length}`}
    {!!value.chunk_ids_omitted && ` · 省略 Chunk：${value.chunk_ids_omitted}`}
  </span>
}

export function HarnessContextDetails({context}: {context: HarnessContext}) {
  return <details><summary>公开上下文引用与决策</summary>
    <p>仅展示公开证据元数据。</p><p>单次工具输出上限：{context.tool_output_chars ?? '—'} 字符</p>
    <ul aria-label="选中上下文引用">{context.references?.map((ref, index) => <li key={ref.id ?? index}><ContextReferenceDetails value={ref} /></li>)}</ul>
    <p>省略选中引用：{context.references_omitted ?? 0}</p>
    <ul aria-label="上下文决策">{context.decisions?.map((decision, index) => <li key={index}>原因：{decision.reason} · <ContextReferenceDetails value={decision.reference} /></li>)}</ul>
    <p>省略决策：{context.decisions_omitted ?? 0}</p>
  </details>
}
