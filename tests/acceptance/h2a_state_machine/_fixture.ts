// H2a 状态机与身份校验 —— 黑盒验收夹具（自带）
//
// 只依据 .devflow/BRIEF-h2a-state-machine.md 与 INTERFACE-h2a-state-machine.md 的对外契约。
// 不读实现、不真起进程、不真杀进程、不碰真实通道目录：spawn / kill / now / procInfo / timer
// 全部注入假件，退避与 grace/timeout 全由假时钟驱动。
//
// 注入面（INTERFACE 一）：构造参数或模块级可替换依赖需可注入
//   spawn(cmd)->{pid}  kill(pid,force)  now()  procInfo(pid)->{args,startedAtMs}|null  timer
// 导出面：phase() / ensure() / stop(force?) / shouldKillOrphan(pid,recorded,uuid,procInfo)
//
// 说明：契约未固定"exit 事件"如何送达。本夹具按 Node ChildProcess 惯例（spawn 返回带
// on/once/emit('exit') 的句柄）驱动；若实现在 WorkerManager 上暴露 onExit/handleExit，夹具
// 自动回退过去。见 _fixture.md 顶部注释。
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync, chmodSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// 模块级 CHANNEL_DIR 在首次 import worker-manager 时定死 → 先设 env 再动态 import。
const BASE = mkdtempSync(join(tmpdir(), 'h2a-ch-'))
process.env.CHANNEL_DIR = BASE
process.env.BOT_NAME ??= 'h2a-bot'
process.env.BOT_NAMESPACE ??= '6ba7b810-9dad-11d1-80b4-00c04fd430c8'  // 构造 WorkerManager 时用于派生会话 uuid

// ── fail-closed 隔离（与 dispatcher/tests/acceptance/desync/_env.ts 同源策略）──
// 目的：即便被测实现无视注入、真去 spawn/kill，也只会命中假桩，绝不触及真实进程。
const HOME = join(BASE, 'home')
const STUB = join(BASE, 'stub')
mkdirSync(HOME, { recursive: true }); mkdirSync(STUB, { recursive: true })
for (const n of ['claude', 'tmux', 'launchctl', 'pkill', 'schtasks', 'kill']) {
  const p = join(STUB, n)
  writeFileSync(p, `#!/bin/sh\necho "$0 $@" >> "${BASE}/calls.log"\necho "test_mode: real ${n} called" >&2\nexit 97\n`)
  chmodSync(p, 0o755)
}
process.env.CLAUDEBOTLIFE_TEST = '1'
process.env.CLAUDEBOTLIFE_TEST_ROOT = BASE
process.env.HOME = HOME
process.env.PATH = `${STUB}:${process.env.PATH ?? ''}`

const mod: any = await import('../../../dispatcher/worker-manager')
export const WorkerManager: any =
  mod.WorkerManager ?? mod.Worker ?? mod.WorkerStateMachine ?? mod.default
export const shouldKillOrphan: any =
  mod.shouldKillOrphan ?? mod.shouldKillOrphanProcess

export const CHANNEL_BASE = BASE
export { rmSync }

// ─── 假时钟：退避回调 + grace/timeout 由 advance(ms) 手动触发 ────────────────
export interface FakeClock {
  timer: { setTimeout: (fn: () => void, ms?: number) => number; clearTimeout: (id: number) => void; setInterval: (fn: () => void, ms?: number) => number; clearInterval: (id: number) => void }
  now: () => number
  advance: (ms: number) => void
  pending: () => number
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
  return { timer, now: () => now, advance, pending: () => timers.length }
}

// ─── 假进程句柄（ChildProcess 精简版，仅测试所需）────────────────────────────
export interface FakeHandle {
  pid: number
  on(ev: string, cb: (...a: any[]) => void): FakeHandle
  once(ev: string, cb: (...a: any[]) => void): FakeHandle
  emit(ev: string, ...a: any[]): void
  listenerCount(ev: string): number
}
export function makeHandle(pid: number): FakeHandle {
  const ls: Record<string, ((...a: any[]) => void)[]> = {}
  const h: any = {
    pid,
    on(ev, cb) { (ls[ev] ||= []).push(cb); return h },
    once(ev, cb) { const w = (...a: any[]) => { h.off(ev, w); cb(...a) }; return h.on(ev, w) },
    off(ev, cb) { const arr = ls[ev]; if (arr) { const i = arr.indexOf(cb); if (i >= 0) arr.splice(i, 1) } return h },
    emit(ev, ...a) { for (const cb of [...(ls[ev] || [])]) cb(...a) },
    listenerCount(ev) { return (ls[ev] || []).length },
  }
  return h as FakeHandle
}

