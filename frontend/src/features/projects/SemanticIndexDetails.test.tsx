import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import type { SemanticIndex } from '../../api/types'
import { SemanticIndexDetails } from './SemanticIndexDetails'

const base: SemanticIndex = {status: 'ready', provider: 'provider', model: 'model', dimension: 128,
  fingerprint: 'fingerprint', artifact: {format_version: 1, dimension_source: 'validated_response', dtype: 'float32',
    byte_order: 'little', vector_count: 17, byte_count: 8704, sha256: 'hash'},
  built_at: '2026-11-09T00:00:00Z', error_code: null, error_message: null, versions: []}

it.each([
  ['disabled', '未启用：语义检索已关闭'], ['not_built', '尚未构建语义向量索引'], ['queued', '语义索引任务已排队'],
  ['building', '正在构建语义向量索引'], ['ready', '语义向量索引已就绪'], ['failed', '语义索引构建失败'],
  ['unavailable', '语义索引产物暂不可用'],
] as const)('explains %s independently of the base snapshot', (status, message) => {
  render(<SemanticIndexDetails value={{...base, status}} />)
  expect(screen.getByText(new RegExp(message))).toBeVisible()
})

it('reads artifact counts and version details from the actual nested schema', () => {
  render(<SemanticIndexDetails value={{...base, error_code: 'provider_failed', error_message: 'Provider unavailable',
    versions: [{provider: 'old-provider', model: 'old-model', dimension: 64, fingerprint: 'old',
      artifact: {...base.artifact!, vector_count: 9, byte_count: 2304}, built_at: '2026-11-08T00:00:00Z'}]}} />)
  expect(screen.getByText('向量数量：17 · Artifact 大小：8704 bytes')).toBeVisible()
  screen.getByText('语义索引历史版本（1）').click()
  expect(screen.getByText('向量数量：9 · Artifact 大小：2304 bytes')).toBeVisible()
  expect(screen.getByText('错误代码：provider_failed')).toBeVisible()
  expect(screen.getByText('Provider unavailable')).toBeVisible()
})
