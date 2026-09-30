// 黑盒验收 · 接入点集成（r2，填补裁判指出的覆盖缺口）
// 目的：证明 backup_retention 的清理策略真的被 worker-manager.ts 的两处备份点**接上了**，
// 而不只是新模块自己算得对（若漏改接线，现有套件在别处全绿也抓不到）。
//
// 只调 worker-manager 的两个对外函数：
//   - stripThinking(jsonl, stripAll)      → 走 'strip' 类
//   - compactSessionIfHuge(jsonl)         → 走 'compact' 类
// 全程 mkdtempSync(os.tmpdir())，绝不碰任何真实用户目录；不读 backup_retention 实现。
// 跑法：cd dispatcher && bun test tests/acceptance/backup_retention_integration.test.ts
//
// 模块缓存注意：worker-manager 的模块级 CHANNEL_DIR 在首次 import 时定死。本文件的两个
// 被测函数只对显式传入的 jsonl 路径操作、不依赖 CHANNEL_DIR，故用 `??=` 只兜底不抢占，
// 避免污染同进程内其它测试文件（worker_manager_exit / worker_manager_stale）。
import { test, expect, afterEach } from 'bun:test'
import {
  existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, join } from 'node:path'

process.env.CHANNEL_DIR ??= mkdtempSync(join(tmpdir(), 'bk-int-ch-'))
process.env.BOT_NAME ??= 'testbot'
const { stripThinking, compactSessionIfHuge } = await import('../../worker-manager')

// ─── 夹具 ────────────────────────────────────────────────────────────────
const TMPDIRS: string[] = []

function mkTmp(): string {
  const d = mkdtempSync(join(tmpdir(), 'bak-int-'))
  TMPDIRS.push(d)
  return d
}

afterEach(() => {
  for (const d of TMPDIRS.splice(0)) rmSync(d, { recursive: true, force: true })
})

const escapeRe = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** 列出某 jsonl 的某后缀类备份文件名（升序）。 */
function listClass(dir: string, base: string, suffix: string): string[] {
  const re = new RegExp(`^${escapeRe(base)}\\.bak\\.([0-9]+)\\.${escapeRe(suffix)}$`)
  return readdirSync(dir).filter(n => re.test(n)).sort()
}

/** 造一份 jsonl 文本行（type=user，content 为纯文本块）。 */
function userLine(text: string, ts = '2026-09-30T00:00:00.000Z'): string {
  return JSON.stringify({ type: 'user', timestamp: ts, message: { content: [{ type: 'text', text }] } })
}

/** 一条含老 thinking 的 assistant 行（时间戳远古 → stripAll=false 也会被剥）。 */
function oldThinkingLine(text = 'THINKING-BODY'): string {
  return JSON.stringify({
    type: 'assistant',
    timestamp: '2020-01-01T00:00:00.000Z',
    message: { content: [{ type: 'thinking', thinking: text }, { type: 'text', text: '久远的回答' }] },
  })
}

/** 造到 >170k token 估值的 jsonl（sizeOf 口径：user/assistant 行 content JSON 长度 /3 求和）。 */
function hugeJsonl(rows = 5, textLen = 120_000): string {
  const text = 'x'.repeat(textLen)
  return Array.from({ length: rows }, (_, i) => userLine(`${i}-${text}`)).join('\n')
}

/** 预置 a..b 秒的旧备份若干。 */
function placeBackups(jsonl: string, suffix: string, secs: number[]): void {
  for (const s of secs) writeFileSync(`${jsonl}.bak.${s}.${suffix}`, `OLD-${s}`)
}

