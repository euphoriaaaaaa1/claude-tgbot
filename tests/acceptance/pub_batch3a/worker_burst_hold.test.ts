// pub-batch3a · worker 进波规则与「扣住」解除（行为清单 8-13）。
// 夹具照 dispatcher/worker_manager_exit.test.ts 的 mkBurstManager：绕开构造函数，收集器用假时钟，
// 并把 holdWhile 接到 manager 的 burstHeld。「本聊天在飞」不手填字段，而是真走一遍 pump 派发
// （假子进程收 stdin），回合结束/死信/退出/停机分别走对应的公开方法。
// 全程假 proc、临时 CHANNEL_DIR/HOME，不起真 claude、不联网。
import { afterEach, expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createBurstCollector } from '../../../dispatcher/burst_inbox'
import { FakeChild } from '../pub_parity/wm_harness'

// 模块级 CHANNEL_DIR 在 import 时定死：必须先设 env 再动态 import
const TMP = mkdtempSync(join(tmpdir(), 'pb3a-wm-'))
Object.assign(process.env, {
  CHANNEL_DIR: TMP, BOT_NAME: 'testbot', HOME: join(TMP, 'home'), USERPROFILE: join(TMP, 'home'),
  TELEGRAM_BOT_TOKEN: 'FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK',
  DISPATCHER_URL: 'http://127.0.0.1:9', TELEGRAM_DISPATCHER_URL: 'http://127.0.0.1:9',
})
const { WorkerManager } = await import('../../../dispatcher/worker-manager')

afterEach(() => { rmSync(TMP, { recursive: true, force: true }) })

let pidSeq = 2_000_000_000   // 远大于系统 pid 上限，任何误发信号都只会 ESRCH
function mkRig(windowMs = 8000) {
  const inbox = join(TMP, 'inbox'), dest = join(TMP, '.burst'), flight = join(TMP, 'flight')
  for (const d of [inbox, dest, flight, join(TMP, 'home')]) mkdirSync(d, { recursive: true })
  let clock = 1_000_000
  let seq = 0
  const delivered: string[] = []
  const proc = new FakeChild(++pidSeq)
  const m = Object.create(WorkerManager.prototype) as any
  Object.assign(m, {
    proc, pid: proc.pid, phaseVal: 'running', handledExit: false, pendingRestart: false, stopRequested: false,
    stopWaitResolve: null, stopGraceTimer: null, stopTimeoutTimer: null,
    inFlight: null, inFlightContent: '', inFlightSince: 0, queue: [], restartAttempt: 0,
    intentionalKill: false, resultTimer: null, stdoutBuf: '', watchers: [], needsReplyReminder: false,
    channelDir: TMP, now: () => Date.now(),
    timer: { setTimeout, clearTimeout, setInterval, clearInterval },
  })
  m.spawnWorker = async () => {}
  m.ensure = async () => {}
  m.burst = createBurstCollector({
    windowMs, maxMs: 20_000, destDir: dest, now: () => clock,
    deliver: (p: string) => { delivered.push(p); m.enqueueFile(p) },
    holdWhile: (chatId: string) => (typeof m.burstHeld === 'function' ? m.burstHeld(chatId) : false),
  })
  return {
    m, delivered,
    at: (ms: number) => { clock = ms },
    tick: () => m.burst.tick(),
    /** 让 chatId 真的"在飞"：走一遍 pump 派发，随后把 pump 桩掉，免得后续入队又被自动派发 */
    startFlight: (chatId: string | number) => {
      const p = join(flight, `${chatId}_f${++seq}.json`)
      const scene = String(chatId).startsWith('-') ? 'group' : 'private'
      writeFileSync(p, JSON.stringify({
        chat_id: chatId, message_id: 900 + seq, from_id: 'U1', scene, chat_type: scene,
        ts: new Date().toISOString(), text: 'in-flight', ...(scene === 'group' ? { from_username: 'alpha' } : {}),
      }))
      m.queue.push({ kind: 'file', path: p })
      m.pump()
      expect(m.inFlight).not.toBeNull()   // 前置条件：确实在飞了
      m.pump = () => {}
      m.queue.length = 0
    },
    add: (text: string, extra: Record<string, unknown> = {}) => {
      const chat = extra.chat_id ?? 'FAKECHAT-1'
      const p = join(inbox, `${chat}_${++seq}.json`)
      writeFileSync(p, JSON.stringify({
        chat_id: 'FAKECHAT-1', message_id: seq, from_id: 'U1', scene: 'private',
        chat_type: 'private', ts: `2026-08-25T0${seq % 10}:00:00.000Z`, text, ...extra,
      }))
      return p
    },
    queuedPaths: () => m.queue.map((q: any) => q.path ?? q.label),
  }
}
const mergedText = (p: string) => JSON.parse(readFileSync(p, 'utf8')).text

