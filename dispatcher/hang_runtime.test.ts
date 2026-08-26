// 白盒单测：被晾感知的 IO 层（状态落盘、桥降级、档案取用）。
// 假时钟 + 假桥 + 临时目录，全程不碰真 python/真 inbox；chat_id 是假值。
import { test, expect } from 'bun:test'
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'
import { createHangRuntime, takeHangArchive, isAsleep, type Situation } from './hang_runtime'
import type { HangState } from './hang_plan'

const MIN = 60_000
const CHAT = 'FAKEDM'
const FREE: Situation = { name: '追剧', state: 'free', interruptible: true }
const CLASS: Situation = { name: '上课', state: 'busy_class', interruptible: false }
const SLEEP: Situation = { name: '睡觉中', state: 'sleeping', interruptible: true }

type Harness = ReturnType<typeof harness>
function harness(opts: { sit?: Situation | null; t0?: number } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'hang-'))
  let clock = opts.t0 ?? Date.UTC(2026, 7, 25, 4, 0, 0)
  let sit: Situation | null = opts.sit === undefined ? FREE : opts.sit   // null 是"桥挂"，不是缺省
  const injected: { chatId: string; text: string }[] = []
  const logs: string[] = []
  const rt = createHangRuntime({
    channelDir: dir,
    inject: (chatId, text) => injected.push({ chatId, text }),
    probe: () => sit,
    now: () => clock,
    rand: () => 0.5,                     // 抖动固定 → 首档正好 7 分钟
    log: line => logs.push(line),
  })
  return {
    dir, rt, injected, logs,
    at: () => clock,
    advance: (ms: number) => { clock += ms },
    setSit: (s: Situation | null) => { sit = s },
    state: (): HangState | undefined => {
      const p = join(dir, '.hang-state.json')
      return existsSync(p) ? JSON.parse(readFileSync(p, 'utf8')).chats?.[CHAT] : undefined
    },
    day: () => {
      const p = join(dir, '.hang-state.json')
      return existsSync(p) ? JSON.parse(readFileSync(p, 'utf8')).day : undefined
    },
    archive: () => {
      const p = join(dir, '.hang-archive.json')
      return existsSync(p) ? JSON.parse(readFileSync(p, 'utf8')) : {}
    },
  }
}

/** 回一条"对用户的回复"：先入站再出站 */
function armIt(h: Harness): void {
  h.rt.onInbound(CHAT)
  h.advance(MIN)
  h.rt.onOutbound(CHAT)
}

test('回复后武装_状态落盘_首档7分钟_此刻不注入', () => {
  const h = harness()
  armIt(h)
  expect(h.injected).toEqual([])
  expect(h.state()).toMatchObject({ chatId: CHAT, stage: 0, episodesToday: 0 })
  expect(h.state()!.nextAt - h.at()).toBe(7 * MIN)
})

test('十分钟内没有入站_视为bot主动消息_不武装', () => {
  const h = harness()
  h.rt.onInbound(CHAT)
  h.advance(11 * MIN)
  h.rt.onOutbound(CHAT)
  expect(h.state()).toBeUndefined()
})

test('到点且闲着_注入一次追问且带hang-check前缀_档位推进', () => {
  const h = harness()
  armIt(h)
  h.advance(7 * MIN - 1)
  h.rt.tick()
  expect(h.injected).toEqual([])                    // 差 1ms 不动
  h.advance(1)
  h.rt.tick()
  expect(h.injected.length).toBe(1)
  expect(h.injected[0]!.chatId).toBe(CHAT)
  expect(h.injected[0]!.text).toStartWith('[hang-check]')
  expect(h.injected[0]!.text).toContain('7')        // 未回时长从出站那一刻算
  expect(h.state()).toMatchObject({ stage: 1, episodesToday: 1 })
})

test('用户回话_事件清除且后续到点不再注入', () => {
  const h = harness()
  armIt(h)
  h.advance(3 * MIN)
  h.rt.onInbound(CHAT)
  expect(h.state()).toBeUndefined()
  h.advance(60 * MIN)
  h.rt.tick()
  expect(h.injected).toEqual([])
})

test('不可打断_零注入_落档案_下次注入带迟到反应且清档', () => {
  const h = harness({ sit: CLASS })
  armIt(h)
  h.advance(7 * MIN)
  h.rt.tick()
  expect(h.injected).toEqual([])
  expect(h.archive()[CHAT]).toMatchObject({ activity: '上课' })
  expect(h.state()!.stage).toBe(3)

  const late = takeHangArchive(h.dir, CHAT, h.at() + 30 * MIN)
  expect(late).toStartWith('[hang-check]')
  expect(late).toContain('上课')
  expect(late).toContain('37')                       // 落档时 7 分钟 + 档案躺了 30 分钟
  expect(takeHangArchive(h.dir, CHAT, h.at())).toBe('')   // 已清档，不重复带
})

test('桥挂_降级不追_不注入不动状态_告警只出一次', () => {
  const h = harness({ sit: null })
  armIt(h)
  const before = h.state()!.nextAt
  h.advance(7 * MIN)
  h.rt.tick()
  h.advance(MIN)
  h.rt.tick()
  expect(h.injected).toEqual([])
  expect(h.state()!.nextAt).toBe(before)             // 事件留着，等桥回来
  expect(h.logs.filter(l => l.startsWith('hang_probe_failed')).length).toBe(1)
  h.setSit(FREE)                                     // 桥恢复 → 照常追
  h.rt.tick()
  expect(h.injected.length).toBe(1)
})

