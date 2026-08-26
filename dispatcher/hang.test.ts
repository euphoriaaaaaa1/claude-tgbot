// 黑盒验收：只依据 .devflow/INTERFACE-hang.md（含裁决1/2/4），未读任何实现。实现缺失时全红是预期。
import { test, expect } from 'bun:test'
import { decideHang, type HangState } from './hang_plan'

const MIN = 60_000
const T0 = Date.UTC(2026, 7, 25, 4, 0, 0) // 固定假基准
const NONE = { kind: 'none' } as const   // as const 只为过本仓 tsc 严格档，断言语义不变
const st = (o: Partial<HangState> = {}): HangState =>
  ({ chatId: 'c1', armedAt: T0, stage: 0, nextAt: T0 + 7 * MIN, topicHint: '', episodesToday: 0, dayKey: '2026-08-25', ...o })
const out = (nowMs: number, hadRecentInbound = true) => ({ kind: 'outboundReply', chatId: 'c1', nowMs, hadRecentInbound }) as const
const inb = (nowMs: number) => ({ kind: 'inbound', chatId: 'c1', nowMs }) as const
const tick = (nowMs: number, activityInterruptible = true, asleep = false) => ({ kind: 'tick', nowMs, activityInterruptible, asleep }) as const
const isArmed = (r: { state: HangState | null }) => r.state !== null && r.state.stage === 0

// ── 武装（四要素齐全 / 各缺一）────────────────────────────────────
test('武装_四要素齐全_只落状态不注入_不预扣当日额度(裁决1)', () => {
  const r = decideHang(null, out(T0), 0.5)
  expect(r.action).toEqual(NONE)
  expect(r.state).toMatchObject({ chatId: 'c1', armedAt: T0, stage: 0, nextAt: T0 + 7 * MIN, episodesToday: 0 })
})
test.each([[0, 5], [0.5, 7], [1, 9]])('武装_rand01=%p_首档定在now+%p分钟(7±2边界)', (rand, mins) => {
  expect(decideHang(null, out(T0), rand).state!.nextAt).toBe(T0 + mins * MIN)
})
test('武装_缺近期入站(等价bot主动消息S5)_不武装', () => {
  const r = decideHang(null, out(T0, false), 0.5)
  expect(r.action).toEqual(NONE)
  expect(r.state).toBeNull()
})
test.each([0, 1, 2] as const)('武装_已有现役事件stage=%p_不重复武装且不改nextAt', (stage) => {
  const r = decideHang(st({ stage, nextAt: T0 + 999 * MIN }), out(T0 + MIN), 0.5)
  expect(r.action).toEqual(NONE)
  expect(r.state!).toMatchObject({ stage, nextAt: T0 + 999 * MIN })
})
test('武装_今天已实际动作3次_第4次不武装', () => {
  let s: HangState | null = null, now = T0
  for (let i = 1; i <= 3; i++) {
    const a = decideHang(s, out(now), 0.5)
    expect(isArmed(a)).toBe(true)
    const b = decideHang(a.state, tick(a.state!.nextAt), 0.5) // 出追问=实际动作，此刻才计数
    expect(b.state!.episodesToday).toBe(i)
    now = b.state!.nextAt + MIN
    s = { ...b.state!, stage: 3 } // 终结本轮，只留当日记账
  }
  const r4 = decideHang(s, out(now), 0.5)
  expect(r4.action).toEqual(NONE)
  expect(isArmed(r4)).toBe(false)
})
test('武装后被inbound取消_零消耗_连聊5个来回仍能武装(裁决1理由)', () => {
  let s: HangState | null = null, now = T0
  for (let i = 0; i < 5; i++) {
    const a = decideHang(s, out(now), 0.5)
    expect(isArmed(a)).toBe(true)
    expect(a.state!.episodesToday).toBe(0)
    s = decideHang(a.state, inb(now + MIN), 0.5).state
    now += 2 * MIN
  }
})
test('武装_dayKey属于旧的一天_episodesToday清零', () => {
  const r = decideHang(st({ stage: 3, episodesToday: 3, dayKey: '2000-01-01' }), out(T0), 0.5)
  expect(isArmed(r)).toBe(true)
  expect(r.state!.episodesToday).toBe(0)
  expect(r.state!.dayKey).not.toBe('2000-01-01')
})

