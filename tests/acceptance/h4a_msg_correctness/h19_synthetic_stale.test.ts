// H4a · H-19（被晾追问的合成件：元数据判定 + 陈旧清理）
//
// 只依据 BRIEF/INTERFACE。用自造短件放临时目录，不碰真实通道目录。
// 契约未给导出名 → 跨 hang_runtime / burst_inbox / inbox_names 多别名回退。
// 复现语义：标注 [复现] 的用例当前应为红（实现缺失或按文件名前缀判定都算红）。
import { test, expect, afterEach } from 'bun:test'
import { existsSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { mkTmp, rmTmp, countEvent, pick } from './_fixture'

const DIRS: string[] = []
afterEach(() => { for (const d of DIRS.splice(0)) rmTmp(d) })
const mk = (p = 'h4a-hang-') => { const d = mkTmp(p); DIRS.push(d); return d }

const hangMod: Record<string, unknown> = await import('../../../dispatcher/hang_runtime')
const burstMod: Record<string, unknown> = await import('../../../dispatcher/burst_inbox')
const namesMod: Record<string, unknown> = await import('../../../dispatcher/inbox_names')
const ALL: Record<string, unknown> = { ...namesMod, ...burstMod, ...hangMod }

const isSynthetic: any = pick(ALL, [
  'isSynthetic', 'isSyntheticMsg', 'isSyntheticInbox', 'isSyntheticFile', 'hasSyntheticMark', 'syntheticFlag',
])
const syntheticTs: any = pick(ALL, [
  'syntheticTs', 'inboxTs', 'pickInboxTs', 'resolveTs', 'fileTs', 'syntheticTime',
])
const sweepStale: any = pick(ALL, [
  'sweepStaleSynthetic', 'cleanStaleSynthetic', 'removeStaleSynthetic', 'pruneStaleSynthetic',
  'sweepStaleHang', 'cleanStaleHang', 'hangStaleSweep', 'sweepStale',
])

const NOW = 1_700_000_000_000
const TTL = 60 * 60_000   // 陈旧阈值 1 小时

/** 位置参数优先、失败回退 deps 对象；两种契约形态都试。 */
function sweep(dir: string, nowMs: number, ttlMs: number, logs: string[]): unknown {
  const deps = {
    dir, channelDir: dir, now: () => nowMs, nowMs, ttlMs, thresholdMs: ttlMs,
    log: (l: string) => logs.push(l),
  }
  try {
    return sweepStale(dir, nowMs, ttlMs)
  } catch (e) {
    if (e instanceof TypeError) return sweepStale(deps)
    throw e
  }
}

// ── 判定：元数据字段说了算，文件名前缀不作数 ──────────────────────────────
test('H19[复现]_件内含synthetic标记_判为合成件（文件名hang-前缀只是可读性）', () => {
  expect(isSynthetic({ synthetic: true, ts: NOW }, 'hang-123.json')).toBe(true)
})

test('H19[复现]_无标记即便名为hang-前缀_也不判为合成件', () => {
  expect(isSynthetic({ text: '普通消息' }, 'hang-999.json')).toBe(false)
})

test('H19[复现]_有标记但文件名无hang前缀_仍判为合成件', () => {
  expect(isSynthetic({ synthetic: true }, 'm7.json')).toBe(true)
})

// ── 陈旧清理：ts 来源与事件 ──────────────────────────────────────────────
test('H19[复现]_合成件ts超阈值_被清理并记hang_stale_removed id等于件名', () => {
  const dir = mk()
  const old = join(dir, 'hang-old.json')
  writeFileSync(old, JSON.stringify({ synthetic: true, ts: NOW - TTL - 1, text: 'x' }))
  const logs: string[] = []
  sweep(dir, NOW, TTL, logs)
  expect(existsSync(old)).toBe(false)                                   // 被清理
  expect(countEvent(logs, 'hang_stale_removed')).toBeGreaterThanOrEqual(1)
  expect(logs.some(l => l.includes('hang_stale_removed') && l.includes('hang-old.json'))).toBe(true)
})

test('H19_非合成件即便很旧_不被清理（反向）', () => {
  const dir = mk()
  const real = join(dir, 'hang-real.json')
  writeFileSync(real, JSON.stringify({ ts: NOW - TTL - 1, text: '真人消息' }))
  const logs: string[] = []
  sweep(dir, NOW, TTL, logs)
  expect(existsSync(real)).toBe(true)
})

test('H19[复现]_合成件缺ts字段_用文件名毫秒前缀判定陈旧并清理', () => {
  const dir = mk()
  const old = join(dir, `hang-${NOW - TTL - 1}.json`)               // 文件名里嵌毫秒时间戳
  writeFileSync(old, JSON.stringify({ synthetic: true, text: 'x' })) // 无件内 ts
  const logs: string[] = []
  sweep(dir, NOW, TTL, logs)
  expect(existsSync(old)).toBe(false)
  expect(countEvent(logs, 'hang_stale_removed')).toBeGreaterThanOrEqual(1)
})

test('H19_ts解析_件内ts优先于文件名前缀（纯函数，存在即可测）', () => {
  expect(syntheticTs).toBeDefined()
  const fromTs = syntheticTs({ ts: 555 }, `hang-${NOW}.json`)
  expect(fromTs).toBe(555)
})
