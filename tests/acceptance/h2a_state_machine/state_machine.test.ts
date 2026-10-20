// H2a 状态机与身份校验 —— 黑盒验收（bug 修复模式：这些用例当前应为红，修好后转绿）
// 只依据 .devflow/{BRIEF,INTERFACE}-h2a-state-machine.md。不读实现。
// 全程假 spawn/kill/now/procInfo/timer，绝不真起/真杀进程、不碰真实通道目录。
// 跑法：bun test tests/acceptance/h2a_state_machine/
import { test, expect, beforeEach, afterEach } from 'bun:test'
import { newHarness, captureStderr, releaseStderr, eventText, shouldKillOrphan } from './_fixture.ts'
import { rmSync } from 'node:fs'
import { join } from 'node:path'

// 事件断言小工具：事件行都写 stderr，用子串/正则定位（兼容可能的行前缀）
const ev = () => eventText()
const hasEvent = (s: string) => ev().includes(s)
const countRe = (re: RegExp) => (ev().match(re) ?? []).length

const HARNS: ReturnType<typeof newHarness>[] = []
beforeEach(() => captureStderr())
afterEach(() => {
  releaseStderr()
  for (const h of HARNS.splice(0)) { try { rmSync(h.channelDir, { recursive: true, force: true }) } catch { /* ignore */ } }
})
function H(opts: Parameters<typeof newHarness>[0] = {}) { const h = newHarness(opts); HARNS.push(h); return h }

// ═══ 状态机契约 §二.1–3：ensure 语义 ═══════════════════════════════════════

test('ensure_从stopped_先同步置starting再spawn_成功后running', async () => {
  let phaseAtSpawn: string | undefined
  const h = H({ spawnImpl: () => { phaseAtSpawn = h.lv(); return undefined } })
  await h.m.ensure()
  expect(h.spawnCalls.length).toBe(1)              // 只起一次
  expect(phaseAtSpawn).toBe('starting')            // spawn 发生那一刻已是 starting
  expect(h.lv()).toBe('running')                   // 成功 → running
  expect(hasEvent('wm_spawn pid=')).toBe(true)
})

// 契约 §二.1 抛错路径 + §四.9 前置段异常 + §二.4 退避回调(stopped)三合一：
// 起进程这一步抛错 → 复位 stopped、留 wm_start_failed、排退避；退避回调在 stopped 走 ensure 再试。
test('ensure_spawn抛错_wm_start_failed_复位stopped_退避回调再试spawn', async () => {
  let failed = false
  const h = H({ spawnImpl: () => { if (!failed) { failed = true; throw new Error('boom') } return undefined } })
  await h.m.ensure()
  expect(h.lv()).toBe('stopped')                   // 抛错 → 回 stopped，不卡 starting
  expect(hasEvent('wm_start_failed step=')).toBe(true)  // 留痕带步骤名
  const after1 = h.spawnCalls.length
  h.flushBackoff()                                 // 触发退避回调（stopped 时）
  await h.settle()
  expect(h.spawnCalls.length).toBeGreaterThan(after1)  // 退避确实重排了一次 spawn
})

test('并发两次ensure_同一tick_只spawn一次_第二次reason=starting', async () => {
  const h = H()
  const p1 = h.m.ensure()
  const p2 = h.m.ensure()                          // 同一 tick 内第二次
  await Promise.all([p1, p2])
  expect(h.spawnCalls.length).toBe(1)              // 只出现一次 wm_spawn
  expect(countRe(/wm_spawn pid=/g)).toBe(1)
  expect(hasEvent('wm_spawn_skipped reason=starting')).toBe(true)
})

test('running时ensure_skipped_running_不杀不重启不改phase', async () => {
  const h = H()
  await h.m.ensure()
  const spawnN = h.spawnCalls.length
  await h.m.ensure()
  expect(h.lv()).toBe('running')
  expect(h.spawnCalls.length).toBe(spawnN)         // 没有第二个进程
  expect(h.killCalls.length).toBe(0)               // 没有杀
  expect(hasEvent('wm_spawn_skipped reason=running')).toBe(true)
})

// ═══ 状态机契约 §二.4：退避回调在三种 phase 下的行为 ═══════════════════════

test('退避回调_running_时_skipped_不杀正在干活的新worker', async () => {
  // 复现 bug#2：退避定时器到点时 worker 已被别的路径拉起
  let n = 0
  const h = H({ spawnImpl: () => { if (++n === 1) throw new Error('boom'); return undefined } })
  await h.m.ensure()                               // 失败 → 排退避
  await h.m.ensure()                               // 另一次调用把 worker 拉起 → running
  expect(h.lv()).toBe('running')
  const spawnN = h.spawnCalls.length
  h.flushBackoff()                                 // 陈旧退避到点
  await h.settle()
  expect(h.lv()).toBe('running')                   // 新 worker 没被当孤儿
  expect(h.spawnCalls.length).toBe(spawnN)
  expect(h.killCalls.length).toBe(0)
  expect(hasEvent('wm_spawn_skipped reason=running')).toBe(true)
})

