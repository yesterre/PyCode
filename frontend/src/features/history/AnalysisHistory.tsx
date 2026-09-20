import { useState } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { analysisHistoryKey, getAnalysisHistory } from '../../api/projects'
import type { AnalysisHistoryItem, AnalysisHistoryMode } from '../../api/types'
import { Badge, Button, Card, EmptyState, Spinner } from '../../components/ui'

const PAGE_SIZE = 20
const PREVIEW_LENGTH = 480
const modeLabel: Record<AnalysisHistoryMode, string> = {
  repository: '仓库问答',
  impact: '文件影响分析',
  agent: '智能体分析',
}
const dateTime = new Intl.DateTimeFormat('zh-CN', {dateStyle: 'medium', timeStyle: 'short', hour12: false})

function HistoryEntry({item}: {item: AnalysisHistoryItem}) {
  const [expanded, setExpanded] = useState(false)
  const long = item.answer.length > PREVIEW_LENGTH
  const answer = long && !expanded ? `${item.answer.slice(0, PREVIEW_LENGTH).trimEnd()}…` : item.answer
  return <article className="border-b border-slate-800 py-6 first:pt-0 last:border-0 last:pb-0">
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
      <time dateTime={item.created_at} className="text-sm text-slate-400">{dateTime.format(new Date(item.created_at))}</time>
      <Badge>{modeLabel[item.mode]}</Badge>
    </div>
    <div className="space-y-4">
      <section aria-label="我的提问" className="border-l-4 border-cyan-400 bg-slate-950/60 px-4 py-3"><h3 className="mb-2 text-xs font-semibold tracking-wide text-cyan-300">我的提问</h3><p className="whitespace-pre-wrap break-words text-sm font-medium leading-6 text-slate-100">{item.question}</p></section>
      <section aria-label="PyCode 回答" className="rounded-lg border border-slate-700/70 bg-slate-800/50 p-4"><h3 className="mb-3 text-xs font-semibold tracking-wide text-slate-300">PyCode 回答</h3><p className="whitespace-pre-wrap break-words text-sm leading-7 text-slate-200">{answer}</p>
        {long && <Button variant="quiet" className="mt-2 px-0" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? '收起回答' : '展开回答'}</Button>}
      </section>
    </div>
  </article>
}

export function AnalysisHistory({projectId}: {projectId: string}) {
  const history = useInfiniteQuery({
    queryKey: analysisHistoryKey(projectId),
    queryFn: ({pageParam}) => getAnalysisHistory(projectId, PAGE_SIZE, pageParam),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => lastPage.has_more
      ? pages.reduce((count, page) => count + page.items.length, 0)
      : undefined,
  })
  const loaded = history.data?.pages.flatMap(page => page.items) ?? []
  const items = loaded.filter(
    (item, index) => loaded.findIndex(candidate => candidate.id === item.id) === index,
  )
  return <Card>
    {history.isLoading && <Spinner label="正在加载历史记录…" />}
    {history.isError && <div role="alert" className="rounded-lg border border-rose-400/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">历史记录加载失败</div>}
    {history.isSuccess && items.length === 0 && <EmptyState title="暂无分析历史" />}
    {items.length > 0 && <div>{items.map(item => <HistoryEntry key={item.id} item={item} />)}</div>}
    {history.hasNextPage && <div className="mt-5"><Button variant="secondary" disabled={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>{history.isFetchingNextPage ? '正在加载…' : '加载更多'}</Button></div>}
  </Card>
}