// ─── stderr 事件捕获（契约：事件行都写 stderr）───────────────────────────────
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
export function countEvents(needle: string): number {
  return eventLines().filter(l => l.includes(needle)).length
}

// ─── 夹具主入口 ──────────────────────────────────────────────────────────────
export type Deps = Record<string, unknown>
export interface Harness {
  m: any
  clock: FakeClock
  channelDir: string
  pidFile: () => string
  spawnCalls: string[]
  killCalls: { pid: number; force: boolean }[]
  handles: FakeHandle[]
  emitExit: (pid: number, code?: number) => void
  advance: (ms: number) => void
  flushBackoff: () => void
  settle: (n?: number) => Promise<void>
  readPidFile: () => any
  lv: () => string    // phase 取值（方法或字段都兼容）
}

export interface HarnessOpts {
  spawnImpl?: (cmd: string, n: number) => any   // 抛错即模拟前置/spawn 失败
  procInfoImpl?: (pid: number) => any
  nowValue?: number
  writeSession?: () => void                       // 会话回写钩子（抛错模拟回写失败）
}

// 注入面适配：契约写"构造参数或模块级可替换依赖"。优先工厂/模块级 setter，最后回退构造参数。
function buildManager(deps: Deps): any {
  if (typeof mod.setWorkerDeps === 'function') { mod.setWorkerDeps(deps); return new WorkerManager(deps) }
  if (typeof mod.createWorkerManager === 'function') return mod.createWorkerManager(deps)
  return new WorkerManager(deps)
}

export function newHarness(over: HarnessOpts = {}): Harness {
  const clock = makeClock(over.nowValue ?? 1_000_000)
  let seq = 0
  const handles: FakeHandle[] = []
  const spawnCalls: string[] = []
  const killCalls: { pid: number; force: boolean }[] = []
  const channelDir = mkdtempSync(join(tmpdir(), 'h2a-run-'))

  const deps: Deps = {
    channelDir,
    now: clock.now,
    timer: clock.timer,
    spawn: (cmd: string) => {
      spawnCalls.push(cmd)
      if (over.spawnImpl) {
        const r = over.spawnImpl(cmd, ++seq)
        if (r) return r
      }
      const h = makeHandle(4000 + handles.length)
      handles.push(h)
      return h
    },
    kill: (pid: number, force: boolean) => { killCalls.push({ pid, force: !!force }); return true },
    procInfo: (pid: number) => (over.procInfoImpl ? over.procInfoImpl(pid) : null),
  }
  if (over.writeSession) deps.writeSession = over.writeSession

  const m: any = buildManager(deps)

  const lv = () => (typeof m.phase === 'function' ? m.phase() : m.phase)
  const pidFile = () => join(channelDir, '.worker.pid')
  const readPidFile = () => { try { return JSON.parse(readFileSync(pidFile(), 'utf8')) } catch { return existsSync(pidFile()) ? readFileSync(pidFile(), 'utf8') : null } }

  const emitExit = (pid: number, code = 0) => {
    const h = handles.find(x => x.pid === pid)
    if (h && h.listenerCount('exit') > 0) { h.emit('exit', code); return }
    // 回退：实现在 WorkerManager 上暴露退出入口
    if (typeof m.onExit === 'function') { m.onExit(code, h ?? { pid }); return }
    if (typeof m.handleExit === 'function') { m.handleExit(pid, code); return }
    if (typeof m.exit === 'function') { m.exit(pid, code); return }
    if (h) h.emit('exit', code)
  }

  const settle = async (n = 8) => { for (let i = 0; i < n; i++) await Promise.resolve() }

  return {
    m, clock, channelDir, pidFile, spawnCalls, killCalls, handles,
    emitExit, advance: clock.advance, flushBackoff: () => clock.advance(3_600_000),
    settle, readPidFile, lv,
  }
}

// 清理辅助：删除临时通道目录里的 pid 文件（不删目录本身，pid 文件断言需要）
export function cleanPid(h: Harness) { try { rmSync(h.pidFile(), { force: true }) } catch { /* ignore */ } }
export function writePidFile(dir: string, obj: unknown) { writeFileSync(join(dir, '.worker.pid'), JSON.stringify(obj)) }
export function makeChannelDir(): string { return mkdtempSync(join(tmpdir(), 'h2a-run-')) }
