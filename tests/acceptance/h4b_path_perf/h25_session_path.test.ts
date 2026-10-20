// H4b · H-25 会话文件路径与会话身份核对（黑盒验收，bug 修复模式：现在应为红）
//
// 只依据 .devflow/{BRIEF,INTERFACE}-h4b-path-perf.md。不读实现。
// 契约入口名 sessionJsonlPath 由 INTERFACE 明确给出；身份核对入口未给名，用别名回退取值。
// 全程临时目录 + 假 HOME/CHANNEL_DIR，绝不读真实会话文件。
//
// 跑法：bun test tests/acceptance/h4b_path_perf/h25_session_path.test.ts
import { test, expect, beforeEach, afterEach } from 'bun:test'
import { existsSync, mkdirSync, realpathSync, symlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  H4B_HOME, mkTmp, rmTmp, captureStderr, releaseStderr, eventText, countEvent,
  pickOrThrow, workerManager,
} from './_fixture.ts'

const DIRS: string[] = []
afterEach(() => {
  for (const d of DIRS.splice(0)) rmTmp(d)
  releaseStderr()
  delete process.env.CLAUDE_CONFIG_DIR
  delete process.env.SESSION_IDENTITY_CHECK
})
beforeEach(() => captureStderr())

function tmp(prefix = 'h25-'): string { const d = mkTmp(prefix); DIRS.push(d); return d }

// 契约入口：sessionJsonlPath（名由 INTERFACE 给死）。projectSlug 为本仓既有真实导出。
// 惰性取值：缺失时让「单个用例」报明确的契约缺失错，而不是整个文件在收集期崩掉。
const sessionJsonlPath = (): ((d: string, id: string, o?: unknown) => string) =>
  pickOrThrow(workerManager, ['sessionJsonlPath'])
const projectSlug = (): ((d: string) => string) => pickOrThrow(workerManager, ['projectSlug'])

// 身份核对入口：契约未固定函数名，按多别名回退（缺失时给明确的「契约入口缺失」错）。
const identityFn = (): ((...a: unknown[]) => unknown) =>
  pickOrThrow(workerManager, ['checkSessionIdentity', 'verifySessionIdentity', 'sessionIdentityCheck',
    'sessionIdentityDecision', 'resolveSessionIdentity'])

// 返回形状归一：容忍 {deliver} 布尔或直接布尔；status 同理容忍 {status} / 字符串。
const deliverOf = (r: unknown): boolean | undefined => {
  if (typeof r === 'boolean') return r
  if (r && typeof r === 'object') {
    const o = r as Record<string, unknown>
    for (const k of ['deliver', 'deliverable', 'ok', 'allowed']) if (typeof o[k] === 'boolean') return o[k] as boolean
  }
  return undefined
}
const statusOf = (r: unknown): string | undefined => {
  if (typeof r === 'string') return r
  if (r && typeof r === 'object') {
    const o = r as Record<string, unknown>
    for (const k of ['status', 'state', 'decision']) if (typeof o[k] === 'string') return o[k] as string
  }
  return undefined
}

// ═══ §一.1–2：路径推导 ══════════════════════════════════════════════════════
// （普通目录的完整路径由下方 symlink 用例断言，此处只补 CLAUDE_CONFIG_DIR 影响）

test('H25_路径_频道目录是符号链接_用真实路径编码而非链接名', () => {
  const target = tmp('h25-real-')
  const link = join(tmp('h25-link-'), 'chan-link')
  symlinkSync(target, link)
  const id = 'dddd-eeee'
  const got = sessionJsonlPath()(link, id)
  const realSlug = projectSlug()(realpathSync(target))
  expect(got).toBe(join(H4B_HOME, '.claude', 'projects', realSlug, `${id}.jsonl`))
  // 反向：不得用链接自身字符串编码（链接名与目标名不同 slug）
  expect(got).not.toBe(join(H4B_HOME, '.claude', 'projects', projectSlug()(link), `${id}.jsonl`))
})

test('H25_路径_设了CLAUDE_CONFIG_DIR_前缀随之改变', () => {
  const cfg = realpathSync(tmp('h25-cfg-'))          // 取真身，规避 /var→/private 符号链接差异
  process.env.CLAUDE_CONFIG_DIR = cfg
  const dir = tmp()
  const got = sessionJsonlPath()(dir, 'ffff')
  expect(got.startsWith(join(cfg, 'projects'))).toBe(true)
  expect(got.startsWith(join(H4B_HOME, '.claude'))).toBe(false)
})

