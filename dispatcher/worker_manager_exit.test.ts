// 白盒测试 · WorkerManager —— onExit（陈旧退出守卫 + 在飞消息回插）与 inbox→队列的波次层。
// 全程假 proc、临时 CHANNEL_DIR，不起真 claude、不碰真实 ~/.claude。
//
// ⚠️ WorkerManager 的白盒测试请一律加在本文件里，别另起一个 *.test.ts：bun test 全仓共用
// 一份模块缓存，worker-manager 的模块级 CHANNEL_DIR 由**第一个** import 它的测试文件定死，
// 第二个文件再设 env 也没用（会拿到别人的临时目录，按文件名顺序随机翻车）。
import { test, expect, afterEach } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'
import { createBurstCollector } from './burst_inbox'

// 模块级 CHANNEL_DIR 在 import 时就定死，必须先设 env 再动态 import
const TMP = mkdtempSync(join(tmpdir(), 'worker-exit-test-'))
process.env.CHANNEL_DIR = TMP
process.env.BOT_NAME = 'testbot'
const { WorkerManager } = await import('./worker-manager')

afterEach(() => { rmSync(TMP, { recursive: true, force: true }) })

// 绕开构造函数：它会装 fs.watch + 两个常驻 setInterval，泄进测试进程里没人收得掉。
// 这里只给 onExit 真正读写的那几个字段，spawnWorker 用桩顶掉（否则 1 秒后真去起 claude）。
type FakeProc = { pid: number }
function mkManager(init: Record<string, unknown> = {}) {
  const m = Object.create(WorkerManager.prototype) as any
  Object.assign(m, {
    proc: null, phase: 'stopped', inFlight: null, inFlightContent: '', inFlightSince: 0,
    queue: [], restartAttempt: 0, intentionalKill: false, resultTimer: null,
    stdoutBuf: '', watchers: [], needsReplyReminder: false,
    ...init,
  })
  m.spawnCalls = 0
  m.spawnWorker = async () => { m.spawnCalls++ }
  m.drainInbox = () => {}
  return m
}
const fakeProc = (pid: number) => ({ pid } as FakeProc)
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

// ─── 陈旧退出守卫 ───────────────────────────────────────────────────────
test('陈旧proc的exit_不清掉新worker状态_也不多排一次spawn', async () => {
  const oldP = fakeProc(111), newP = fakeProc(222)
  // kill(oldP) 之后、exit 事件到达之前，有消息进来 ensure() 起了 newP
  const m = mkManager({ proc: newP, phase: 'ready' })
  m.onExit(0, oldP)
  expect(m.proc).toBe(newP)          // 新 worker 没被清掉
  expect(m.phase).toBe('ready')
  await sleep(1200)                  // BACKOFF_MS[0] = 1000
  expect(m.spawnCalls).toBe(0)       // 没有第二个 claude 被排上去
})

test('当前proc的exit_照常走重启', async () => {
  const p = fakeProc(111)
  const m = mkManager({ proc: p, phase: 'ready' })
  m.onExit(1, p)
  expect(m.proc).toBe(null)
  expect(m.phase).toBe('stopped')
  await sleep(1200)
  expect(m.spawnCalls).toBe(1)
})

test('kill后proc已置空的exit_仍走重启', async () => {
  // kill() 把 proc 置 null 后 exit 才到：这是最常见的主动重启路径，不能被守卫拦掉
  const m = mkManager({ proc: null, phase: 'stopped' })
  m.onExit(0, fakeProc(111))
  await sleep(1200)
  expect(m.spawnCalls).toBe(1)
})

test('不带proc参数的老调用_不受守卫影响', async () => {
  const m = mkManager({ proc: fakeProc(999), phase: 'ready' })
  m.onExit(0)
  expect(m.proc).toBe(null)
  await sleep(1200)
  expect(m.spawnCalls).toBe(1)
})

// ─── 在飞消息回插 ───────────────────────────────────────────────────────
test('在飞file消息_按raw回插到队头_内容一字不改', async () => {
  const p = fakeProc(1)
  const content = '【私聊】<channel chat_id="42">你好</channel>'
  const m = mkManager({
    proc: p, phase: 'ready',
    inFlight: { kind: 'file', path: '/tmp/fake-inbox/42_7.json' },  // 该文件写 stdin 时已删
    inFlightContent: content,
    queue: [{ kind: 'file', path: '/tmp/fake-inbox/42_8.json' }],
  })
  m.onExit(1, p)
  expect(m.queue.length).toBe(2)
  expect(m.queue[0]).toEqual({ kind: 'raw', content, label: 'requeue-after-restart' })
  expect(m.queue[1]).toEqual({ kind: 'file', path: '/tmp/fake-inbox/42_8.json' })  // 排在它后面
  expect(m.inFlight).toBe(null)
  expect(m.inFlightContent).toBe('')
  await sleep(1200)
  expect(m.spawnCalls).toBe(1)
})

test('在飞raw消息_原样回插不重新包装', () => {
  const p = fakeProc(1)
  const item = { kind: 'raw', content: '/compact', label: 'slash /compact' }
  const m = mkManager({ proc: p, phase: 'ready', inFlight: item, inFlightContent: '/compact' })
  m.onExit(1, p)
  expect(m.queue[0]).toBe(item)      // 同一个对象，label 保住（pump 靠它认 slash）
})