// ═══ 1. strip 链路：真的写了新 .bak.<秒>.strip，且同类被清到 K=2 ═══════════
test('strip链路_含老thinking_生成新strip备份且同类清到K2', () => {
  const dir = mkTmp()
  const jsonl = join(dir, 'session.jsonl')
  const src = [oldThinkingLine(), userLine('普通消息')].join('\n') + '\n'
  writeFileSync(jsonl, src)
  // 预置 4 份超量旧 strip 备份 → 新写 1 份后共 5，应清到 K=2
  placeBackups(jsonl, 'strip', [1000, 1001, 1002, 1003])
  const before = new Set(readdirSync(dir))

  stripThinking(jsonl, false)

  const after = listClass(dir, basename(jsonl), 'strip')
  const created = readdirSync(dir).filter(n => !before.has(n))
  // 证明接线：确实新生成了 1 个 strip 备份
  expect(created.length).toBe(1)
  expect(created[0]).toMatch(new RegExp(`^${escapeRe(basename(jsonl))}\\.bak\\.([0-9]+)\\.strip$`))
  // 清到 K=2：留最新新份 + 旧的 1003；最旧三份(1000/1001/1002)被删
  expect(after.length).toBe(2)
  expect(after).toContain(`${basename(jsonl)}.bak.1003.strip`)
  expect(after).not.toContain(`${basename(jsonl)}.bak.1000.strip`)
  expect(after).not.toContain(`${basename(jsonl)}.bak.1001.strip`)
  expect(after).not.toContain(`${basename(jsonl)}.bak.1002.strip`)
  // 新备份内容 = 写盘前的 jsonl 全文（证明备份的是这次会话，而非空壳）
  expect(readFileSync(join(dir, created[0]), 'utf8')).toBe(src)
  // 新份秒数 ≈ 当前
  const sec = Number(/\.bak\.([0-9]+)\.strip$/.exec(created[0])![1])
  expect(sec).toBeGreaterThanOrEqual(Math.floor(Date.now() / 1000) - 5)
})

// ═══ 2. compact 链路：真的写了新 .bak.<秒>.compact，且同类被清到 K=5 ═══════
test('compact链路_超170k_生成新compact备份且同类清到K5', () => {
  const dir = mkTmp()
  const jsonl = join(dir, 'session.jsonl')
  const src = hugeJsonl()
  writeFileSync(jsonl, src)
  // 预置 6 份超量旧 compact 备份 → 新写 1 份后共 7，应清到 K=5
  placeBackups(jsonl, 'compact', [2000, 2001, 2002, 2003, 2004, 2005])
  const before = new Set(readdirSync(dir))

  compactSessionIfHuge(jsonl)

  const after = listClass(dir, basename(jsonl), 'compact')
  const created = readdirSync(dir).filter(n => !before.has(n))
  // 证明接线：确实新生成了 1 个 compact 备份
  expect(created.length).toBe(1)
  expect(created[0]).toMatch(new RegExp(`^${escapeRe(basename(jsonl))}\\.bak\\.([0-9]+)\\.compact$`))
  // 清到 K=5：留最新新份 + 2002..2005；2000/2001 被删
  expect(after.length).toBe(5)
  for (const s of [2002, 2003, 2004, 2005]) {
    expect(after).toContain(`${basename(jsonl)}.bak.${s}.compact`)
  }
  expect(after).not.toContain(`${basename(jsonl)}.bak.2000.compact`)
  expect(after).not.toContain(`${basename(jsonl)}.bak.2001.compact`)
  // 新备份内容 = 写盘前的 jsonl 全文
  expect(readFileSync(join(dir, created[0]), 'utf8')).toBe(src)
})

// ═══ 3. 早退不写盘：无变化 → 零成本，不产生任何新 .bak、也不清理 ═══════════
test('早退_无thinking_不写新strip备份也不清理', () => {
  const dir = mkTmp()
  const jsonl = join(dir, 'session.jsonl')
  writeFileSync(jsonl, [userLine('没有 thinking 的会话'), userLine('再来一条')].join('\n') + '\n')
  // 预置一份旧备份：若早退后仍触发清理会被删/被动，应原封不动
  placeBackups(jsonl, 'strip', [500])
  const before = readdirSync(dir).sort()

  stripThinking(jsonl, false)

  expect(readdirSync(dir).sort()).toEqual(before)          // 目录零变化
  expect(listClass(dir, basename(jsonl), 'strip')).toEqual([`${basename(jsonl)}.bak.500.strip`])
})

