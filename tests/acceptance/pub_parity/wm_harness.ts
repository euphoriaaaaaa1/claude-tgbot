// pub-parity · WorkerManager 拉起观察夹具（非测试文件，被 *.test.ts 引用）。
// 零副作用：HOME/CHANNEL_DIR/引擎目录全在临时目录，令牌为假值，PATH 前置拒绝桩（claude/tmux/... 一律 exit 97），不联网。
// 观察点（接口文件「行为清单·worker」）：经 spawn 捕获的参数与子进程环境、临时 HOME 下的 flag 与共享目录、拉起日志；
// 假句柄可喂 init 事件、发 exit。
// 接缝假设（接口文件只写了「deps.spawn」，没有写构造/拉起的调用形态，这里是唯一需要与实现对齐的地方）：
//   S1 new WorkerManager({ spawn }) 接受含 spawn 的依赖对象（同时用 mock.module 兜住 node:child_process 的 spawn）
//   S2 await manager.spawnWorker() 执行一次完整拉起（既有测试里被桩掉的就是这个方法）
//   S3 spawn 以 spawn(标签字符串, { file, args, opts:{ env, cwd, ... } }) 调用（运行时实测的调用形态）；返回 Bun Subprocess 形态（pid、stdin.write、stdout 流、exited）
//   S4 拉起日志经 console.error / process.stderr 输出
import { mock } from 'bun:test'
import * as realCp from 'node:child_process'
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

export const FAKE_BOT_TOKEN = 'FAKE-TELEGRAM-TOKEN-DO-NOT-LEAK'
export const FAKE_AUTH = 'FAKE-ANTHROPIC-AUTH-DO-NOT-LEAK'
export const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

export class FakeChild {
  pid: number
  exitCode: number | null = null; signalCode: string | null = null; killed = false
  stdin: any; stdout: ReadableStream<Uint8Array>; stderr: ReadableStream<Uint8Array>
  exited: Promise<number>; written: string[] = []
  private outCtl!: ReadableStreamDefaultController<Uint8Array>
  private errCtl!: ReadableStreamDefaultController<Uint8Array>
  private resolveExit!: (c: number) => void
  constructor(pid: number, private onExitCb?: (...a: any[]) => void) {
    this.pid = pid
    this.stdout = new ReadableStream({ start: c => { this.outCtl = c } })
    this.stderr = new ReadableStream({ start: c => { this.errCtl = c } })
    this.stdin = {
      write: (d: any) => { this.written.push(typeof d === 'string' ? d : new TextDecoder().decode(d)); return typeof d === 'string' ? d.length : d.byteLength },
      flush: () => 0, end: () => 0, ref() {}, unref() {},
    }
    this.exited = new Promise(r => { this.resolveExit = r })
  }
  kill(_sig?: unknown) { this.killed = true; setTimeout(() => this.exit(143, 'SIGTERM'), 5) }
  ref() {} unref() {}
  /** 喂一行 stream-json 事件（如 init）。 */
  emitLine(obj: unknown) { this.outCtl.enqueue(new TextEncoder().encode(JSON.stringify(obj) + '\n')) }
  exit(code: number | null, signal: string | null = null) {
    if (this.exitCode !== null) return
    this.exitCode = code ?? -1; this.signalCode = signal
    try { this.outCtl.close() } catch { /* ignore */ }
    try { this.errCtl.close() } catch { /* ignore */ }
    this.resolveExit(this.exitCode)
    this.onExitCb?.(this, this.exitCode, signal, undefined)
  }
}

export type Call = { file: string; args: string[]; env: Record<string, string>; cwd?: string; child: FakeChild }

