// 白盒测试 · WorkerManager.onExit —— 陈旧退出守卫 + 在飞消息回插。
// 全程假 proc、临时 CHANNEL_DIR，不起真 claude、不碰真实 ~/.claude。
import { test, expect, afterEach } from 'bun:test'
import { mkdtempSync, rmSync } from 'fs'
import { join } from 'path'
import { tmpdir } from 'os'

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
