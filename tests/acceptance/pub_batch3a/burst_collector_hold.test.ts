// pub-batch3a · 收集器「扣住」契约（行为清单 1-7）。
// 只依据 pub-batch3a-接口.md 的对外约定：createBurstCollector(deps)（可选 holdWhile、新增 hasWave）。
// 假时钟 + 临时目录，夹具全假数据（FAKECHAT-*）。窗口 8000ms。
import { afterAll, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createBurstCollector } from '../../../dispatcher/burst_inbox'

const ROOT = mkdtempSync(join(tmpdir(), 'pb3a-collector-'))
afterAll(() => { if (ROOT.startsWith(tmpdir())) rmSync(ROOT, { recursive: true, force: true }) })

let caseSeq = 0
function rig(opts: { windowMs?: number; maxMs?: number; holdWhile?: unknown } = {}) {
  const dir = join(ROOT, `case${++caseSeq}`)
  mkdirSync(dir, { recursive: true })
  let clock = 1_000_000
  let seq = 0
  const out: string[] = []
  const deps: Record<string, unknown> = {
    windowMs: opts.windowMs ?? 8000, maxMs: opts.maxMs ?? 0, destDir: join(dir, '.pending'),
    now: () => clock, deliver: (p: string) => out.push(p), log: () => {},
  }
  if (opts.holdWhile !== undefined) deps.holdWhile = opts.holdWhile
  const c: any = createBurstCollector(deps as any)
  const add = (o: Record<string, unknown> = {}): string => {
    const p = join(dir, `m${++seq}.json`)
    writeFileSync(p, JSON.stringify({
      chat_id: 'FAKECHAT-1', message_id: seq, from_id: 'U1', scene: 'private',
      chat_type: 'private', ts: `2026-08-25T0${seq}:00:00.000Z`, text: `T${seq}`, ...o,
    }))
    return p
  }
  return { c, out, add, at: (ms: number) => { clock = ms } }
}
const readOut = (p: string) => JSON.parse(readFileSync(p, 'utf8'))

test('1_扣住期间满窗也不投_释放后下一拍投一个合并件_内容依次是B和C', () => {
  let held = true
  const r = rig({ holdWhile: (id: string) => id === 'FAKECHAT-1' && held })
  r.at(1000); r.c.arrive(r.add({ text: 'B' }))
  r.at(10_000); r.c.tick()                       // B 后 9s，超过静默窗但被扣住
  expect(r.out).toEqual([])
  r.at(13_000); r.c.arrive(r.add({ text: 'C' })) // 扣住期间 C 在 B 后 12s 到
  r.at(22_000); r.c.tick()                       // C 后 9s
  expect(r.out).toEqual([])
  held = false; r.c.tick()
  expect(r.out.length).toBe(1)
  expect(readOut(r.out[0]!).text).toBe('B\nC')
})

test('2_只扣住被点名的聊天_另一个聊天满窗照投', () => {
  const r = rig({ holdWhile: (id: string) => id === 'FAKECHAT-1' })
  r.at(1000)
  r.c.arrive(r.add({ chat_id: 'FAKECHAT-1', text: 'X1' }))
  const y = r.add({ chat_id: 'FAKECHAT-2', from_id: 'U2', text: 'Y1' })
  r.c.arrive(y)
  r.at(10_000); r.c.tick()
  expect(r.out).toEqual([y])                     // 只有 Y 投出，X 仍在波里
})

test('3_扣住时maxMs封顶照常_20s封顶下9s不投_21s投', () => {
  const r = rig({ maxMs: 20_000, holdWhile: () => true })
  r.at(1000); r.c.arrive(r.add({ text: 'B' }))
  r.at(10_000); r.c.tick()
  expect(r.out).toEqual([])
  r.at(13_000); r.c.arrive(r.add({ text: 'C' }))
  r.at(20_000); r.c.tick()                       // 距首条 19s，未到封顶
  expect(r.out).toEqual([])
  r.at(22_000); r.c.tick()                       // 距首条 21s，封顶强投
  expect(r.out.length).toBe(1)
  expect(readOut(r.out[0]!).text).toBe('B\nC')
})

test('4_扣住时来带图消息_先冲波再单投_顺序为B然后C', () => {
  const r = rig({ holdWhile: () => true })
  r.at(1000); const b = r.add({ text: 'B' })
  r.c.arrive(b)
  r.at(2000); const c = r.add({ text: '', image_path: '/FAKE/img.png' })
  r.c.arrive(c)
  expect(r.out).toEqual([b, c])
})

for (const [label, hold] of [
  ['抛错', () => { throw new Error('boom') }],
  ["返回字符串'yes'（非严格true）", () => 'yes'],
] as const) {
  test(`5_holdWhile${label}_按不扣住处理_满窗照投且tick不抛`, () => {
    const r = rig({ holdWhile: hold })
    r.at(1000); const a = r.add({ text: 'A' })
    r.c.arrive(a)
    r.at(10_000)
    expect(() => r.c.tick()).not.toThrow()
    expect(r.out).toEqual([a])
  })
}

test('6_不给holdWhile_满窗照投（回归）', () => {
  const r = rig()
  r.at(1000); const a = r.add({ text: 'A' })
  r.c.arrive(a)
  r.at(8999); r.c.tick()
  expect(r.out).toEqual([])
  r.at(9000); r.c.tick()
  expect(r.out).toEqual([a])
})

test('7_hasWave_有波为true_无波或入参非字符串为false', () => {
  const r = rig()
  expect(typeof r.c.hasWave).toBe('function')
  expect(r.c.hasWave('FAKECHAT-1')).toBe(false)
  r.at(1000); r.c.arrive(r.add({ chat_id: 'FAKECHAT-1' }))
  expect(r.c.hasWave('FAKECHAT-1')).toBe(true)
  expect(r.c.hasWave('FAKECHAT-2')).toBe(false)
  for (const bad of [123, null, undefined, {}, ['FAKECHAT-1']]) expect(r.c.hasWave(bad as any)).toBe(false)
  r.at(10_000); r.c.tick()
  expect(r.c.hasWave('FAKECHAT-1')).toBe(false)  // 投出后波已清
})
