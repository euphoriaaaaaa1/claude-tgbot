// provider_fp.ts 单测：分档合并、指纹稳定性、不可判定态。读文件全部走注入，不碰真实 settings。
import { describe, expect, test } from 'bun:test'
import { computeProviderFp, mergeProviderEnv, readProviderFp, readProviderFpStatus } from './provider_fp'

const paths = {
  globalSharedPath: '/g/settings.json',
  globalLocalPath: '/g/settings.local.json',
  projectSharedPath: '/p/settings.json',
  projectLocalPath: '/p/settings.local.json',
}

describe('mergeProviderEnv', () => {
  test('后档覆盖前档，只取两个键', () => {
    const env = mergeProviderEnv(
      { ANTHROPIC_BASE_URL: 'a', ANTHROPIC_MODEL: 'm1', OTHER: 'x' },
      { ANTHROPIC_MODEL: 'm2' },
      { ANTHROPIC_BASE_URL: 'b' },
      { ANTHROPIC_MODEL: 'm3' },
    )
    expect(env).toEqual({ baseUrl: 'b', model: 'm3' })
  })

  test('非字符串值视作未提供，不污染指纹', () => {
    expect(mergeProviderEnv({ ANTHROPIC_BASE_URL: 'a' }, { ANTHROPIC_BASE_URL: 1 as never }, null, undefined))
      .toEqual({ baseUrl: 'a', model: '' })
    expect(mergeProviderEnv(null, null, null, null)).toEqual({ baseUrl: '', model: '' })
  })
})

describe('computeProviderFp', () => {
  test('16 位 hex 且稳定；空值也是确定指纹', () => {
    const a = computeProviderFp({ baseUrl: 'https://x', model: 'm' })
    expect(a).toMatch(/^[0-9a-f]{16}$/)
    expect(a).toBe(computeProviderFp({ baseUrl: 'https://x', model: 'm' }))
    expect(computeProviderFp({ baseUrl: '', model: '' })).toMatch(/^[0-9a-f]{16}$/)
    expect(computeProviderFp({ baseUrl: '', model: '' })).not.toBe(a)
  })
})

describe('readProviderFpStatus', () => {
  test('四份都不存在 → 确定的全空指纹', () => {
    const r = readProviderFpStatus({ ...paths, readFile: () => { throw Object.assign(new Error('x'), { code: 'ENOENT' }) } })
    expect(r.uncertain).toBe(false)
    expect(r.fp).toBe(computeProviderFp({ baseUrl: '', model: '' }))
  })

  test('项目级覆盖全局级', () => {
    const files: Record<string, string> = {
      [paths.globalSharedPath]: JSON.stringify({ env: { ANTHROPIC_BASE_URL: 'g', ANTHROPIC_MODEL: 'gm' } }),
      [paths.projectSharedPath]: JSON.stringify({ env: { ANTHROPIC_MODEL: 'pm' } }),
    }
    const r = readProviderFpStatus({ ...paths, readFile: p => {
      if (!(p in files)) throw Object.assign(new Error('x'), { code: 'ENOENT' })
      return files[p]!
    } })
    expect(r).toEqual({ fp: computeProviderFp({ baseUrl: 'g', model: 'pm' }), uncertain: false })
  })

  test('坏 JSON / 读失败 → null 且 uncertain', () => {
    for (const readFile of [
      () => '{',
      () => { throw Object.assign(new Error('x'), { code: 'EACCES' }) },
    ]) {
      const r = readProviderFpStatus({ ...paths, readFile })
      expect(r).toEqual({ fp: null, uncertain: true })
      expect(readProviderFp({ ...paths, readFile })).toBeNull()
    }
  })

  test('顶层非对象 / env 非对象按空档继续', () => {
    const r = readProviderFpStatus({ ...paths, readFile: () => '[1,2]' })
    expect(r).toEqual({ fp: computeProviderFp({ baseUrl: '', model: '' }), uncertain: false })
    const r2 = readProviderFpStatus({ ...paths, readFile: () => JSON.stringify({ env: [] }) })
    expect(r2.uncertain).toBe(false)
  })
})