export async function boot() {
  const base = mkdtempSync(join(tmpdir(), 'pp-wm-'))
  const home = join(base, 'home'), channelRoot = join(base, 'channels'), botDir = join(channelRoot, 'bot1')
  const engineDir = join(base, 'engine', 'novelai-skill'), stub = join(base, 'stub')
  for (const d of [home, botDir, join(botDir, '.claude'), engineDir, stub]) mkdirSync(d, { recursive: true })
  for (const n of ['claude', 'tmux', 'launchctl', 'pkill', 'schtasks']) {
    writeFileSync(join(stub, n), `#!/bin/sh\necho "test_mode: real ${n} called" >&2\nexit 97\n`); chmodSync(join(stub, n), 0o755)
  }
  Object.assign(process.env, {
    HOME: home, USERPROFILE: home, CHANNEL_DIR: botDir, BOT_NAME: 'bot1', TELEGRAM_WORKER_BOT: 'bot1', TELEGRAM_WORKER_CHAT_ID: '100001',
    TELEGRAM_DISPATCHER_URL: 'http://127.0.0.1:9', DISPATCHER_URL: 'http://127.0.0.1:9', TELEGRAM_BOT_TOKEN: FAKE_BOT_TOKEN,
    ANTHROPIC_BASE_URL: 'http://127.0.0.1:9', ANTHROPIC_AUTH_TOKEN: FAKE_AUTH, NOVELAI_SKILL_ROOT: engineDir,
    BOT_NAMESPACE: '550e8400-e29b-41d4-a716-446655440001',   // 构造器报错要求：UUIDv5 命名空间（测试用假值）
    CLAUDEBOTLIFE_TEST: '1', CLAUDEBOTLIFE_TEST_ROOT: base, PATH: `${stub}:${process.env.PATH ?? ''}`,
  })
  const skill = join(engineDir, 'SKILL.md')
  const marker = (dir: string) => join(dir, '.worker-isolation-off')
  const flag = join(botDir, '.worker-settings.json')
  const shared = join(home, '.claude', 'worker-shared')
  const link = join(shared, '.claude', 'skills', 'novelai-skill')

  const logs: string[] = []
  const grab = (c: unknown) => { logs.push(typeof c === 'string' ? c : String(c)); return true }
  ;(process.stderr as any).write = grab
  for (const k of ['error', 'warn', 'log'] as const) (console as any)[k] = (...a: unknown[]) => { logs.push(a.map(String).join(' ') + '\n') }

  const calls: Call[] = []
  let pidSeq = 2_000_000_000                      // 远大于系统 pid 上限：任何误发信号都只会 ESRCH
  const rawCalls: unknown[][] = []                 // 诊断用：spawn 被调用时的原始参数
  const fakeSpawn = (...all: any[]) => {
    rawCalls.push(all)
    const [a, b, c] = all
    // 实测调用形态：spawn(标签字符串, { file, args, opts })；另兼容 Bun 风格 spawn(命令数组, 选项) 与 Node 风格 spawn(file, args, 选项)
    let file: string, args: string[], opts: any
    if (typeof a === 'string' && b && typeof b === 'object' && 'file' in b) { file = String(b.file); args = b.args ?? []; opts = b.opts ?? {} }
    else if (Array.isArray(a)) { file = String(a[0]); args = a.slice(1); opts = b ?? {} }
    else { file = String(a); args = Array.isArray(b) ? b : []; opts = (Array.isArray(b) ? c : b) ?? {} }
    const child = new FakeChild(pidSeq++, opts.onExit)
    calls.push({ file, args: args.map(String), env: { ...(opts.env ?? {}) }, cwd: opts.cwd, child })
    return child
  }
  const cpMock = () => ({ ...realCp, spawn: fakeSpawn, default: { ...realCp, spawn: fakeSpawn } })
  mock.module('node:child_process', cpMock); mock.module('child_process', cpMock)

  const mod: any = await import('../../../dispatcher/worker-manager')
  const WorkerManager = mod.WorkerManager

  const h = {
    base, home, channelRoot, botDir, engineDir, flag, shared, link, logs, calls, rawCalls, sleep,
    newManager(): any {
      try { return new WorkerManager({ spawn: fakeSpawn }) } catch (e: any) { throw new Error('无法构造 WorkerManager: ' + String(e?.message ?? e)) }
    },
    async launch(m: any): Promise<Call> {
      const before = calls.length
      if (typeof m.spawnWorker !== 'function') throw new Error('WorkerManager 没有 spawnWorker 方法')
      try { await m.spawnWorker() } catch (e: any) { throw new Error('spawnWorker 抛错: ' + String(e?.message ?? e)) }
      if (calls.length !== before + 1) throw new Error(`一次拉起应恰好捕获 1 次 spawn，实际 ${calls.length - before} 次（接缝与实现不一致？）`)
      return calls[calls.length - 1]
    },
    /** 开关关着拉起一次，得到"改动前"的参数与环境，随后撤掉开关。 */
    async legacy(): Promise<Call> {
      writeFileSync(marker(botDir), '')
      try { return await h.launch(h.newManager()) } finally { rmSync(marker(botDir), { force: true }) }
    },
    reset() {
      try { chmodSync(botDir, 0o755) } catch { /* ignore */ }
      for (const f of [marker(botDir), marker(channelRoot), flag, join(botDir, '.claude', 'settings.json'), join(botDir, '.claude', 'settings.local.json'),
        join(home, '.claude', 'settings.json'), join(home, '.claude', 'settings.local.json')]) rmSync(f, { force: true })
      rmSync(shared, { recursive: true, force: true })
      mkdirSync(engineDir, { recursive: true }); writeFileSync(skill, '# engine\n')
    },
    setUser(obj: unknown, file = 'settings.json') { mkdirSync(join(home, '.claude'), { recursive: true }); writeFileSync(join(home, '.claude', file), typeof obj === 'string' ? obj : JSON.stringify(obj)) },
    setBot(main?: unknown, local?: unknown) {
      if (main !== undefined) writeFileSync(join(botDir, '.claude', 'settings.json'), JSON.stringify(main))
      if (local !== undefined) writeFileSync(join(botDir, '.claude', 'settings.local.json'), JSON.stringify(local))
    },
    removeEngine() { rmSync(skill, { force: true }) },
    markSwitch(where: 'bot' | 'root') { writeFileSync(marker(where === 'bot' ? botDir : channelRoot), '') },
    /** 自 logs[from] 起所有含 WORKER-ISOLATION 的行（去掉时间/bot 名前缀）。 */
    isoLines(from = 0): string[] {
      return logs.slice(from).join('').split('\n').map(l => { const i = l.indexOf('WORKER-ISOLATION'); return i >= 0 ? l.slice(i).trimEnd() : '' }).filter(Boolean)
    },
    argAfter: (args: string[], k: string) => args[args.indexOf(k) + 1],
    /** 比较参数时把 --session-id/--resume 及其值归一：同一 manager 二次拉起可能改用 --resume，与隔离无关。 */
    normArgs: (args: string[]) => args.map((a, i) => (args[i - 1] === '--session-id' || args[i - 1] === '--resume') ? 'SID' : (a === '--resume' ? '--session-id' : a)),
    exists: existsSync,
  }
  return h
}
