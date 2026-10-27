// H2b 在飞消息重放 —— 黑盒验收夹具(自带)
//
// 只依据 .devflow/BRIEF-h2b-replay.md 与 .devflow/INTERFACE-h2b-replay.md 的对外契约。不读实现。
// 全程假 worker / 假时钟 / 假发送 / 临时目录:绝不真起进程、不发真消息、不碰真实信道目录。
//
// 注入面(依既有 h2a 夹具的可注入依赖惯例):构造参数 {channelDir, now, timer, spawn, kill, procInfo}。
// 真实现里 spawn 返回的句柄被当作 worker;此处注入一个只提供 stdin/ stdout / exit 的假句柄。
// 驱动面(运行时探测到的公开方法):onEvent(result) / onStdout(chunk) / onExit(code,proc) /
//   checkStuck() / pump() / drainInbox() / enqueueFile(path) / injectRaw(text,meta)。
// 目录约定(INTERFACE 一):worker-manager 在 <CHANNEL_DIR>/inbox 收件,待投件在 <inbox>/.pending/,
//   死信在 <inbox>/.dead-letter/。
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// 模块级 CHANNEL_DIR 在首次 import worker-manager 时定死 → 先设 env 再动态 import。
const BASE = mkdtempSync(join(tmpdir(), 'h2b-ch-'))
process.env.CHANNEL_DIR = BASE
process.env.BOT_NAME ??= 'h2b-bot'
process.env.BOT_NAMESPACE ??= '6ba7b810-9dad-11d1-80b4-00c04fd430c8'  // 构造 WorkerManager 派生会话 uuid 用

// fail-closed 隔离:即便被测实现无视注入真去 spawn,也只会命中假桩,绝不触及真实进程。
const STUB = join(BASE, 'stub')
mkdirSync(STUB, { recursive: true })
for (const n of ['claude', 'tmux', 'launchctl', 'pkill', 'kill']) {
  const p = join(STUB, n)
  writeFileSync(p, `#!/bin/sh\necho "$0 $@" >> "${BASE}/calls.log"\nexit 97\n`)
  chmodSync(p, 0o755)
}
process.env.PATH = `${STUB}:${process.env.PATH ?? ''}`

const mod: any = await import('../../../dispatcher/worker-manager')
export const WorkerManager: any = mod.WorkerManager ?? mod.Worker ?? mod.default

export const INBOX = join(BASE, 'inbox')
export const PENDING = join(INBOX, '.pending')
export const DEAD = join(INBOX, '.dead-letter')

// ─── 假时钟:定时器手动 advance 触发 ──────────────────────────────────────────
export interface FakeClock {
  timer: { setTimeout: (fn: () => void, ms?: number) => number; clearTimeout: (id: number) => void; setInterval: (fn: () => void, ms?: number) => number; clearInterval: (id: number) => void }
  now: () => number
  advance: (ms: number) => void
}
export function makeClock(start = 1_000_000): FakeClock {
  let now = start
  const timers: { id: number; at: number; fn: () => void; interval?: number }[] = []
  let nextId = 1
  const remove = (id: number) => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1) }
  const timer = {
    setTimeout: (fn: () => void, ms = 0) => { const id = nextId++; timers.push({ id, at: now + ms, fn }); return id },
    clearTimeout: (id: number) => remove(id),
    setInterval: (fn: () => void, ms = 1) => { const id = nextId++; timers.push({ id, at: now + ms, fn, interval: ms }); return id },
    clearInterval: (id: number) => remove(id),
  }
  function advance(ms: number) {
    const target = now + ms
    let guard = 0
    for (;;) {
      if (++guard > 100000) throw new Error('fake clock: 定时器自触发死循环')
      timers.sort((a, b) => a.at - b.at)
      const t = timers[0]
      if (!t || t.at > target) break
      now = t.at
      if (t.interval != null) t.at = now + t.interval
      else remove(t.id)
      t.fn()
    }
    now = target
  }
  return { timer, now: () => now, advance }
}

