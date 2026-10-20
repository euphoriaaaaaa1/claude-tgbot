// feature_gate.ts 单测：API base 白名单与 /send 文件丢弃判定。只用临时目录。
import { afterEach, describe, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'
import { planFileDrops, telegramApiBase } from './feature_gate'

const dirs: string[] = []
function mkDir(): string {
  const d = mkdtempSync(join(tmpdir(), 't19-gate-'))
  dirs.push(d)
  return d
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true })
})

describe('telegramApiBase', () => {
  test('未设置 → 放行且不指定', () => {
    expect(telegramApiBase(undefined)).toEqual({ ok: true, apiRoot: undefined })
  })
  test('只接受回环 http 与合法端口', () => {
    expect(telegramApiBase('http://127.0.0.1:17801')).toEqual({ ok: true, apiRoot: 'http://127.0.0.1:17801' })
    expect(telegramApiBase('http://127.0.0.1:65535').ok).toBe(true)
  })
  test('外部地址、https、路径、前导零、空串、越界端口一律拒绝', () => {
    for (const v of [
      '', 'http://127.0.0.1', 'http://127.0.0.1:0', 'http://127.0.0.1:65536',
      'http://127.0.0.1:017801', 'https://127.0.0.1:17801', 'http://localhost:17801',
      'http://10.0.0.1:17801', 'http://127.0.0.1:17801/',
    ]) expect(telegramApiBase(v).ok).toBe(false)
  })
})

describe('planFileDrops', () => {
  function fixture() {
    const root = mkDir()
    const image = join(root, 'images')
    const stickers = join(root, 'stickers')
    mkdirSync(image)
    mkdirSync(stickers)
    const a = join(image, 'a.png')
    const b = join(stickers, 'b.webp')
    const c = join(root, 'other.txt')
    for (const f of [a, b, c]) writeFileSync(f, 'x')
    return { root, image, stickers, files: [a, b, c] }
  }

  test('两项全开：不碰文件系统，全部放行', () => {
    const r = planFileDrops(['/no/such/file'], { image: '/no/such', stickers: '/no/such' }, { image: true, stickers: true })
    expect(r.dropped.size).toBe(0)
    expect(r.names).toEqual([])
  })

  test('生图关：只丢生图目录下的文件', () => {
    const f = fixture()
    const r = planFileDrops(f.files, { image: f.image, stickers: f.stickers }, { image: false, stickers: true })
    expect([...r.dropped]).toEqual([0])
    expect(r.names).toEqual(['a.png'])
    expect(r.counts).toEqual({ image: 1, stickers: 0 })
  })

  test('表情包关：只丢表情库目录下的文件', () => {
    const f = fixture()
    const r = planFileDrops(f.files, { image: f.image, stickers: f.stickers }, { image: true, stickers: false })
    expect([...r.dropped]).toEqual([1])
    expect(r.names).toEqual(['b.webp'])
  })

  test('两项都关：目录外的文件保留；非字符串项跳过', () => {
    const f = fixture()
    const r = planFileDrops([...f.files, null, 42, ''], { image: f.image, stickers: f.stickers }, { image: false, stickers: false })
    expect([...r.dropped]).toEqual([0, 1])
    expect(r.counts).toEqual({ image: 1, stickers: 1 })
  })

  test('同前缀兄弟目录不算目录内', () => {
    const f = fixture()
    const sibling = `${f.image}-sibling`
    mkdirSync(sibling)
    const x = join(sibling, 'x.png')
    writeFileSync(x, 'x')
    const r = planFileDrops([x], { image: f.image, stickers: f.stickers }, { image: false, stickers: true })
    expect(r.dropped.size).toBe(0)
  })
})