// ── stop 在 starting 期间：先等 spawn 落地再停 ────────────────────────────
test('退避回调_stopping_时_wm_restart_pending_exit后自愈ensure', async () => {
  // 复现 bug#3：主动停的过程中退避到点 → 置 pending，等 exit 后再拉起
  let n = 0
  const h = H({ spawnImpl: () => { if (++n === 1) throw new Error('boom'); return undefined } })
  await h.m.ensure()                               // 失败 → 排退避
  await h.m.ensure()                               // 拉起 → running
  const pid = h.handles[h.handles.length - 1]?.pid ?? -1
  const sp = h.m.stop()                            // 进入 stopping（尚未收到 exit）
  await h.settle()
  expect(h.lv()).toBe('stopping')
  h.flushBackoff()                                 // 退避到点时正处于 stopping
  await h.settle()
  expect(hasEvent('wm_restart_pending')).toBe(true)   // 置标志，不乱起新进程
  const spawnN = h.spawnCalls.length
  h.emitExit(pid)                                  // exit 到达 → 自愈
  await sp
  await h.settle()
  await h.settle()
  expect(h.lv()).toBe('running')                   // exit 后自动 ensure 重新拉起
  expect(h.spawnCalls.length).toBeGreaterThan(spawnN)
})

// ═══ 状态机契约 §二.5–8：stop / grace / timeout / 迟到 exit / pid 文件 ══════

test('stop_在running_发SIGTERM_收到exit后stopped', async () => {
  const h = H()
  await h.m.ensure()
  const pid = h.handles[0]?.pid ?? -1
  const sp = h.m.stop()
  await h.settle()
  expect(hasEvent(`wm_stop pid=${pid} force=false`)).toBe(true)
  h.emitExit(pid)
  await sp
  await h.settle()
  expect(h.lv()).toBe('stopped')                   // stop() 返回即 stopped
})

test('stop_在starting_先等spawn落地再杀_不在落地前杀', async () => {
  const h = H()
  const e = h.m.ensure()                            // 同步进入 starting
  const sp = h.m.stop()                             // starting 期间停
  await Promise.all([e.catch(() => {}), sp])
  await h.settle()
  expect(h.handles.length).toBeGreaterThan(0)       // spawn 已落地
  const pid = h.handles[0]?.pid ?? -1
  expect(h.killCalls.some(k => k.pid === pid)).toBe(true)   // 杀的是落地后的 pid
})

test('stop_SIGTERM后grace内无exit_补SIGKILL(force=true)', async () => {
  const h = H()
  await h.m.ensure()
  const pid = h.handles[0]?.pid ?? -1
  h.m.stop()
  await h.settle()
  expect(h.killCalls.filter(k => !k.force).length).toBe(1)  // 先 SIGTERM
  h.advance(5000)                                   // 越过 STOP_GRACE_MS
  await h.settle()
  expect(h.killCalls.some(k => k.pid === pid && k.force)).toBe(true)  // 补 SIGKILL
})

test('stop_SIGTERM后STOP_TIMEOUT内仍无exit_强制stopped_且wm_stop_timeout留痕', async () => {
  const h = H()
  await h.m.ensure()
  const pid = h.handles[0]?.pid ?? -1
  const sp = h.m.stop()
  h.advance(15000)                                  // 越过 STOP_TIMEOUT_MS
  await sp
  await h.settle()
  expect(h.lv()).toBe('stopped')
  expect(hasEvent('wm_stop_timeout')).toBe(true)
})

test('超时强制停后_迟到的真exit被吞_不改phase_不再触发退避', async () => {
  const h = H()
  await h.m.ensure()
  const pid = h.handles[0]?.pid ?? -1
  const sp = h.m.stop()
  h.advance(15000)
  await sp
  await h.settle()
  const exitsBefore = countRe(/wm_exit pid=/g)
  const spawnBefore = h.spawnCalls.length
  h.emitExit(pid)                                   // 迟到的真 exit
  await h.settle()
  expect(h.lv()).toBe('stopped')                    // phase 不动
  expect(countRe(/wm_exit pid=/g)).toBe(exitsBefore) // 不再记一次 exit
  h.flushBackoff()
  await h.settle()
  expect(h.spawnCalls.length).toBe(spawnBefore)      // 不触发退避重启
})

test('exit事件对同一实例只处理一次_wm_exit只出现一次', async () => {
  const h = H()
  await h.m.ensure()
  const pid = h.handles[0]?.pid ?? -1
  h.emitExit(pid)
  await h.settle()
  h.emitExit(pid)                                   // 第二次同实例 exit
  await h.settle()
  expect(countRe(/wm_exit pid=\d+/g)).toBe(1)
})

test('stopping期间ensure_不产生wm_spawn', async () => {
  const h = H()
  await h.m.ensure()
  const pid = h.handles[0]?.pid ?? -1
  h.m.stop()                                        // 进入 stopping
  await h.settle()
  const spawnN = h.spawnCalls.length
  await h.m.ensure().catch(() => {})                // stopping 期间 ensure
  await h.settle()
  expect(h.spawnCalls.length).toBe(spawnN)          // 没有新进程
  h.emitExit(pid)
  await h.settle()
})