// ─── 假 worker 句柄:stdin 收写、stdout/stderr 可发事件、可 emit exit ──────────
function emitter() {
  const ls: Record<string, ((...a: any[]) => void)[]> = {}
  const e: any = {
    on(x: string, cb: any) { (ls[x] ||= []).push(cb); return e },
    once(x: string, cb: any) { const w = (...a: any[]) => { e.off(x, w); cb(...a) }; return e.on(x, w) },
    off(x: string, cb: any) { const a = ls[x]; if (a) { const i = a.indexOf(cb); if (i >= 0) a.splice(i, 1) } return e },
    emit(x: string, ...a: any[]) { for (const cb of [...(ls[x] || [])]) cb(...a) },
    listenerCount(x: string) { return (ls[x] || []).length },
  }
  return e
}
export interface FakeHandle { pid: number; writes: string[]; stdin: any; stdout: any; stderr: any; kill: () => void; on: any; once: any; off: any; emit: any }
export function makeHandle(pid: number): FakeHandle {
  const writes: string[] = []
  const h: any = emitter()
  h.pid = pid
  h.writes = writes
  h.stdin = { write: (s: any, cb?: any) => { writes.push(String(s)); if (cb) cb(); return true }, end: () => {} }
  h.stdout = emitter()
  h.stderr = emitter()
  h.kill = () => {}
  return h as FakeHandle
}

// ─── stderr 事件捕获(契约:事件行都写 stderr)──────────────────────────────────
let buf: string[] = []
let origWrite: any = null
export function captureStderr() {
  buf = []
  if (!origWrite) origWrite = process.stderr.write.bind(process.stderr)
  ;(process.stderr as any).write = (c: any) => { buf.push(typeof c === 'string' ? c : String(c)); return true }
}
export function releaseStderr() { if (origWrite) { (process.stderr as any).write = origWrite; origWrite = null } }
export function eventText(): string { return buf.join('') }
export function eventLines(): string[] { return eventText().split('\n').map(s => s.trim()).filter(Boolean) }
export function countEvents(needle: string): number { return eventLines().filter(l => l.includes(needle)).length }

// ─── 假扫描函数:既有"待投件"口径(INTERFACE 一.1)──────────────────────────────
// 待投件 = 以 .json 结尾、且非隐藏件。inflight 名(.inflight-*.part)必须不被它认作待投件。
export const isPendingName = (name: string): boolean => name.endsWith('.json') && !name.startsWith('.')

// ─── 夹具主入口 ──────────────────────────────────────────────────────────────
export interface Harness {
  m: any
  clock: FakeClock
  handle: FakeHandle
  channelDir: string
  inboxDir: string
  pendingDir: string
  deadDir: string
  sent: { chat: string; text: string }[]
  spawnCalls: number
  put: (name: string, meta?: Record<string, unknown>) => string
  putRaw: (name: string, body: string) => string
  exists: (dir: string, name: string) => boolean
  namesIn: (dir: string) => string[]
  inflight: () => string[]
  enqueue: (path: string) => void
  deliver: (path: string) => Promise<void>
  result: (r: Record<string, unknown>) => void
  stdout: (chunk: string) => void
  exit: (code?: number, extra?: Record<string, unknown>) => void
  stallTick: () => void
  advance: (ms: number) => void
  settle: (n?: number) => Promise<void>
  phase: () => any
  cleanInbox: () => void
}
export interface BootOpts { init?: Record<string, unknown>; start?: number }

let pidSeq = 4000
let nameSeq = 0
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await Promise.resolve() }

