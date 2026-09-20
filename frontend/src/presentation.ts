export const STATUS_LABELS: Record<string, string> = {
  created: '已创建',
  preparing: '准备中',
  indexing: '索引中',
  queued: '排队中',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  ready: '就绪',
  historical: '历史记录',
  live: '实时',
  connecting: '正在连接',
  reconnecting: '正在重连',
  unavailable: '暂时不可用',
  gap: '数据不完整',
  idle: '未连接',
  success: '成功',
}

export const TRACE_EVENT_LABELS: Record<string, string> = {
  run_started: '运行开始',
  plan_created: '已生成计划',
  context_updated: '上下文已更新',
  llm_started: '模型调用开始',
  llm_finished: '模型调用结束',
  tool_started: '工具调用开始',
  tool_finished: '工具调用结束',
  run_completed: '运行完成',
  run_failed: '运行失败',
}

export const statusLabel = (status: string) => STATUS_LABELS[status] ?? status
export const traceEventLabel = (eventType: string) => TRACE_EVENT_LABELS[eventType] ?? eventType
