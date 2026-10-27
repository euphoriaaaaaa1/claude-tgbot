// H3b · H-10 备份失败则不改写 —— 黑盒验收
// 只依据 .devflow/INTERFACE-h3b-delivery.md 一(1-2)。不读实现。
// 判定口径：调用前后的"备份文件名集合"没有新文件 → 不改写、返回 backup_failed、stderr 记
// wm_backup_failed_skip_write；已生成备份则行为不变。
// 复现手法：把会话文件所在目录设为只读 → 备份建不出来（集合无新增）→ 按契约应走 backup_failed。
import { test, expect, afterEach } from 'bun:test'
import { chmodSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import './_fixtures.ts' // 先定死隔离环境（CHANNEL_DIR/HOME/PATH/CLAUDE_BIN），再动态引入被测模块

const wm: any = await import('../../../dispatcher/worker-manager')
const stripThinking: any = wm.stripOldThinking ?? wm.stripThinking
const compactSessionIfHuge: any = wm.compactIfTooLarge ?? wm.compactSessionIfHuge

const DIRS: string[] = []
function mkTmp(): string { const d = mkdtempSync(join(tmpdir(), 'h3b-h10-')); DIRS.push(d); return d }
afterEach(() => { for (const d of DIRS.splice(0)) { try { chmodSync(d, 0o755) } catch { /* ignore */ } try { rmSync(d, { recursive: true, force: true }) } catch { /* ignore */ } } })

const OLD = new Date(Date.now() - 13 * 3600_000).toISOString()
const now = () => new Date().toISOString()
const asstThink = (uuid: string, parent: string | null, ts: string, think: string, text: string) =>
  JSON.stringify({ type: 'assistant', uuid, parentUuid: parent, timestamp: ts, message: { role: 'assistant', content: [{ type: 'thinking', thinking: think }, { type: 'text', text }] } })
const backups = (d: string) => readdirSync(d).filter(f => f.includes('.bak'))

/** 造超大会话：compact_boundary + 80 轮 user/assistant，每行正文约 6000 字符（超预算触发压缩）。 */
function writeHuge(p: string): void {
  const ls: string[] = [JSON.stringify({ type: 'system', subtype: 'compact_boundary', uuid: 'b0', parentUuid: null, timestamp: now() })]
  let prev = 'b0'
  for (let i = 0; i < 80; i++) {
    const ru = 'u' + i, ra = 'a' + i
    ls.push(JSON.stringify({ type: 'user', uuid: ru, parentUuid: prev, timestamp: now(), message: { role: 'user', content: [{ type: 'text', text: 'q' + i + 'X'.repeat(6000) }] } }))
    ls.push(JSON.stringify({ type: 'assistant', uuid: ra, parentUuid: ru, timestamp: now(), message: { role: 'assistant', content: [{ type: 'text', text: 'a' + i + 'X'.repeat(6000) }] } }))
    prev = ra
  }
  writeFileSync(p, ls.join('\n') + '\n')
}

/** 捕获调用期间写往 stderr 的内容。 */
function captureStderr<T>(fn: () => T): { out: T; text: string } {
  const chunks: string[] = []
  const orig = process.stderr.write.bind(process.stderr)
  ;(process.stderr as any).write = (s: any) => { chunks.push(String(s)); return true }
  try { const out = fn(); return { out, text: chunks.join('') } }
  finally { (process.stderr as any).write = orig }
}

// ─── 契约 1：无新备份文件 → 不改写 + backup_failed + stderr 标记 ────────────
test('stripThinking_无新备份_不改写_返回backup_failed_且有stderr标记', () => {
  const d = mkTmp(), p = join(d, 's.jsonl')
  writeFileSync(p, asstThink('a1', null, OLD, 'SECRET', 'hi') + '\n')
  const before = readFileSync(p)
  chmodSync(d, 0o555) // 备份建不出来 → 集合无新增
  const { out, text } = captureStderr(() => stripThinking(p, 'all'))
  expect(out?.ok).toBe(false)
  expect(out?.error).toBe('backup_failed')
  expect(text.includes('wm_backup_failed_skip_write')).toBe(true)
  expect(readFileSync(p).equals(before)).toBe(true) // 原文件逐字不变
})

test('compactSessionIfHuge_无新备份_不改写_返回backup_failed', () => {
  const d = mkTmp(), p = join(d, 'big.jsonl')
  writeHuge(p)
  const before = readFileSync(p)
  chmodSync(d, 0o555)
  const { out } = captureStderr(() => compactSessionIfHuge(p))
  expect(out?.ok).toBe(false)
  expect(out?.error).toBe('backup_failed')
  expect(readFileSync(p).equals(before)).toBe(true)
})

// ─── 契约 2：已生成备份 → 行为不变（正常改写，返回结构照旧）─────────────────
test('stripThinking_有备份_照常改写_且生成备份文件', () => {
  const d = mkTmp(), p = join(d, 's.jsonl')
  writeFileSync(p, asstThink('a1', null, OLD, 'SECRET', 'hi') + '\n')
  const { out } = captureStderr(() => stripThinking(p, 'all'))
  expect(out?.ok).toBe(true)
  expect(out?.replaced).toBe(1)
  expect(backups(d).length).toBeGreaterThan(0)           // 新备份确实出现
  expect(readFileSync(p, 'utf8').includes('thinking')).toBe(false) // 已改写
})

test('compactSessionIfHuge_有备份_照常改写', () => {
  const d = mkTmp(), p = join(d, 'big.jsonl')
  writeHuge(p)
  const { out } = captureStderr(() => compactSessionIfHuge(p))
  expect(out?.ok).toBe(true)
  expect(out?.dropped).toBeGreaterThan(0)
  expect(backups(d).length).toBeGreaterThan(0)
})
