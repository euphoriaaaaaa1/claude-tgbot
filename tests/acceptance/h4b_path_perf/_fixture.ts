// H4b 路径/性能/自愈 —— 黑盒验收共享夹具（自带）
//
// 只依据 .devflow/BRIEF-h4b-path-perf.md 与 INTERFACE-h4b-path-perf.md 的对外契约。
// 不读真实会话文件、不碰真实通道目录、不执行真实 CLI、不打印密钥。
//
// fail-closed 隔离：先把 HOME / CHANNEL_DIR / PATH 指向临时目录与假桩，再动态 import 被测
// 模块（worker-manager 顶层会读 env 并可能 mkdir），确保即便实现无视注入去真起进程，也只会
// 命中假桩（exit 97）。与本仓 h2a 夹具同源策略。
//
// 单进程共享模块警告：本夹具持有的 BASE 临时目录是跨文件共享的，绝不在任何 afterAll 里删它；
// 各测试文件自己的短生命期目录用 mkTmp() 自建、afterEach 自清。
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// ─── fail-closed 隔离（只在首次 import 时执行一次）────────────────────────────
const BASE = mkdtempSync(join(tmpdir(), 'h4b-base-'))
const HOME = join(BASE, 'home')
const STUB = join(BASE, 'stub')
mkdirSync(HOME, { recursive: true })
mkdirSync(STUB, { recursive: true })
for (const n of ['claude', 'tmux', 'launchctl', 'pkill', 'schtasks', 'kill', 'security']) {
  const p = join(STUB, n)
  writeFileSync(p, `#!/bin/sh\necho "$0 $@" >> "${BASE}/calls.log"\necho "test_mode: real ${n} called" >&2\nexit 97\n`)
  chmodSync(p, 0o755)
}
process.env.CLAUDEBOTLIFE_TEST = '1'
process.env.CLAUDEBOTLIFE_TEST_ROOT = BASE
process.env.CHANNEL_DIR = join(BASE, 'channel')
process.env.BOT_NAME ??= 'h4b-bot'
process.env.HOME = HOME
process.env.PATH = `${STUB}:${process.env.PATH ?? ''}`

export const H4B_HOME = HOME
export const H4B_STUB = STUB

// ─── 临时目录：每个测试自管，绝不共享 ───────────────────────────────────────
export function mkTmp(prefix = 'h4b-'): string {
  return mkdtempSync(join(tmpdir(), prefix))
}
export function rmTmp(d: string): void {
  try { rmSync(d, { recursive: true, force: true }) } catch { /* ignore */ }
}

// ─── stderr 事件捕获（本仓事件行惯例写 stderr）──────────────────────────────
let buf: string[] = []
let origWrite: ((c: unknown) => boolean) | null = null
export function captureStderr(): void {
  buf = []
  if (!origWrite) origWrite = process.stderr.write.bind(process.stderr) as never
  ;(process.stderr as unknown as { write: (c: unknown) => boolean }).write = (c: unknown) => {
    buf.push(typeof c === 'string' ? c : String(c))
    return true
  }
}
export function releaseStderr(): void {
  if (origWrite) { (process.stderr as unknown as { write: unknown }).write = origWrite; origWrite = null }
}
export function eventText(): string { return buf.join('') }
export function eventLines(): string[] { return eventText().split('\n').map(s => s.trim()).filter(Boolean) }
export function countEvent(needle: string): number { return eventLines().filter(l => l.includes(needle)).length }

// ─── 契约名 → 实际导出名 的兼容取值（同名模块可能延后实现，缺失时给明确报错）───
export function pick<T = unknown>(mod: Record<string, unknown>, names: string[]): T | undefined {
  for (const n of names) if (mod[n] !== undefined) return mod[n] as T
  return undefined
}
export function pickOrThrow<T = unknown>(mod: Record<string, unknown>, names: string[]): T {
  const v = pick<T>(mod, names)
  if (v === undefined) throw new Error(`契约入口缺失，候选名：${names.join(' / ')}`)
  return v
}

// ─── 被测模块（动态 import，env 已就位）─────────────────────────────────────
export const workerManager: Record<string, unknown> = await import('../../../dispatcher/worker-manager.ts')
export const providerSync: Record<string, unknown> = await import('../../../dispatcher/provider_sync.ts')
export const providerWatch: Record<string, unknown> = await import('../../../dispatcher/provider_watch.ts')
export const timeAnnotate: Record<string, unknown> = await import('../../../dispatcher/time_annotate.ts')
export const inboxNames: Record<string, unknown> = await import('../../../dispatcher/inbox_names.ts')