test('pid文件_运行中存在_stop确认退出后删除_强制超时路径也删除', async () => {
  // 正常退出路径
  const h1 = H()
  await h1.m.ensure()
  expect(h1.readPidFile()).not.toBeNull()           // spawn 后写入
  const pid1 = h1.handles[0].pid
  const sp1 = h1.m.stop()
  h1.emitExit(pid1)
  await sp1
  await h1.settle()
  expect(h1.readPidFile()).toBeNull()               // 确认退出后才删

  // 强制超时路径
  const h2 = H()
  await h2.m.ensure()
  const sp2 = h2.m.stop()
  h2.advance(15000)
  await sp2
  await h2.settle()
  expect(h2.readPidFile()).toBeNull()               // 超时路径同样清掉
})

// ═══ §三 pid 文件与身份校验：shouldKillOrphan（H-03）═══════════════════════
const UUID_A = '11111111-2222-3333-4444-555555555555'
const UUID_B = '99999999-8888-7777-6666-555555555555'
const rec = (pid: number, uuid: string, startedAtMs: number) => ({ pid, sessionUuid: uuid, startedAtMs })
const info = (uuid: string, startedAtMs: number) => ({ args: ['/bin/claude', '--resume', uuid], startedAtMs })

test('shouldKillOrphan_四条件全真_kill_true_且wm_orphan_kill', () => {
  const recorded = rec(4321, UUID_A, 1_000_000)
  const r = shouldKillOrphan(4321, recorded, UUID_A, info(UUID_A, 1_000_000))
  expect(r.kill).toBe(true)
  expect(hasEvent('wm_orphan_kill pid=4321')).toBe(true)
})

test('shouldKillOrphan_uuid不匹配_kill_false_reason=uuid_mismatch', () => {
  const r = shouldKillOrphan(4321, rec(4321, UUID_B, 1_000_000), UUID_A, info(UUID_A, 1_000_000))
  expect(r.kill).toBe(false)
  expect(r.reason).toBe('uuid_mismatch')
  expect(hasEvent('wm_orphan_skip reason=uuid_mismatch')).toBe(true)
})

test('shouldKillOrphan_命令行不含uuid_kill_false_reason=args_mismatch', () => {
  const r = shouldKillOrphan(4321, rec(4321, UUID_A, 1_000_000), UUID_A, { args: ['/bin/claude'], startedAtMs: 1_000_000 })
  expect(r.kill).toBe(false)
  expect(r.reason).toBe('args_mismatch')
  expect(hasEvent('wm_orphan_skip reason=args_mismatch')).toBe(true)
})

test('shouldKillOrphan_进程启动过新(>记录+5000ms)_kill_false_reason=too_new', () => {
  const r = shouldKillOrphan(4321, rec(4321, UUID_A, 1_000_000), UUID_A, info(UUID_A, 1_000_000 + 6000))
  expect(r.kill).toBe(false)
  expect(r.reason).toBe('too_new')
  expect(hasEvent('wm_orphan_skip reason=too_new')).toBe(true)
})

test('shouldKillOrphan_旧格式记录(无sessionUuid)_kill_false_reason=legacy', () => {
  const r = shouldKillOrphan(4321, { pid: 4321, startedAtMs: 1_000_000 } as any, UUID_A, info(UUID_A, 1_000_000))
  expect(r.kill).toBe(false)
  expect(r.reason).toBe('legacy')
  expect(hasEvent('wm_orphan_skip reason=legacy')).toBe(true)
})

test('shouldKillOrphan_取不到进程信息(null)_kill_false_reason=no_proc_info', () => {
  const r = shouldKillOrphan(4321, rec(4321, UUID_A, 1_000_000), UUID_A, null)
  expect(r.kill).toBe(false)
  expect(r.reason).toBe('no_proc_info')
  expect(hasEvent('wm_orphan_skip reason=no_proc_info')).toBe(true)
})

test('shouldKillOrphan_pid非法(<=0)_一律不杀', () => {
  const r = shouldKillOrphan(0, rec(0, UUID_A, 1_000_000), UUID_A, info(UUID_A, 1_000_000))
  expect(r.kill).toBe(false)
  expect(typeof r.reason).toBe('string')            // 具体 reason 契约未列举
  expect(r.kill).toBe(false)
})

// ═══ §四 启动前置段（H-09）：spawn 步抛错已在上文合并覆盖 ══════════════════

test('会话回写失败_wm_session_write_failed_且继续启动', async () => {
  const h = H({ writeSession: () => { throw new Error('session write boom') } })
  await h.m.ensure()
  await h.settle()
  expect(hasEvent('wm_session_write_failed')).toBe(true)
  expect(h.spawnCalls.length).toBeGreaterThan(0)          // 不因回写失败而中止
  expect(['running', 'ready']).toContain(h.lv())          // 仍在往前跑，没落到 stopped
})

// __END__
