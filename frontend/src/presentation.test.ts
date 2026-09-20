import { expect, it } from 'vitest'
import { statusLabel, traceEventLabel } from './presentation'

it('maps common statuses and trace events without changing unknown identifiers', () => {
  expect(statusLabel('created')).toBe('已创建')
  expect(statusLabel('queued')).toBe('排队中')
  expect(statusLabel('running')).toBe('运行中')
  expect(statusLabel('completed')).toBe('已完成')
  expect(statusLabel('failed')).toBe('失败')
  expect(statusLabel('ready')).toBe('就绪')
  expect(statusLabel('historical')).toBe('历史记录')
  expect(traceEventLabel('tool_started')).toBe('工具调用开始')
  expect(statusLabel('custom_status')).toBe('custom_status')
  expect(traceEventLabel('custom_event')).toBe('custom_event')
})
