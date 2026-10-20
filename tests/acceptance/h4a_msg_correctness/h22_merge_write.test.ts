// H4a · H-22（合并多条后写入失败不得丢消息）
//
// 只依据 BRIEF/INTERFACE。用既有的 createBurstCollector（本仓波次合并落盘入口，导出名确定），
// 注入"目标目录只读"制造写/rename 失败，观察文件后果与队列可取回性。
// 复现语义：标注 [复现] 的用例当前应为红。
import { test, expect, afterEach } from 'bun:test'
import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { createBurstCollector } from '../../../dispatcher/burst_inbox'
import { mkTmp, rmTmp, countEvent } from './_fixture'

const DIRS: string[] = []
afterEach(() => { for (const d of DIRS.splice(0)) rmTmp(d) })

const isRoot = typeof process.getuid === 'function' && process.getuid() === 0

type Rig = {
  dir: string; dest: string; out: string[]; logs: string[]
  at: (ms: number) => void; add: (o: Record<string, unknown>) => string
  c: ReturnType<typeof createBurstCollector>
}
function rig(): Rig {
  const dir = mkTmp('h4a-burst-'); DIRS.push(dir)
  const dest = join(dir, '.pending')
  let clock = 1_000_000, seq = 0
  const out: string[] = [], logs: string[] = []
  const c = createBurstCollector({
    windowMs: 5000, maxMs: 12000, destDir: dest, now: () => clock,
    deliver: (p: string) => out.push(p), log: (l: string) => logs.push(l),
  })
  const add = (o: Record<string, unknown> = {}): string => {
    const p = join(dir, `m${++seq}.json`)
    writeFileSync(p, JSON.stringify({
      chat_id: 'FAKECHAT-1', message_id: seq, from_id: 'U1', scene: 'private',
      chat_type: 'private', ts: `2026-08-25T0${seq}:00:00.000Z`, text: `T${seq}`, ...o,
    }))
    return p
  }
  return { dir, dest, out, logs, at: (ms) => { clock = ms }, add, c }
}
const readText = (p: string) => JSON.parse(readFileSync(p, 'utf8')).text
const chmodDir = (d: string, mode: number) => { try { chmodSync(d, mode) } catch { /* ignore */ } }

test('H22[复现]_写入失败_不降级提前投递（内容仍留在队列未丢）', () => {
  if (isRoot) return
  const r = rig()
  r.at(1000); const a = r.add({ text: 'A1' }); r.c.arrive(a)
  r.at(1200); const b = r.add({ text: 'B2' }); r.c.arrive(b)
  mkdirSync(r.dest, { recursive: true }); chmodDir(r.dest, 0o555)
  r.at(6300); r.c.tick()
  expect(r.out).toEqual([])                                         // 失败当刻什么都没投出
  chmodDir(r.dest, 0o755)
})

test('H22[复现]_失败后恢复可写_再tick组装内容可投出（证明未丢）', () => {
  if (isRoot) return
  const r = rig()
  r.at(1000); const a = r.add({ text: 'A1' }); r.c.arrive(a)
  r.at(1200); const b = r.add({ text: 'B2' }); r.c.arrive(b)
  mkdirSync(r.dest, { recursive: true }); chmodDir(r.dest, 0o555)
  r.at(6300); r.c.tick()                                            // 失败
  chmodDir(r.dest, 0o755)                                           // 恢复
  r.at(7000); r.c.tick()                                            // 重试
  const delivered = r.out.map(p => { try { return readText(p) } catch { return '' } })
  expect(delivered.some(t => t === 'A1\nB2')).toBe(true)           // 组装内容可取回
})

test('H22[复现]_失败事件名_merge_write_failed带条数', () => {
  if (isRoot) return
  const r = rig()
  r.at(1000); r.c.arrive(r.add({ text: 'A1' }))
  r.at(1200); r.c.arrive(r.add({ text: 'B2' }))
  mkdirSync(r.dest, { recursive: true }); chmodDir(r.dest, 0o555)
  r.at(6300); r.c.tick()
  expect(countEvent(r.logs, 'merge_write_failed')).toBeGreaterThanOrEqual(1)
  expect(r.logs.some(l => /merge_write_failed\s+n=2/.test(l))).toBe(true)
  chmodDir(r.dest, 0o755)
})

test('H22_边界_N等于0_空波tick不产生任何写入与日志', () => {
  const r = rig()
  r.at(9999); r.c.tick(); r.c.tick()
  expect([r.out.length, r.logs.length]).toEqual([0, 0])
  expect(existsSync(r.dest)).toBe(false)
})

test('H22_边界_N等于1_走原路径_不组装不删文件_dest不产生', () => {
  const r = rig()
  r.at(1000); const a = r.add({ text: 'ONLY' }); r.c.arrive(a)
  r.at(6100); r.c.tick()
  expect(r.out).toEqual([a])                                        // 原件直投
  expect(existsSync(a)).toBe(true)                                  // 单条不删
  expect(existsSync(r.dest)).toBe(false)                            // 无合并件
  expect(r.logs.some(l => l.includes('merge_write_failed'))).toBe(false)
})

test('H22_反向_失败后不存在"内容既不在文件也不在队列"的丢失态', () => {
  if (isRoot) return
  const r = rig()
  r.at(1000); const a = r.add({ text: 'A1' }); r.c.arrive(a)
  r.at(1200); const b = r.add({ text: 'B2' }); r.c.arrive(b)
  mkdirSync(r.dest, { recursive: true }); chmodDir(r.dest, 0o555)
  r.at(6300); r.c.tick()
  // 每条源文件要么还在磁盘、要么已被投出/在队列；这里断言都在磁盘（因为未投出）
  for (const p of [a, b]) expect(existsSync(p)).toBe(true)
  chmodDir(r.dest, 0o755)
})