test('8_本聊天在飞时来文字_扣住不入队_回合成功后下一拍入队一件', () => {
  const r = mkRig()
  r.startFlight('FAKECHAT-1')
  r.at(1000); r.add('B'); r.m.drainInbox()
  expect(r.m.queue).toEqual([])                  // 在飞期间不入队
  r.at(10_000); r.tick()                         // 距到达 9s，满窗但仍在飞
  expect(r.m.queue).toEqual([])
  r.m.settleInFlightSuccess()
  r.tick()
  expect(r.m.queue.length).toBe(1)
  expect(mergedText(r.m.queue[0].path)).toBe('B')
})

// 四种结束方式，各自之后「满窗的下一拍」都必须放行（扣住不能卡死）
const ENDINGS: Array<[string, (r: ReturnType<typeof mkRig>) => Promise<void> | void]> = [
  ['死信', r => { r.m.deadLetterInFlight('FAKE_REASON') }],
  ['进程退出', r => { r.m.onExit(1, r.m.proc) }],
  ['停机', async r => {
    const proc = r.m.proc
    r.m.killFn = () => { setTimeout(() => r.m.onExit(0, proc), 5) }   // 假 kill：随后"进程退出"
    await r.m.stop()
  }],
  ['重试时无进程_件塞回队首_在飞清空但聊天标记残留', r => { r.m.proc = null; r.m.retryInFlight() }],
]
for (const [label, end] of ENDINGS) {
  test(`9_${label}之后_满窗的下一拍放行`, async () => {
    const r = mkRig()
    r.startFlight('FAKECHAT-1')
    r.at(1000); r.add('B'); r.m.drainInbox()
    r.at(10_000); r.tick()
    expect(r.m.queue).toEqual([])                // 在飞期间确实被扣住
    expect(r.delivered).toEqual([])
    await end(r)
    r.tick()
    expect(r.delivered.length).toBe(1)
    expect(mergedText(r.delivered[0]!)).toBe('B')
  })
}

test('10_回合结束时末条刚到3秒不投_末条满8秒才入队', () => {
  const r = mkRig()
  r.startFlight('FAKECHAT-1')
  r.at(1000); r.add('B'); r.m.drainInbox()
  r.at(8000); r.add('C'); r.m.drainInbox()
  r.at(11_000); r.m.settleInFlightSuccess()      // 末条 C 到后 3s 回合结束
  r.tick()
  expect(r.m.queue).toEqual([])
  r.at(15_999); r.tick()
  expect(r.m.queue).toEqual([])
  r.at(16_000); r.tick()                         // 末条满 8s
  expect(r.m.queue.length).toBe(1)
  expect(mergedText(r.m.queue[0].path)).toBe('B\nC')
})

test('11_X在飞且有扣住的波时_Y不在飞来文字_Y立即入队_X不在队里', () => {
  const r = mkRig()
  r.startFlight('FAKECHAT-1')
  r.at(1000); r.add('X1'); r.m.drainInbox()
  const y = r.add('Y1', { chat_id: 'FAKECHAT-2', from_id: 'U2' })
  r.m.drainInbox()
  expect(r.queuedPaths()).toEqual([y])
})

test('12_inbox里chat_id是数字_在飞的是字符串_仍被扣住', () => {
  const r = mkRig()
  r.startFlight('-100123')
  r.at(1000)
  r.add('G1', { chat_id: -100123, scene: 'group', chat_type: 'supergroup', from_username: 'beta' })
  r.m.drainInbox()
  expect(r.m.queue).toEqual([])
})

test('13a_在飞的是别的聊天且X无波_X直接入队', () => {
  const r = mkRig()
  r.startFlight('FAKECHAT-2')
  r.at(1000); const a = r.add('A1'); r.m.drainInbox()
  expect(r.queuedPaths()).toEqual([a])
})

test('13b_无在飞但队列有积压_直接入队不攒波', () => {
  const r = mkRig()
  r.m.queue.push({ kind: 'raw', content: '/compact', label: 'slash /compact' })
  r.at(1000); const a = r.add('A1'); r.m.drainInbox()
  expect(r.queuedPaths()).toEqual(['slash /compact', a])
})

test('13c_windowMs为0_本聊天在飞也逐条直接入队', () => {
  const r = mkRig(0)
  r.startFlight('FAKECHAT-1')
  r.at(1000); const a = r.add('A1'); const b = r.add('B2'); r.m.drainInbox()
  expect(r.queuedPaths()).toEqual([a, b])
})
