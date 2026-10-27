// H4b · H-21 坏件不删、先重试、超阈值移入隔离目录（黑盒验收，bug 修复模式：现在应为红）
//
// 只依据 .devflow/{BRIEF,INTERFACE}-h4b-path-perf.md。不读实现。
// 契约常量名由 INTERFACE 明确：BAD_HEAD_MAX_RETRY=3、BAD_HEAD_MAX_AGE_MS=10分钟。
// 隔离目录为 <channel_dir>/.quarantine/（必须在 inbox 扫描范围之外）。
// 判定入口名未固定，按别名回退；返回形状 {action,retryCount,quarantinePath?}。
//
// 跑法：bun test tests/acceptance/h4b_path_perf/h21_bad_head.test.ts
import { test, expect } from 'bun:test'
import { join, sep } from 'node:path'
import { mkTmp, rmTmp, pick, pickOrThrow, inboxNames } from './_fixture.ts'

// 契约常量（名由 INTERFACE 给死）
const MAX_RETRY = (): number => pickOrThrow(inboxNames, ['BAD_HEAD_MAX_RETRY'])
const MAX_AGE_MS = (): number => pickOrThrow(inboxNames, ['BAD_HEAD_MAX_AGE_MS'])

// 判定入口（名未定，别名回退）
const decide = (): ((i: Record<string, unknown>) => Record<string, unknown>) | undefined =>
  pick(inboxNames, ['decideBadHead', 'planBadHead', 'badHeadDecision', 'badHeadPlan',
    'handleBadHead', 'classifyBadHead', 'badHeadAction'])

// 归一：容忍 {action} 或直接字符串
const actionOf = (r: unknown): string | undefined => {
  if (typeof r === 'string') return r
  if (r && typeof r === 'object') {
    const o = r as Record<string, unknown>
    for (const k of ['action', 'decision', 'verdict']) if (typeof o[k] === 'string') return o[k] as string
  }
  return undefined
}

const CH = mkTmp('h21-chan-')   // 每文件自管，不跨文件共享

// ═══ 常量口径 ══════════════════════════════════════════════════════════════

test('H21_常量_重试阈值3_年龄阈值10分钟', () => {
  expect(MAX_RETRY()).toBe(3)
  expect(MAX_AGE_MS()).toBe(10 * 60_000)
})

// ═══ §12–13：阈值内保留、超阈值隔离 ════════════════════════════════════════

test('H21_坏件_重试1次且年龄尚小_保留原位不隔离', () => {
  const fn = decide()
  expect(typeof fn).toBe('function')
  const logs: string[] = []
  const r = fn!({ name: 'bad-1.json', channelDir: CH, retryCount: 1, ageMs: 1000, log: (l: string) => logs.push(l) })
  expect(actionOf(r)).toBe('keep')
  expect(logs.some(l => /wm_bad_head_retry\s+n=1/.test(l))).toBe(true)
})

test('H21_坏件_重试超过阈值_移入隔离目录且原位置不再有该件', () => {
  const fn = decide()
  expect(typeof fn).toBe('function')
  const logs: string[] = []
  const r = fn!({ name: 'bad-3.json', channelDir: CH, retryCount: MAX_RETRY() + 1, ageMs: 1000, log: (l: string) => logs.push(l) })
  expect(actionOf(r)).toBe('quarantine')
  const qp = r.quarantinePath as string | undefined
  expect(typeof qp).toBe('string')
  expect(qp!.startsWith(join(CH, '.quarantine') + sep)).toBe(true)   // 落在 <channel_dir>/.quarantine/
  expect(qp).not.toContain(`${sep}inbox${sep}`)                       // 不在 inbox 扫描范围内
  expect(logs.some(l => l.includes('wm_bad_head_quarantine') && l.includes('bad-3.json'))).toBe(true)
})

test('H21_坏件_年龄超过10分钟_即便重试次数未超也隔离', () => {
  const fn = decide()
  expect(typeof fn).toBe('function')
  const r = fn!({ name: 'bad-4.json', channelDir: CH, retryCount: 1, ageMs: MAX_AGE_MS() + 1, log: () => {} })
  expect(actionOf(r)).toBe('quarantine')
})

test('H21_坏件_年龄恰好等于10分钟_仍在原位(未超阈值)', () => {
  const fn = decide()
  expect(typeof fn).toBe('function')
  const r = fn!({ name: 'bad-5.json', channelDir: CH, retryCount: 1, ageMs: MAX_AGE_MS(), log: () => {} })
  expect(actionOf(r)).toBe('keep')
})

// __END__
void rmTmp