test('早退_不超170k_不写新compact备份也不清理', () => {
  const dir = mkTmp()
  const jsonl = join(dir, 'session.jsonl')
  writeFileSync(jsonl, [userLine('小会话'), userLine('远不到阈值')].join('\n') + '\n')
  placeBackups(jsonl, 'compact', [600])
  const before = readdirSync(dir).sort()

  compactSessionIfHuge(jsonl)

  expect(readdirSync(dir).sort()).toEqual(before)
  expect(listClass(dir, basename(jsonl), 'compact')).toEqual([`${basename(jsonl)}.bak.600.compact`])
})

// ═══ 4. 反向（INV6）：只碰本类 .bak；无关文件/旧无后缀/他类/跨目录诱饵全不动 ═══
test('反向_strip清理不碰无关文件与异类备份', () => {
  const dir = mkTmp()
  const decoyDir = mkTmp()
  const jsonl = join(dir, 'session.jsonl')
  writeFileSync(jsonl, [oldThinkingLine(), userLine('正文')].join('\n') + '\n')

  // 本目录诱饵：全都不该被 strip 清理碰
  const untouched: Record<string, string> = {
    'MEMORY.md.bak': 'MEMO',
    'notes.txt': 'NOTES',
    [`${basename(jsonl)}.cleared-1.json`]: 'CLEARED',
    [`${basename(jsonl)}.bak.999`]: 'LEGACY-NO-SUFFIX',   // 无后缀旧备份（裁定不动）
    [`${basename(jsonl)}.bak.777.compact`]: 'OTHER-CLASS',
    [`${basename(jsonl)}.bak.888.stripp`]: 'PREFIX-SIMILAR',
  }
  for (const [n, v] of Object.entries(untouched)) writeFileSync(join(dir, n), v)
  // 跨目录诱饵：同名同后缀，但不在 jsonl 所在目录
  const crossDecoy = join(decoyDir, `${basename(jsonl)}.bak.1000.strip`)
  writeFileSync(crossDecoy, 'CROSS-DIR-DECOY')

  // 预置 4 份本类旧备份 → 触发删除，顺带验证删除只落在本类
  placeBackups(jsonl, 'strip', [1000, 1001, 1002, 1003])

  stripThinking(jsonl, false)

  // 本类清到 K=2
  expect(listClass(dir, basename(jsonl), 'strip').length).toBe(2)
  // jsonl 本体仍在（内容会因 strip 改写，属预期；只断言存在）
  expect(existsSync(jsonl)).toBe(true)
  // 所有诱饵原样：存在 + 内容不变
  for (const [n, v] of Object.entries(untouched)) {
    const p = join(dir, n)
    expect(existsSync(p)).toBe(true)
    expect(readFileSync(p, 'utf8')).toBe(v)
  }
  // 跨目录同名诱饵不动
  expect(existsSync(crossDecoy)).toBe(true)
  expect(readFileSync(crossDecoy, 'utf8')).toBe('CROSS-DIR-DECOY')
})

test('反向_compact清理不碰无关文件与异类备份', () => {
  const dir = mkTmp()
  const jsonl = join(dir, 'session.jsonl')
  writeFileSync(jsonl, hugeJsonl())

  const untouched: Record<string, string> = {
    'MEMORY.md.bak': 'MEMO',
    [`${basename(jsonl)}.cleared-2.json`]: 'CLEARED',
    [`${basename(jsonl)}.bak.555`]: 'LEGACY-NO-SUFFIX',
    [`${basename(jsonl)}.bak.444.strip`]: 'OTHER-CLASS',
  }
  for (const [n, v] of Object.entries(untouched)) writeFileSync(join(dir, n), v)

  placeBackups(jsonl, 'compact', [2000, 2001, 2002, 2003, 2004, 2005])

  compactSessionIfHuge(jsonl)

  expect(listClass(dir, basename(jsonl), 'compact').length).toBe(5)
  expect(existsSync(jsonl)).toBe(true)
  for (const [n, v] of Object.entries(untouched)) {
    const p = join(dir, n)
    expect(existsSync(p)).toBe(true)
    expect(readFileSync(p, 'utf8')).toBe(v)
  }
  // 异类 strip 备份一份没少
  expect(listClass(dir, basename(jsonl), 'strip')).toEqual([`${basename(jsonl)}.bak.444.strip`])
})