test('在睡_顺延不追_不耗档位不耗次数', () => {
  const h = harness({ sit: SLEEP })
  armIt(h)
  const first = h.state()!.nextAt
  h.advance(7 * MIN)
  h.rt.tick()
  expect(h.injected).toEqual([])
  expect(h.state()).toMatchObject({ stage: 0, episodesToday: 0 })
  expect(h.state()!.nextAt).toBeGreaterThan(first)
})

test('三档走完_共两次追问加一次记忆写入_之后再tick也不打扰', () => {
  const h = harness()
  armIt(h)
  for (let i = 0; i < 3; i++) { h.advance(120 * MIN); h.rt.tick() }
  expect(h.injected.length).toBe(3)
  expect(h.injected[2]!.text).toContain('记忆')
  expect(h.state()!.stage).toBe(3)
  h.advance(120 * MIN)
  h.rt.tick()
  expect(h.injected.length).toBe(3)
})

test('状态文件损坏_不崩不误触发_还能重新武装', () => {
  const dir = mkdtempSync(join(tmpdir(), 'hang-'))
  writeFileSync(join(dir, '.hang-state.json'), '{"FAKEDM": {"stage": 0, "nex')  // 半截 JSON
  let clock = Date.UTC(2026, 7, 25, 4, 0, 0)
  const injected: string[] = []
  const rt = createHangRuntime({
    channelDir: dir, inject: (_c, t) => injected.push(t), probe: () => FREE,
    now: () => clock, rand: () => 0.5,
  })
  rt.tick()
  expect(injected).toEqual([])
  rt.onInbound(CHAT); clock += MIN; rt.onOutbound(CHAT)
  clock += 7 * MIN; rt.tick()
  expect(injected.length).toBe(1)
})

test('状态条目结构缺键_当没事件清掉_不注入', () => {
  const dir = mkdtempSync(join(tmpdir(), 'hang-'))
  writeFileSync(join(dir, '.hang-state.json'), JSON.stringify({ [CHAT]: { stage: 0, armedAt: 1 } }))
  const injected: string[] = []
  const rt = createHangRuntime({
    channelDir: dir, inject: (_c, t) => injected.push(t), probe: () => FREE,
    now: () => Date.UTC(2026, 7, 25, 4, 0, 0), rand: () => 0.5,
  })
  rt.tick()
  expect(injected).toEqual([])
  expect(rt.live()).toBe(0)
})

test('机器睡了一夜_醒来不追几小时前的那句', () => {
  const h = harness()
  armIt(h)
  h.advance(9 * 3600_000)
  h.rt.tick()
  expect(h.injected).toEqual([])
  expect(h.state()).toBeUndefined()
  expect(h.logs.some(l => l.includes('reason=stale'))).toBe(true)
})

test('日封顶三次_中间被回话打断也不刷新额度_第四次不再武装', () => {
  const h = harness({ sit: CLASS })                  // 不可打断 → 一次 tick 结一个事件
  const rounds: (number | undefined)[] = []
  for (let i = 0; i < 4; i++) {
    h.rt.onInbound(CHAT)                             // 每轮她都回过话（会清掉上一轮的事件记录）
    h.advance(MIN)
    h.rt.onOutbound(CHAT)
    h.advance(8 * MIN)
    h.rt.tick()
    rounds.push(h.day()?.count)
  }
  expect(rounds).toEqual([1, 2, 3, 3])               // 第四轮没起事件，额度不再涨
  expect(h.state()!.stage).toBe(3)
})

test('跨天_额度重新算', () => {
  const h = harness({ sit: CLASS })
  for (let i = 0; i < 3; i++) {
    h.rt.onInbound(CHAT); h.advance(MIN); h.rt.onOutbound(CHAT); h.advance(8 * MIN); h.rt.tick()
  }
  expect(h.day()!.count).toBe(3)
  h.advance(24 * 3600_000)
  h.rt.onInbound(CHAT); h.advance(MIN); h.rt.onOutbound(CHAT)
  expect(h.state()).toMatchObject({ stage: 0, episodesToday: 0 })
})

test('日志只含chat_id档位与原因码_不含正文与活动名', () => {
  const h = harness({ sit: CLASS })
  armIt(h)
  h.advance(7 * MIN)
  h.rt.tick()
  expect(h.logs.length).toBeGreaterThan(0)
  for (const line of h.logs) {
    expect(line).not.toContain('上课')
    expect(line).toMatch(/^[a-z_]+ (chat|count)=/)
  }
})

test('无档案时取档返回空串_文件不存在也不抛', () => {
  const dir = mkdtempSync(join(tmpdir(), 'hang-'))
  expect(takeHangArchive(dir, CHAT)).toBe('')
  writeFileSync(join(dir, '.hang-archive.json'), 'not json')
  expect(takeHangArchive(dir, CHAT)).toBe('')
})

test.each([['sleeping', true], ['睡觉中', true], ['free', false], ['busy_work', false], [null, false]])(
  'isAsleep(%p)=%p', (s, want) => { expect(isAsleep(s)).toBe(want) })
