// H4b · H-26 启动前置段不再阻塞主线程（黑盒验收，bug 修复模式：现在应为红）
//
// 只依据 .devflow/{BRIEF,INTERFACE}-h4b-path-perf.md。不读实现。
// 契约点名常量 PRECHECK_SUBPROC_TIMEOUT_MS=5000、SESSION_STREAM_THRESHOLD_BYTES默认8MiB。
// cliVersion 为本仓既有真实导出（签名 (bin, viaCmd, run)，run 可注入）——§5b 要求版本缓存键
// 为「二进制 mtime + size」，故缓存必须落在 cliVersion 内：同 stat 不重跑、改 size 或 mtime 即失效。
// 全程假二进制 + 注入 run，绝不执行真实 CLI。
//
// 跑法：bun test tests/acceptance/h4b_path_perf/h26_precheck_cache.test.ts
import { test, expect, afterEach } from 'bun:test'
import { writeFileSync, utimesSync } from 'node:fs'
import { join } from 'node:path'
import { mkTmp, rmTmp, pickOrThrow, workerManager } from './_fixture.ts'

const DIRS: string[] = []
afterEach(() => { for (const d of DIRS.splice(0)) rmTmp(d) })
function tmp(): string { const d = mkTmp('h26-'); DIRS.push(d); return d }

const cliVersion = (): ((bin: string, viaCmd: boolean, run: unknown) => string) =>
  pickOrThrow(workerManager, ['cliVersion'])

// ═══ §5 / §7：契约常量 ═════════════════════════════════════════════════════

test('H26_常量_前置子进程超时5秒_流式阈值默认8MiB', () => {
  expect(pickOrThrow(workerManager, ['PRECHECK_SUBPROC_TIMEOUT_MS'])).toBe(5000)
  expect(pickOrThrow(workerManager, ['SESSION_STREAM_THRESHOLD_BYTES'])).toBe(8 * 1024 * 1024)
})

// ═══ §5b / §6：CLI 版本缓存键 = mtime + size ═══════════════════════════════

test('H26_版本缓存_同二进制stat未变_第二次不再起子进程(命中缓存)', () => {
  const bin = join(tmp(), 'claude')
  writeFileSync(bin, 'A')
  utimesSync(bin, 1_600_000_000, 1_600_000_000)
  let calls = 0
  const run = () => { calls++; return { status: 0, stdout: '9.9.9\n' } }
  const cli = cliVersion()
  expect(cli(bin, false, run)).toBe('9.9.9')
  expect(cli(bin, false, run)).toBe('9.9.9')
  expect(calls).toBe(1)                       // 未变 → 只跑一次
})

test('H26_版本缓存_改mtime或改size_缓存失效重新取值', () => {
  const bin = join(tmp(), 'claude')
  writeFileSync(bin, 'A')                     // size=1
  utimesSync(bin, 1_600_000_000, 1_600_000_000)
  let calls = 0
  const run = () => { calls++; return { status: 0, stdout: '9.9.9\n' } }
  const cli = cliVersion()
  cli(bin, false, run); expect(calls).toBe(1)

  // 只改 mtime（size 不变）→ 失效
  utimesSync(bin, 1_600_000_100, 1_600_000_100)
  cli(bin, false, run); expect(calls).toBe(2)

  // 只改 size（把 mtime 还原成上一次）→ 失效
  writeFileSync(bin, 'AB')                    // size=2
  utimesSync(bin, 1_600_000_100, 1_600_000_100)
  cli(bin, false, run); expect(calls).toBe(3)
})