// ═══ §一.3–4：身份核对三态 + 自愈 + 覆盖开关 ═════════════════════════════════

test('H25_身份_pending(CLI未回报)_不阻断照常投递', () => {
  const fn = identityFn()
  expect(typeof fn).toBe('function')
  const r = fn!('id-1', null, { log: () => {} })
  expect(statusOf(r) ?? 'pending').toBe('pending')
  expect(deliverOf(r) ?? true).toBe(true)          // 返回 undefined 也视为不阻断
})

test('H25_身份_match(id相同)_照常投递', () => {
  const fn = identityFn()
  expect(typeof fn).toBe('function')
  const r = fn!('id-1', 'id-1', { log: () => {} })
  expect((statusOf(r) ?? '').toLowerCase()).toContain('match')
  expect(deliverOf(r) ?? true).toBe(true)
  expect(countEvent('wm_session_mismatch')).toBe(0)
})

test('H25_身份_mismatch_只跳过本次且下轮可再试(非永久停)', () => {
  const fn = identityFn()
  expect(typeof fn).toBe('function')
  const logs: string[] = []
  const projectsDir = tmp('h25-proj-')                    // 空目录：无对应 jsonl → 不能自愈
  const r1 = fn!('id-A', 'id-B', { projectsDir, log: (l: string) => logs.push(l) })
  expect(deliverOf(r1)).toBe(false)                        // 本条不投递
  expect(logs.some(l => /wm_session_mismatch\s+expected_id=id-A\s+got_id=id-B/.test(l))).toBe(true)
  // 下一轮重新核对：仍给判定（不是被永久拉黑/停摆），且没有「永久停」类事件
  const r2 = fn!('id-A', 'id-B', { projectsDir, log: () => {} })
  expect(deliverOf(r2)).toBe(false)
  expect(countEvent('wm_session_stopped') + countEvent('wm_session_permanent')).toBe(0)
})

test('H25_身份_能自愈_CLI回报的id在projects下找得到文件_采用CLI的id', () => {
  const fn = identityFn()
  expect(typeof fn).toBe('function')
  const projectsDir = tmp('h25-proj-')
  mkdirSync(projectsDir, { recursive: true })
  writeFileSync(join(projectsDir, 'id-CLI.jsonl'), '')
  const logs: string[] = []
  const r = fn!('id-EXPECT', 'id-CLI', { projectsDir, log: (l: string) => logs.push(l) })
  expect(countEvent('wm_session_mismatch')).toBe(0)        // 自愈成功不报 mismatch
  expect(deliverOf(r) ?? true).toBe(true)
  const adopted = (r && typeof r === 'object')
    ? ((r as Record<string, unknown>).adoptedId ?? (r as Record<string, unknown>).useId ?? (r as Record<string, unknown>).id)
    : undefined
  if (adopted !== undefined) expect(String(adopted)).toBe('id-CLI')
})

test('H25_身份_SESSION_IDENTITY_CHECK=off_完全跳过核对只记一行', () => {
  const fn = identityFn()
  expect(typeof fn).toBe('function')
  process.env.SESSION_IDENTITY_CHECK = 'off'
  const logs: string[] = []
  const r = fn!('id-A', 'id-B', { projectsDir: tmp('h25-proj-'), log: (l: string) => logs.push(l) })
  expect(deliverOf(r) ?? true).toBe(true)                  // 即便 id 不符也照常投
  expect(logs.some(l => l.includes('wm_session_identity_check_off'))).toBe(true)
  expect(logs.some(l => l.includes('wm_session_mismatch'))).toBe(false)
})

test('H25_身份_同类相比_id对目录不得当match', () => {
  const fn = identityFn()
  expect(typeof fn).toBe('function')
  const projectsDir = tmp('h25-proj-')
  // CLI 回报的是目录路径，期望的是 id：不得因"路径尾==id"就当匹配
  const r = fn!('id-1', '/some/dir/id-1', { projectsDir, log: () => {} })
  expect((statusOf(r) ?? '').toLowerCase()).not.toBe('match')
  expect(deliverOf(r)).toBe(false)
})

// __END__
void existsSync
