// features.ts 单测：文件缺失/坏形状/类型错误/缓存失效/派生函数。只用临时目录。
import { afterEach, describe, expect, test } from 'bun:test'
import { mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { FEATURE_KEYS, FEATURE_LABELS, featureOn, offList, readFeatures } from './features'

const dirs: string[] = []
function mkDir(): string {
  const d = mkdtempSync(join(tmpdir(), 't19-features-'))
  dirs.push(d)
  return d
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

describe('readFeatures', () => {
  test('文件不存在：八项全开且 readable', () => {
    const r = readFeatures(mkDir())
    expect(r.readable).toBe(true)
    expect(Object.keys(r.features)).toEqual([...FEATURE_KEYS])
    expect(FEATURE_KEYS.every(k => r.features[k])).toBe(true)
  })

  test('default 与逐项覆盖', () => {
    const d = mkDir()
    writeFileSync(join(d, 'features.json'), JSON.stringify({
      default: false,
      features: { voice: true, groups: true },
    }))
    const r = readFeatures(d)
    expect(r.readable).toBe(true)
    expect(r.features.voice).toBe(true)
    expect(r.features.groups).toBe(true)
    expect(r.features.image).toBe(false)
    expect(r.warnings).toEqual([])
  })

  test('非布尔值按关并给警告', () => {
    const d = mkDir()
    writeFileSync(join(d, 'features.json'), JSON.stringify({
      default: true,
      features: { voice: 'yes', image: 1, stickers: null },
    }))
    const r = readFeatures(d)
    expect(r.features.voice).toBe(false)
    expect(r.features.image).toBe(false)
    expect(r.features.stickers).toBe(false)
    expect(r.features.moments).toBe(true)
    expect(r.warnings.length).toBe(3)
    expect(r.warnings[0]).toContain('发语音')
  })

  test('坏 JSON / 顶层非对象：八项全关且 not readable', () => {
    for (const text of ['{', '[]', 'null', '42']) {
      const d = mkDir()
      writeFileSync(join(d, 'features.json'), text)
      const r = readFeatures(d)
      expect(r.readable).toBe(false)
      expect(FEATURE_KEYS.every(k => r.features[k] === false)).toBe(true)
    }
  })

  test('文件改动后重新读取（缓存按 stat 失效）', () => {
    const d = mkDir()
    const p = join(d, 'features.json')
    writeFileSync(p, JSON.stringify({ features: { voice: false } }))
    expect(readFeatures(d).features.voice).toBe(false)
    writeFileSync(p, JSON.stringify({ features: { voice: true, image: false } }))
    expect(readFeatures(d).features.voice).toBe(true)
    expect(readFeatures(d).features.image).toBe(false)
  })

  test('返回副本：调用方修改不影响缓存', () => {
    const d = mkDir()
    writeFileSync(join(d, 'features.json'), JSON.stringify({ features: { voice: true } }))
    const a = readFeatures(d)
    a.features.voice = false
    expect(readFeatures(d).features.voice).toBe(true)
  })
})

describe('派生函数', () => {
  test('featureOn 只认 true', () => {
    const flags = { voice: true } as never
    expect(featureOn(flags, 'voice')).toBe(true)
    expect(featureOn(flags, 'image')).toBe(false)
  })

  test('offList 按固定顺序给中文键', () => {
    const flags = Object.fromEntries(FEATURE_KEYS.map(k => [k, true])) as never
    ;(flags as Record<string, boolean>).image = false
    ;(flags as Record<string, boolean>).life = false
    expect(offList(flags)).toEqual(['image', 'life'])
    expect(FEATURE_LABELS.image).toBe('生图')
  })
})