test('回插的label不以slash开头_不会被当成命令跳过冷轮提醒', () => {
  const p = fakeProc(1)
  const m = mkManager({
    proc: p, phase: 'ready',
    inFlight: { kind: 'file', path: '/tmp/x.json' }, inFlightContent: '正文',
  })
  m.onExit(1, p)
  expect((m.queue[0].label as string).startsWith('slash')).toBe(false)
})

test('在飞内容为空_不回插空消息', () => {
  const p = fakeProc(1)
  const m = mkManager({
    proc: p, phase: 'ready',
    inFlight: { kind: 'file', path: '/tmp/x.json' }, inFlightContent: '',
  })
  m.onExit(1, p)
  expect(m.queue.length).toBe(0)     // 空 content 回插只会给 claude 喂一条空消息
})

test('intentional kill_不回插不重启', async () => {
  const p = fakeProc(1)
  const m = mkManager({
    proc: p, phase: 'ready', intentionalKill: true,
    inFlight: { kind: 'file', path: '/tmp/x.json' }, inFlightContent: '正文',
  })
  m.onExit(0, p)
  expect(m.queue.length).toBe(0)     // /clearall 之类：本来就不该把旧消息带回来
  await sleep(1200)
  expect(m.spawnCalls).toBe(0)
})

test('没有在飞消息_队列不动', () => {
  const p = fakeProc(1)
  const m = mkManager({ proc: p, phase: 'ready', queue: [{ kind: 'file', path: '/tmp/y.json' }] })
  m.onExit(1, p)
  expect(m.queue.length).toBe(1)
})

// ─── inbox → 队列的波次层接线（凑一波再递）──────────────────────────────
// 纯逻辑与落盘由 burst.test.ts / burst_collect.test.ts 覆盖，这里只验两件接线的事：
// 空闲时首条不入队、以及 5s 兜底扫重复看见同一个原件时不会把它再入一次队。
function mkBurstManager(windowMs = 5000) {
  const inbox = join(TMP, 'inbox')
  const dest = join(TMP, '.burst')
  mkdirSync(inbox, { recursive: true })
  let clock = 1_000_000
  let seq = 0
  const m = mkManager({ phase: 'ready' })
  delete m.drainInbox          // mkManager 把它桩掉了，这几条测的就是它
  m.ensure = async () => {}
  m.pump = () => {}
  m.burst = createBurstCollector({
    windowMs, maxMs: 12_000, destDir: dest, now: () => clock,
    deliver: (p: string) => m.enqueueFile(p),
  })
  return {
    m,
    at: (ms: number) => { clock = ms },
    tick: () => m.burst.tick(),
    add: (text: string, extra: Record<string, unknown> = {}) => {
      const p = join(inbox, `FAKECHAT-1_${++seq}.json`)
      writeFileSync(p, JSON.stringify({
        chat_id: 'FAKECHAT-1', message_id: seq, from_id: 'U1', scene: 'private',
        chat_type: 'private', ts: `2026-08-25T0${seq}:00:00.000Z`, text, ...extra,
      }))
      return p
    },
  }
}

test('空闲时连发三条_先攒波不入队_满窗后只入队一个合并件', () => {
  const r = mkBurstManager()
  r.at(1000); r.add('A1'); r.add('B2'); r.add('C3')
  r.m.drainInbox()
  expect(r.m.queue).toEqual([])            // 三条都在波里，一条都没进队
  r.at(6100); r.tick()
  expect(r.m.queue.length).toBe(1)
  const merged = JSON.parse(readFileSync(r.m.queue[0].path, 'utf8'))
  expect([merged.text, merged.chat_id, merged.message_id]).toEqual(['A1\nB2\nC3', 'FAKECHAT-1', 1])
})

test('攒波期间被兜底扫重复看见_同一原件不会重复入队', () => {
  const r = mkBurstManager()
  r.at(1000); r.add('A1'); r.add('B2')
  r.m.drainInbox(); r.m.drainInbox(); r.m.drainInbox()   // 5s 定时扫 + fs.watch 重复触发
  expect(r.m.queue).toEqual([])
  r.at(6100); r.tick()
  expect(r.m.queue.length).toBe(1)
  expect(JSON.parse(readFileSync(r.m.queue[0].path, 'utf8')).text).toBe('A1\nB2')
})

test('队列有积压时不攒波_照旧直接入队', () => {
  const r = mkBurstManager()
  r.m.queue.push({ kind: 'raw', content: '/compact', label: 'slash /compact' })
  r.at(1000); const a = r.add('A1')
  r.m.drainInbox()
  expect(r.m.queue.map((q: any) => q.path ?? q.label)).toEqual(['slash /compact', a])
})

test('burstWindowMs=0_关闭防抖_原件逐条直接入队', () => {
  const r = mkBurstManager(0)
  r.at(1000); const a = r.add('A1'); const b = r.add('B2')
  r.m.drainInbox()
  expect(r.m.queue.map((q: any) => q.path)).toEqual([a, b])
})