export async function boot(over: BootOpts = {}): Promise<Harness> {
  // 每个用例独立:清空共享 inbox,避免上一用例残留串味
  rmSync(INBOX, { recursive: true, force: true })
  mkdirSync(PENDING, { recursive: true })
  mkdirSync(DEAD, { recursive: true })

  const clock = makeClock(over.start ?? 1_000_000)
  const handle = makeHandle(++pidSeq)
  let spawnCalls = 0
  const sent: { chat: string; text: string }[] = []
  const rec = (a: any, b: any) => {
    if (a && typeof a === 'object') sent.push({ chat: String(a.chat_id ?? a.chat ?? ''), text: String(a.text ?? b ?? '') })
    else sent.push({ chat: String(a ?? ''), text: String(b ?? '') })
    return true
  }

  const deps: any = {
    channelDir: BASE, now: clock.now, timer: clock.timer,
    spawn: () => { spawnCalls++; return handle },
    kill: () => true, procInfo: () => null,
    send: rec, sendMessage: rec, sendToChat: rec, notifyUser: rec,
    ...(over.init ?? {}),
  }
  const m: any = new WorkerManager(deps)
  for (const k of ['send', 'sendMessage', 'sendToChat', 'notifyUser', 'pushUser']) if (typeof m[k] !== 'function') m[k] = rec
  // 假发送:接口可能叫 sendText / pushToChat 等,统一兜一层
  if (typeof m.sendText !== 'function') m.sendText = rec
  if (typeof m.pushText !== 'function') m.pushText = rec

  await m.ensure()
  await settle()
  // 让 worker "就绪":模拟 claude 首个 system/init 行(不少实现在此之前不下发)
  try { handle.stdout.emit('data', JSON.stringify({ type: 'system', subtype: 'init' }) + '\n') } catch { /* ignore */ }
  await settle()

  const putRaw = (name: string, body: string) => { const p = join(PENDING, name); writeFileSync(p, body); return p }
  const put = (name: string, meta: Record<string, unknown> = {}) =>
    putRaw(name, JSON.stringify({ chat_id: 'FAKECHAT-1', message_id: ++nameSeq, from_id: 'U1', scene: 'private', chat_type: 'private', ts: '2026-08-25T01:00:00.000Z', text: `BODY-${nameSeq}`, ...meta }))

  const exists = (dir: string, name: string) => existsSync(join(dir, name))
  const namesIn = (dir: string) => (existsSync(dir) ? readdirSync(dir) : [])
  const inflight = () => namesIn(PENDING).filter(n => n.startsWith('.inflight-'))
  const enqueue = (path: string) => { if (typeof m.enqueueFile === 'function') m.enqueueFile(path); else m.queue.push({ kind: 'file', path }) }
  const deliver = async (path: string) => { m.queue = [{ kind: 'file', path }]; await m.pump() }
  const result = (r: Record<string, unknown>) => { if (typeof m.onEvent === 'function') m.onEvent(r); else throw new Error('h2b fixture: 无 onEvent 入口(结果事件无法投递)') }
  const stdout = (chunk: string) => { if (typeof m.onStdout === 'function') m.onStdout(chunk); else handle.stdout.emit('data', chunk) }
  const exit = (code = 0, extra: Record<string, unknown> = {}) => {
    if (typeof m.onExit === 'function') m.onExit(code, Object.assign(Object.create(handle), { pid: handle.pid }, extra))
    else handle.emit('exit', code)
  }
  const stallTick = () => { if (typeof m.checkStuck === 'function') m.checkStuck(); else throw new Error('h2b fixture: 无 checkStuck 入口') }

  return {
    m, clock, handle, channelDir: BASE, inboxDir: INBOX, pendingDir: PENDING, deadDir: DEAD, sent,
    get spawnCalls() { return spawnCalls },
    put, putRaw, exists, namesIn, inflight, enqueue, deliver, result, stdout, exit, stallTick,
    advance: clock.advance, settle, phase: () => (typeof m.phase === 'function' ? m.phase() : m.phaseVal),
    cleanInbox: () => { rmSync(INBOX, { recursive: true, force: true }); mkdirSync(PENDING, { recursive: true }); mkdirSync(DEAD, { recursive: true }) },
  }
}

export function cleanup(_h: Harness) { /* inbox 由 boot() 自清,进程退出即回收 BASE */ }
export function cleanupAll() { try { rmSync(BASE, { recursive: true, force: true }) } catch { /* ignore */ } }
export { readFileSync, readdirSync, existsSync }
