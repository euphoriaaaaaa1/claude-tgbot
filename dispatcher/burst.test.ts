// 黑盒验收：decideBurst 状态机（唯一依据 .devflow/INTERFACE-burst.md）。实现未落地，现红为预期。
import { test, expect } from 'bun:test'
import { decideBurst } from './burst_plan'
import type { InMsg } from './burst_plan'

const W = 5000, M = 12000
const m = (path: string, ts: number, hasMedia = false): InMsg =>
  ({ path, ts, hasMedia, text: 'FAKE', chatId: 'FAKECHAT-1' })
const arrive = (msg: InMsg) => ({ kind: 'arrive' as const, msg })
const timer = (nowMs: number) => ({ kind: 'timer' as const, nowMs })
const call = (wave: InMsg[], ev: any, w = W, mx = M) => decideBurst(wave, ev, w, mx)

test('开波：无波时文本到达 → hold，波内只有它', () => {
  const r = call([], arrive(m('a.json', 1000)))
  expect(r.action).toEqual({ kind: 'hold' })
  expect(r.wave.map(x => x.path)).toEqual(['a.json'])
})

test('续窗：波内再来文本 → hold，按到达序追加', () => {
  const r = call([m('a.json', 1000)], arrive(m('b.json', 2000)))
  expect(r.action.kind).toBe('hold')
  expect(r.wave.map(x => x.path)).toEqual(['a.json', 'b.json'])
})

test('hold 动作不带 paths 字段（反向）', () => {
  expect('paths' in call([], arrive(m('a.json', 1000))).action).toBe(false)
})

test('窗口到：timer 距末条 ts 超过 windowMs → flush 全波，新波清空', () => {
  const r = call([m('a.json', 1000), m('b.json', 2000)], timer(8000))
  expect(r.action).toEqual({ kind: 'flush', paths: ['a.json', 'b.json'] })
  expect(r.wave).toEqual([])
})

test('封顶到：距末条未满 windowMs 但距首条超 maxMs → flush', () => {
  const r = call([m('a.json', 1000), m('b.json', 10000)], timer(13500))
  expect(r.action).toEqual({ kind: 'flush', paths: ['a.json', 'b.json'] })
  expect(r.wave).toEqual([])
})

test('两条规则都未到 → hold，波原样保留', () => {
  const r = call([m('a.json', 1000), m('b.json', 10000)], timer(12000))
  expect(r.action).toEqual({ kind: 'hold' })
  expect(r.wave.map(x => x.path)).toEqual(['a.json', 'b.json'])
})

test('边界：timer 距末条恰好等于 windowMs → flush（契约是 >=）', () => {
  expect(call([m('a.json', 1000), m('b.json', 2000)], timer(7000)).action.kind).toBe('flush')
})

test('边界：timer 距首条恰好等于 maxMs → flush（契约是 >=）', () => {
  expect(call([m('a.json', 1000), m('b.json', 10000)], timer(13000)).action.kind).toBe('flush')
})

test('边界：两条规则各差 1ms → hold', () => {
  expect(call([m('a.json', 1000), m('b.json', 10000)], timer(12999)).action.kind).toBe('hold')
})

test('单条波也能 flush，paths 只有一条', () => {
  expect(call([m('a.json', 1000)], timer(6000)).action).toEqual({ kind: 'flush', paths: ['a.json'] })
})

test('ts 乱序到达：窗口判定看末条（到达序），不看最大 ts', () => {
  const r = call([m('a.json', 5000), m('b.json', 1000)], timer(6000))
  expect(r.action).toEqual({ kind: 'flush', paths: ['a.json', 'b.json'] })
})

test('媒体单投：无波时媒体到达 → flush 只含它自己', () => {
  const r = call([], arrive(m('img.json', 1000, true)))
  expect(r.action).toEqual({ kind: 'flush', paths: ['img.json'] })
  expect(r.wave).toEqual([])
})

test('媒体截波：波内媒体 → flushThenSingle，波先投、媒体单投', () => {
  const r = call([m('a.json', 1000), m('b.json', 2000)], arrive(m('img.json', 3000, true)))
  expect(r.action).toEqual({ kind: 'flushThenSingle', paths: ['a.json', 'b.json'], single: 'img.json' })
  expect(r.wave).toEqual([])
})

test('媒体截波时 paths 不含媒体自身（反向）', () => {
  const a: any = call([m('a.json', 1000)], arrive(m('img.json', 2000, true))).action
  expect(a.paths).not.toContain('img.json')
})

test('空波 timer → hold 且幂等，连调两次结果一致', () => {
  const r1 = call([], timer(999999)), r2 = call(r1.wave, timer(999999))
  expect(r1.action).toEqual({ kind: 'hold' })
  expect(r2).toEqual(r1)
})

test('windowMs=0：文本到达立即 flush，不开波', () => {
  const r = call([], arrive(m('a.json', 1000)), 0)
  expect(r.action).toEqual({ kind: 'flush', paths: ['a.json'] })
  expect(r.wave).toEqual([])
})

test('windowMs=0：媒体到达同样立即 flush', () => {
  expect(call([], arrive(m('img.json', 1000, true)), 0).action).toEqual({ kind: 'flush', paths: ['img.json'] })
})

test('windowMs 为负同样视为关闭防抖', () => {
  expect(call([], arrive(m('a.json', 1000)), -1).action).toEqual({ kind: 'flush', paths: ['a.json'] })
})

test('不修改传入的 wave 数组（无副作用）', () => {
  const wave = [m('a.json', 1000)]
  call(wave, arrive(m('b.json', 2000)))
  expect(wave.map(x => x.path)).toEqual(['a.json'])
})

test('错误契约：wave 非数组 → TypeError', () => {
  expect(() => call(null as any, timer(1))).toThrow(TypeError)
})

test('错误契约：事件 kind 非法 → TypeError', () => {
  expect(() => call([], { kind: 'boom', nowMs: 1 } as any)).toThrow(TypeError)
})

test('错误契约：arrive 缺 msg → TypeError', () => {
  expect(() => call([], { kind: 'arrive' } as any)).toThrow(TypeError)
})

test('错误契约：arrive 的 ts 为 NaN → TypeError', () => {
  expect(() => call([], arrive(m('a.json', NaN)))).toThrow(TypeError)
})

test('错误契约：arrive 的 ts 为 Infinity → TypeError', () => {
  expect(() => call([], arrive(m('a.json', Infinity)))).toThrow(TypeError)
})

test('错误契约：arrive 的 ts 为字符串 → TypeError', () => {
  expect(() => call([], arrive(m('a.json', '1000' as any)))).toThrow(TypeError)
})

test('错误契约：timer 的 nowMs 非有限数 → TypeError', () => {
  expect(() => call([m('a.json', 1000)], timer(NaN))).toThrow(TypeError)
})
