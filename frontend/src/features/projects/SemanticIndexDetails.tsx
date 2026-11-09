import type { SemanticIndex } from '../../api/types'
import { Badge } from '../../components/ui'

const descriptions: Record<string, string> = {
  disabled: '未启用：语义检索已关闭，基础代码索引仍可独立使用。',
  not_built: '尚未构建语义向量索引，不影响基础代码索引状态。',
  queued: '语义索引任务已排队，等待构建。', building: '正在构建语义向量索引。',
  ready: '语义向量索引已就绪。', failed: '语义索引构建失败，基础代码索引状态独立。',
  unavailable: '语义索引产物暂不可用，基础代码索引状态独立。',
}
export function SemanticIndexDetails({value}: {value?: SemanticIndex | null}) {
  return <div className="mt-4 space-y-2 border-t border-slate-700 pt-3 text-sm">
    <h3 className="font-semibold">语义索引</h3>
    {!value ? <p>该快照未提供语义索引信息。</p> : <>
      <Badge>{value.status}</Badge><p>{descriptions[value.status] ?? '未知语义索引状态，请查看服务端信息。'}</p>
      <p>Provider：{value.provider ?? '—'} · Model：{value.model ?? '—'} · Dimension：{value.dimension ?? '—'}</p>
      <p className="break-all">Fingerprint：{value.fingerprint ? <code title={value.fingerprint}>{value.fingerprint}</code> : '—'}</p>
      <p>向量数量：{value.artifact?.vector_count ?? '—'} · Artifact 大小：{value.artifact?.byte_count ?? '—'} bytes</p>
      <p>构建时间：{value.built_at ? new Date(value.built_at).toLocaleString('zh-CN') : '—'}</p>
      {value.error_code && <p>错误代码：{value.error_code}</p>}{value.error_message && <p>{value.error_message}</p>}
      <details><summary>语义索引历史版本（{value.versions.length}）</summary>
        {value.versions.length === 0 && <p>暂无历史版本。</p>}
        {value.versions.map(version => <div key={version.fingerprint} className="my-2 break-all border-t border-slate-700 pt-2">
          <p>{version.provider} · {version.model} · Dimension {version.dimension}</p>
          <p>Fingerprint：{version.fingerprint}</p>
          <p>向量数量：{version.artifact.vector_count} · Artifact 大小：{version.artifact.byte_count} bytes</p>
          <p>构建时间：{new Date(version.built_at).toLocaleString('zh-CN')}</p>
        </div>)}
      </details>
    </>}
  </div>
}