// ── inbound / tick ───────────────────────────────────────────────
test.each([0, 1, 2, 3] as const)('inbound_stage=%p_清除事件且不注入', (stage) => {
  const r = decideHang(st({ stage }), inb(T0 + MIN), 0.5)
  expect(r.action).toEqual(NONE)
  expect(r.state).toBeNull()
})
test('tick_未到点_不动作且状态原样', () => {
  const s = st()
  const r = decideHang(s, tick(s.nextAt - 1), 0.5)
  expect(r.action).toEqual(NONE)
  expect(r.state).toEqual(s)
})
test('tick_到点但在睡_顺延且不耗档位不耗次数', () => {
  const s = st()
  const r = decideHang(s, tick(s.nextAt, true, true), 0.5)
  expect(r.action).toEqual(NONE)
  expect(r.state!).toMatchObject({ stage: 0, episodesToday: 0 })
  expect(r.state!.nextAt).toBeGreaterThan(s.nextAt)
})
test('tick_到点且活动不可打断_只记档案并终结', () => {
  const s = st()
  const r = decideHang(s, tick(s.nextAt, false), 0.5)
  expect(r.action).toEqual({ kind: 'archiveOnly' })
  expect(r.state!.stage).toBe(3)
})
// as const 同上，只为过 tsc（表里的对象否则被推成 {kind: string}）
test.each([[true, { kind: 'injectFollowup', stage: 1 }], [false, { kind: 'archiveOnly' }]] as const)(
  '事件首个实际动作(可打断=%p)_此刻才把episodesToday+1(裁决1)', (interruptible, action) => {
    const s = st({ episodesToday: 0 })
    const r = decideHang(s, tick(s.nextAt, interruptible as boolean), 0.5)
    expect(r.action).toEqual(action)
    expect(r.state!.episodesToday).toBe(1)
  })
test.each([1, 2] as const)('同一事件的后续动作(stage=%p)_不再重复计数', (stage) => {
  const s = st({ stage, episodesToday: 1 })
  expect(decideHang(s, tick(s.nextAt), 0.5).state!.episodesToday).toBe(1)
})
test.each([[0, 0, 20], [0, 0.5, 25], [0, 1, 30], [1, 0, 75], [1, 0.5, 90], [1, 1, 105]])(
  'tick_stage%p到点可打断_出追问_rand01=%p_下一档+%p分钟(25±5/90±15边界)', (stage, rand, mins) => {
    const s = st({ stage: stage as 0 | 1 })
    const r = decideHang(s, tick(s.nextAt), rand)
    // 两个 as 只为过 tsc（stage+1 被推成 number），比较的值一字不变
    expect(r.action).toEqual({ kind: 'injectFollowup', stage: (stage + 1) as 1 | 2 })
    expect(r.state!.stage).toBe((stage + 1) as 1 | 2)
    expect(r.state!.nextAt).toBe(s.nextAt + mins * MIN)
  })
test('tick_stage2到点可打断_出记忆写入并终结', () => {
  const s = st({ stage: 2 })
  const r = decideHang(s, tick(s.nextAt), 0.5)
  expect(r.action).toEqual({ kind: 'injectMemoryWrite' })
  expect(r.state!.stage).toBe(3)
})
test.each([true, false])('stage3已终结_到点tick(可打断=%p)_不再打扰', (interruptible) => {
  expect(decideHang(st({ stage: 3 }), tick(T0 + 999 * MIN, interruptible), 0.5).action).toEqual(NONE)
})

// ── 安全降级 & 错误契约 ──────────────────────────────────────────
test.each([
  ['缺nextAt', { chatId: 'c1', armedAt: T0, stage: 0, topicHint: '', episodesToday: 1, dayKey: '2026-08-25' }],
  ['缺stage', { chatId: 'c1', armedAt: T0, nextAt: T0, topicHint: '', episodesToday: 1, dayKey: '2026-08-25' }],
  ['空对象', {}],
])('state结构%s_视为无事件安全降级_不误触发', (_n, bad) => {
  const r = decideHang(bad as unknown as HangState, tick(T0 + 999 * MIN), 0.5)
  expect(r.action).toEqual(NONE)
  expect(r.state).toBeNull()
})
test.each([NaN, Infinity, -Infinity])('nowMs非有限数(%p)_抛TypeError', (n) => {
  expect(() => decideHang(null, out(n as number), 0.5)).toThrow(TypeError)
})
test.each([-0.001, 1.001, NaN, -1])('rand01越界(%p)_抛TypeError', (r) => {
  expect(() => decideHang(null, out(T0), r)).toThrow(TypeError)
})
test.each([
  ['未知kind', { kind: 'boom', nowMs: T0 }], ['缺kind', { nowMs: T0 }], ['null事件', null], ['字符串事件', 'tick'],
])('事件非法(%s)_抛TypeError', (_n, ev) => {
  expect(() => decideHang(null, ev as never, 0.5)).toThrow(TypeError)
})
