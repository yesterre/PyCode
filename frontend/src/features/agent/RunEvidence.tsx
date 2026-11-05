import type { AgentRun } from '../../api/types'

const qualityLabel = {
  evidence_available: '结构化证据可用', partial: '证据部分可用',
  insufficient: '证据不足', unavailable: '未提供结构化证据',
}

export function RunEvidence({run}: {run: AgentRun}) {
  const value = run.result_metadata
  const unsupported = !!value && (value.schema_version !== 1 || value.outcome !== run.status ||
    value.binding.project_id !== run.project_id || value.binding.snapshot_id !== run.snapshot_id)
  if (unsupported || run.metadata_warnings?.length) return <p role="status">此记录的结构化元数据暂不可用，原始文本结果仍保留。</p>
  if (!value) return null
  return <div className="mt-4 space-y-2 border-t border-slate-700 pt-3 text-sm text-slate-300">
    <p>{qualityLabel[value.quality]}。此状态不评价回答正确性。</p>
    <p className="break-all font-mono text-xs">Snapshot {value.binding.snapshot_id} · Commit {value.binding.commit_sha}</p>
    {value.references.length > 0 && <ul aria-label="持久化代码证据" className="space-y-2">
      {value.references.map((ref, index) => <li key={index} className="break-all font-mono text-xs">
        {ref.path}{ref.start_line != null && `:${ref.start_line}–${ref.end_line}`} {ref.symbol}
        {ref.chunk_ids_omitted > 0 && <span>（另有 {ref.chunk_ids_omitted} 个 Chunk 引用未展示）</span>}
        {ref.relation_resolution != null && <span>（静态关系，不保证运行时调用）</span>}
      </li>)}
    </ul>}
    {value.references_omitted > 0 && <p>另有 {value.references_omitted} 条证据引用未保存于此摘要。</p>}
    {value.diagnostics.includes('execution_failed') && <p>执行失败；已有代码引用不能替代最终答案。</p>}
  </div>
}
