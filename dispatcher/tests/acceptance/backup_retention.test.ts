// 黑盒验收 · backup_retention.ts（会话备份保留策略）
// 只依据 .devflow/INTERFACE-bak-retention.md 的对外契约：BACKUP_KEEP_* 常量、
// backupSessionJsonl / pruneBackups 两个导出入口。不读实现、不 import worker-manager。
// 全程只用 mkdtempSync(os.tmpdir())，绝不碰任何真实用户目录。
// 跑法：cd dispatcher && bun test tests/acceptance/backup_retention.test.ts
//
// 红绿预期：实现尚不存在 → 本文件因 `../../backup_retention` 解析失败而整体报错（预期全红）。
import { test, expect, afterEach, spyOn } from 'bun:test'
import {
  chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync,
  rmSync, symlinkSync, utimesSync, writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, join } from 'node:path'
import {
  BACKUP_KEEP_STRIP, BACKUP_KEEP_COMPACT, backupSessionJsonl, pruneBackups,
} from '../../backup_retention'

// ─── 夹具 ────────────────────────────────────────────────────────────────
const TMPDIRS: string[] = []
const isRoot = typeof process.getuid === 'function' && process.getuid() === 0

function mkTmp(): string {
  const d = mkdtempSync(join(tmpdir(), 'bak-retention-'))
  TMPDIRS.push(d)
  return d
}

/** 目录可能被测试 chmod 成只读，清理前先恢复权限再递归删。 */
function restorePerms(d: string): void {
  try { chmodSync(d, 0o755) } catch { /* ignore */ }
  try {
    for (const e of readdirSync(d)) {
      try { chmodSync(join(d, e), 0o644) } catch { /* symlink/权限不可改，忽略 */ }
    }
  } catch { /* ignore */ }
}

afterEach(() => {
  for (const d of TMPDIRS.splice(0)) {
    restorePerms(d)
    rmSync(d, { recursive: true, force: true })
  }
})

const bak = (jsonl: string, sec: number, suffix: string) => `${jsonl}.bak.${sec}.${suffix}`
/** 造一个合法备份文件；返回其绝对路径。 */
function seed(jsonl: string, sec: number, suffix: string, content = `c-${sec}`): string {
  const p = bak(jsonl, sec, suffix)
  writeFileSync(p, content)
  return p
}
const listDir = (d: string) => readdirSync(d).sort()
/** 某目录里所有匹配 `<jsonl>.bak.<数字>.<suffix>` 的名字。 */
function namesOf(jsonl: string, suffix: string, d: string): string[] {
  const re = new RegExp(`^${basename(jsonl).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\.bak\\.[0-9]+\\.${suffix}$`)
  return listDir(d).filter(n => re.test(n))
}

// ─── 常量（钉死 2 / 5）───────────────────────────────────────────────────
test('常量_BACKUP_KEEP_STRIP为2', () => { expect(BACKUP_KEEP_STRIP).toBe(2) })
test('常量_BACKUP_KEEP_COMPACT为5', () => { expect(BACKUP_KEEP_COMPACT).toBe(5) })

// ─── 正常路径：默认保留数（strip=2 / compact=5）──────────────────────────
test('prune_strip默认保留2_5份删3份_留最新2', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'x')
  for (let i = 0; i < 5; i++) seed(jsonl, 1000 + i, 'strip')
  const r = pruneBackups(jsonl, 'strip')
  expect(r).toEqual({
    total: 5, keep: 2, deleted: 3, failed: 0, doomed: [bak(jsonl, 1000, 'strip'), bak(jsonl, 1001, 'strip'), bak(jsonl, 1002, 'strip')],
  })
  expect(existsSync(bak(jsonl, 1000, 'strip'))).toBe(false)
  expect(existsSync(bak(jsonl, 1003, 'strip'))).toBe(true)
  expect(existsSync(bak(jsonl, 1004, 'strip'))).toBe(true)
})

test('prune_compact默认保留5_8份删3份_留最新5', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'x')
  for (let i = 0; i < 8; i++) seed(jsonl, 2000 + i, 'compact')
  const r = pruneBackups(jsonl, 'compact')
  expect(r.total).toBe(8)
  expect(r.keep).toBe(5)
  expect(r.deleted).toBe(3)
  expect(r.doomed).toEqual([2000, 2001, 2002].map(s => bak(jsonl, s, 'compact')))
  expect(namesOf(jsonl, 'compact', d).length).toBe(5)
})

test('prune_显式keep覆盖默认_strip传4_6份只删2', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'x')
  for (let i = 0; i < 6; i++) seed(jsonl, 3000 + i, 'strip')
  const r = pruneBackups(jsonl, 'strip', 4)
  expect(r).toEqual({ total: 6, keep: 4, deleted: 2, failed: 0, doomed: [bak(jsonl, 3000, 'strip'), bak(jsonl, 3001, 'strip')] })
})

test('prune_显式keep=1_compact也压到1', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'x')
  for (let i = 0; i < 3; i++) seed(jsonl, 4000 + i, 'compact')
  const r = pruneBackups(jsonl, 'compact', 1)
  expect(r.keep).toBe(1)
  expect(r.deleted).toBe(2)
  expect(namesOf(jsonl, 'compact', d)).toEqual([basename(bak(jsonl, 4002, 'compact'))])
})

test('prune_排序以文件名字段秒为主键_不按mtime（keep=1留名字最新）', () => {
  // 名字秒 200 的 mtime 设为"旧"，名字秒 100 的 mtime 设为"新"。
  // 正确实现以名字秒为主键 → 留 200、删 100；若误用 mtime 主键 → 会留 100，本用例抓住。
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'x')
  const p100 = seed(jsonl, 100, 'strip')
  const p200 = seed(jsonl, 200, 'strip')
  utimesSync(p100, new Date(), new Date())
  utimesSync(p200, new Date(1_000_000_000_000), new Date(1_000_000_000_000))
  const r = pruneBackups(jsonl, 'strip', 1)
  expect(r.doomed).toEqual([p100])
  expect(existsSync(p100)).toBe(false)
  expect(existsSync(p200)).toBe(true)
})

// ─── 边界：0 / 1 / K / K+1 份 ────────────────────────────────────────────
test('prune_空目录0份_返回total0_keep默认2_零删除', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'x')
  expect(pruneBackups(jsonl, 'strip')).toEqual({ total: 0, keep: 2, deleted: 0, failed: 0, doomed: [] })
})

test('prune_仅1份_不超过K_不删不打行', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'x')
  seed(jsonl, 5000, 'strip')
  const lines: string[] = []
  const r = pruneBackups(jsonl, 'strip', 2, l => lines.push(l))
  expect(r).toEqual({ total: 1, keep: 2, deleted: 0, failed: 0, doomed: [] })
  expect(lines).toEqual([])
})

test('prune_恰好K份_不删不打行', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'x')
  seed(jsonl, 6000, 'strip'); seed(jsonl, 6001, 'strip')
  const lines: string[] = []
  expect(pruneBackups(jsonl, 'strip', 2, l => lines.push(l))).toEqual({ total: 2, keep: 2, deleted: 0, failed: 0, doomed: [] })
  expect(lines).toEqual([])
})

test('prune_K加1份_只删最旧1份', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'x')
  for (let i = 0; i < 3; i++) seed(jsonl, 7000 + i, 'strip')
  const r = pruneBackups(jsonl, 'strip', 2)
  expect(r.deleted).toBe(1)
  expect(r.doomed).toEqual([bak(jsonl, 7000, 'strip')])
  expect(namesOf(jsonl, 'strip', d)).toEqual([basename(bak(jsonl, 7001, 'strip')), basename(bak(jsonl, 7002, 'strip'))].sort())
})

test('prune_目录不存在_静默空结果_不抛', () => {
  const jsonl = join(mkTmp(), 'nope', 'a.jsonl')   // 父目录不存在
  let r: any
  expect(() => { r = pruneBackups(jsonl, 'strip') }).not.toThrow()
  expect(r.deleted).toBe(0)
  expect(r.failed).toBe(0)
  expect(r.doomed).toEqual([])
})

// ─── 错误路径：keep 归一 / 非法 ──────────────────────────────────────────
test('prune_keep=0_归一为1', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 3; i++) seed(jsonl, 8000 + i, 'strip')
  const r = pruneBackups(jsonl, 'strip', 0)
  expect(r.keep).toBe(1)
  expect(r.deleted).toBe(2)
  expect(namesOf(jsonl, 'strip', d)).toEqual([basename(bak(jsonl, 8002, 'strip'))])
})

test('prune_keep为负数_归一为1', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 3; i++) seed(jsonl, 8100 + i, 'strip')
  const r = pruneBackups(jsonl, 'strip', -5)
  expect(r.keep).toBe(1)
  expect(r.deleted).toBe(2)
})

test('prune_keep为负Infinity_归一为1', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  seed(jsonl, 8200, 'strip'); seed(jsonl, 8201, 'strip')
  const r = pruneBackups(jsonl, 'strip', -Infinity)
  expect(r.keep).toBe(1)
  expect(r.deleted).toBe(1)
})

for (const [label, bad] of [
  ['Infinity', Infinity], ['NaN', NaN], ['小数1.5', 1.5], ['字符串2', '2'], ['null', null],
] as [string, unknown][]) {
  test(`prune_keep为非有限整数_${label}_返回空结果不打行`, () => {
    const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
    for (let i = 0; i < 4; i++) seed(jsonl, 8300 + i, 'strip')
    const lines: string[] = []
    const r = pruneBackups(jsonl, 'strip', bad as never, l => lines.push(l))
    expect(r).toEqual({ total: 0, keep: 0, deleted: 0, failed: 0, doomed: [] })
    expect(lines).toEqual([])
    expect(namesOf(jsonl, 'strip', d).length).toBe(4)   // 一份都没动
  })
}

// ─── 错误路径：suffix 非法 ───────────────────────────────────────────────
for (const [label, bad] of [
  ['空串', ''], ['大写STRIP', 'STRIP'], ['尾随空格"strip "', 'strip '], ['前缀"stripx"', 'stripx'],
  ['带数字"strip2"', 'strip2'], ['含分隔符"a/strip"', 'a/strip'], ['含分隔符反斜杠', 'a\\strip'],
  ['含点号"st.rip"', 'st.rip'], ['含星号"st*rip"', 'st*rip'], ['只一个点"."', '.'],
  ['null', null], ['undefined', undefined],
] as [string, unknown][]) {
  test(`prune_suffix非法_${label}_返回空结果不打行_零删除`, () => {
    const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
    for (let i = 0; i < 5; i++) seed(jsonl, 9000 + i, 'strip')
    const lines: string[] = []
    const r = pruneBackups(jsonl, bad as never, 2, l => lines.push(l))
    expect(r).toEqual({ total: 0, keep: 0, deleted: 0, failed: 0, doomed: [] })
    expect(lines).toEqual([])
    expect(namesOf(jsonl, 'strip', d).length).toBe(5)
  })
}

test('backupSessionJsonl_suffix非法_不写备份不prune不打行_零副作用', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 5; i++) seed(jsonl, 9100 + i, 'strip')   // 预置超量旧备份，验证也不清理
  const before = listDir(d)
  const lines: string[] = []
  expect(backupSessionJsonl(jsonl, 'STRIP', l => lines.push(l))).toBeUndefined()
  expect(listDir(d)).toEqual(before)          // 无新增、无删除
  expect(lines).toEqual([])
})

// ─── 错误路径：jsonlPath 非法 ────────────────────────────────────────────
for (const [label, bad] of [['空串', ''], ['null', null], ['undefined', undefined]] as [string, unknown][]) {
  test(`prune_jsonlPath非法_${label}_返回空结果不打行`, () => {
    const lines: string[] = []
    const r = pruneBackups(bad as never, 'strip', 2, l => lines.push(l))
    expect(r).toEqual({ total: 0, keep: 0, deleted: 0, failed: 0, doomed: [] })
    expect(lines).toEqual([])
  })
}

test('backupSessionJsonl_jsonlPath为空_零副作用不抛', () => {
  expect(() => backupSessionJsonl('', 'strip')).not.toThrow()
  expect(() => backupSessionJsonl(null as never, 'strip')).not.toThrow()
})

// ─── 观测行（log 回调）语义 ──────────────────────────────────────────────
test('log_有删除时恰好调用一次_行格式逐字一致', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 5; i++) seed(jsonl, 9200 + i, 'strip')
  const lines: string[] = []
  pruneBackups(jsonl, 'strip', 2, l => lines.push(l))
  expect(lines.length).toBe(1)
  expect(lines[0]).toBe('backup-prune suffix=strip total=5 keep=2 deleted=3 failed=0')
})

test('log_无操作时不调用（零删除零失败）', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  seed(jsonl, 9300, 'compact')
  let calls = 0
  pruneBackups(jsonl, 'compact', 5, () => { calls++ })
  expect(calls).toBe(0)
})

test('log_未传log_不产生任何console输出', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 4; i++) seed(jsonl, 9400 + i, 'strip')
  const logSpy = spyOn(console, 'log').mockImplementation(() => {})
  const errSpy = spyOn(console, 'error').mockImplementation(() => {})
  try {
    pruneBackups(jsonl, 'strip', 1)   // 传了log才有输出；这里没传 → 连console都不许碰
    expect(logSpy).not.toHaveBeenCalled()
    expect(errSpy).not.toHaveBeenCalled()
  } finally { logSpy.mockRestore(); errSpy.mockRestore() }
})

test('log_回调自身抛异常_被吞_删除结果不受影响_只尝试调用一次', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 4; i++) seed(jsonl, 9500 + i, 'strip')
  let calls = 0
  let r: any
  expect(() => { r = pruneBackups(jsonl, 'strip', 1, () => { calls++; throw new Error('log boom') }) }).not.toThrow()
  expect(calls).toBe(1)
  expect(r.deleted).toBe(3)
  expect(namesOf(jsonl, 'strip', d)).toEqual([basename(bak(jsonl, 9503, 'strip'))])
})

// ─── 正常路径：backupSessionJsonl 写盘 ───────────────────────────────────
test('backupSessionJsonl_写出一份_bak_秒_后缀_内容与源一致_返回undefined', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'SOURCE-BODY-1')
  const ret = backupSessionJsonl(jsonl, 'strip')
  expect(ret).toBeUndefined()
  const made = namesOf(jsonl, 'strip', d)
  expect(made.length).toBe(1)
  expect(made[0]).toMatch(/^a\.jsonl\.bak\.[0-9]+\.strip$/)
  expect(readFileSync(join(d, made[0]), 'utf8')).toBe('SOURCE-BODY-1')
})

test('backupSessionJsonl_写后清理_strip留2_5份减到2', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'BODY')
  for (let i = 0; i < 4; i++) seed(jsonl, 1_000_000_000 + i, 'strip')   // 4 份更旧
  backupSessionJsonl(jsonl, 'strip')                                    // +1 新 → 5，留2
  const kept = namesOf(jsonl, 'strip', d)
  expect(kept.length).toBe(2)
  expect(existsSync(bak(jsonl, 1_000_000_003, 'strip'))).toBe(true)      // 旧的里最新的那份保住
  expect(existsSync(bak(jsonl, 1_000_000_000, 'strip'))).toBe(false)    // 最旧三份被清
  expect(existsSync(bak(jsonl, 1_000_000_001, 'strip'))).toBe(false)
  expect(existsSync(bak(jsonl, 1_000_000_002, 'strip'))).toBe(false)
})

test('backupSessionJsonl_撞名EEXIST_sec自增重试_两份并存互不覆盖', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'NEW-BODY')
  const sec = Math.floor(Date.now() / 1000)
  const pre = bak(jsonl, sec, 'strip')
  writeFileSync(pre, 'PREEXISTING-BODY')
  backupSessionJsonl(jsonl, 'strip')
  expect(existsSync(pre)).toBe(true)                       // 未被覆盖
  expect(readFileSync(pre, 'utf8')).toBe('PREEXISTING-BODY')
  const made = namesOf(jsonl, 'strip', d)
  expect(made.length).toBe(2)
  const fresh = made.find(n => n !== basename(pre))!
  expect(fresh).toBeDefined()
  expect(fresh).toMatch(new RegExp(`^a\\.jsonl\\.bak\\.${sec + 1}\\.strip$`))   // sec++
  expect(readFileSync(join(d, fresh), 'utf8')).toBe('NEW-BODY')
})

test('backupSessionJsonl_撞满上限_跳过备份不prune零副作用', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'BODY')
  const sec = Math.floor(Date.now() / 1000)
  for (let i = -2; i <= 70; i++) seed(jsonl, sec + i, 'strip')   // 覆盖 sec..sec+64 及两侧
  const before = listDir(d)
  expect(() => backupSessionJsonl(jsonl, 'strip')).not.toThrow()
  expect(listDir(d)).toEqual(before)   // 既不新增、也不 prune
})

test('backupSessionJsonl_返回void_异常路径也返回undefined', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  expect(backupSessionJsonl(jsonl, 'compact')).toBeUndefined()
  expect(backupSessionJsonl(null as never, 'strip')).toBeUndefined()
})

// ─── 幂等（INV2）────────────────────────────────────────────────────────
test('prune_连续两次_第二次全0_文件集合不变', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 7; i++) seed(jsonl, 1_100_000_000 + i, 'compact')
  expect(pruneBackups(jsonl, 'compact').deleted).toBe(2)
  const afterFirst = listDir(d)
  const r2 = pruneBackups(jsonl, 'compact')
  expect(r2).toEqual({ total: 5, keep: 5, deleted: 0, failed: 0, doomed: [] })
  expect(listDir(d)).toEqual(afterFirst)
})

test('prune_空目录重复执行_恒为空结果', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  expect(pruneBackups(jsonl, 'strip')).toEqual({ total: 0, keep: 2, deleted: 0, failed: 0, doomed: [] })
  expect(pruneBackups(jsonl, 'strip')).toEqual({ total: 0, keep: 2, deleted: 0, failed: 0, doomed: [] })
})

// ─── 反向用例：不该删的绝不动（INV6）────────────────────────────────────
test('反向_清strip时_他类备份_无后缀旧备份_本体_sidecar全不动', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl')
  writeFileSync(jsonl, 'JSONL-BODY')
  writeFileSync(join(d, '.cleared-abc'), 'CLEARED')
  writeFileSync(join(d, 'MEMORY.md.bak'), 'MEMORY')
  writeFileSync(join(d, 'a.jsonl.lock'), 'LOCK')
  writeFileSync(join(d, 'a.jsonl.tmp.x'), 'TMP')
  seed(jsonl, 100, 'compact', 'COMPACT-BODY')                 // 他类后缀
  seed(jsonl, 111, 'strip', 'X')                              // 本类候选
  seed(jsonl, 112, 'strip', 'Y')                              // 本类候选
  seed(jsonl, 222, 'strip', 'Z')                              // 本类最新（受 INV1 保护）
  writeFileSync(`${jsonl}.bak.999`, 'LEGACY')                 // 无后缀旧备份
  writeFileSync(`${jsonl}.bak.100.strip2`, 'STRIP2')          // 前缀相似
  writeFileSync(`${jsonl}.bak.100.compaction`, 'COMPACTION')  // 他类后缀
  writeFileSync(`${jsonl}.bak2.100.strip`, 'BAK2')            // 杂项 .bak2
  writeFileSync(`${jsonl}.bak-1234.strip`, 'DASH')            // 杂项 .bak-<毫秒>

  const r = pruneBackups(jsonl, 'strip', 2)
  expect(r.doomed).toEqual([bak(jsonl, 111, 'strip')])        // 只删本类最旧那份

  for (const [name, body] of [
    ['a.jsonl', 'JSONL-BODY'], ['.cleared-abc', 'CLEARED'], ['MEMORY.md.bak', 'MEMORY'],
    ['a.jsonl.lock', 'LOCK'], ['a.jsonl.tmp.x', 'TMP'],
    ['a.jsonl.bak.100.compact', 'COMPACT-BODY'], ['a.jsonl.bak.999', 'LEGACY'],
    ['a.jsonl.bak.100.strip2', 'STRIP2'], ['a.jsonl.bak.100.compaction', 'COMPACTION'],
    ['a.jsonl.bak2.100.strip', 'BAK2'], ['a.jsonl.bak-1234.strip', 'DASH'],
    ['a.jsonl.bak.222.strip', 'Z'],
  ] as [string, string][]) {
    expect(existsSync(join(d, name))).toBe(true)
    expect(readFileSync(join(d, name), 'utf8')).toBe(body)
  }
})

test('反向_无后缀旧备份bak点秒_不参与清理不增长', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 6; i++) writeFileSync(`${jsonl}.bak.${500 + i}`, `legacy-${i}`)
  const before = listDir(d)
  const r = pruneBackups(jsonl, 'strip')
  expect(r.total).toBe(0)                 // 无后缀旧备份不计入本类
  expect(r.deleted).toBe(0)
  expect(listDir(d)).toEqual(before)
})

test('反向_前缀相似strip2与compaction_不被match', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  writeFileSync(`${jsonl}.bak.10.strip2`, 'A')
  writeFileSync(`${jsonl}.bak.10.compaction`, 'B')
  writeFileSync(`${jsonl}.bak.10.stripp`, 'C')
  const before = listDir(d)
  expect(pruneBackups(jsonl, 'strip', 1)).toEqual({ total: 0, keep: 1, deleted: 0, failed: 0, doomed: [] })
  expect(listDir(d)).toEqual(before)
})

test('反向_符号链接备份_被跳过计入failed_链接与目标都在', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  const target = join(d, 'target.txt'); writeFileSync(target, 'TARGET')
  const link = bak(jsonl, 50, 'strip')            // 最旧，理应被删，但它是软链
  symlinkSync(target, link)
  seed(jsonl, 200, 'strip', 'B200')
  seed(jsonl, 300, 'strip', 'B300')

  const r = pruneBackups(jsonl, 'strip', 1)
  expect(r.failed).toBeGreaterThanOrEqual(1)
  expect(r.deleted).toBe(1)
  expect(lstatSync(link).isSymbolicLink()).toBe(true)   // 链接项未被移除
  expect(existsSync(target)).toBe(true)
  expect(readFileSync(target, 'utf8')).toBe('TARGET')   // 目标未被碰
  expect(existsSync(bak(jsonl, 300, 'strip'))).toBe(true)
})

test('反向_目录占同名_bak_秒_后缀_被跳过_目录仍在', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  mkdirSync(bak(jsonl, 50, 'strip'))              // 目录占位（最旧，理应删除资格）
  seed(jsonl, 300, 'strip', 'NEW')
  const r = pruneBackups(jsonl, 'strip', 1)
  expect(r.deleted).toBe(0)
  expect(r.failed).toBeGreaterThanOrEqual(1)
  expect(lstatSync(bak(jsonl, 50, 'strip')).isDirectory()).toBe(true)
  expect(existsSync(bak(jsonl, 300, 'strip'))).toBe(true)
})

test('反向_跨目录诱饵_同名同后缀_不动', () => {
  const a = mkTmp(), b = mkTmp()
  const jsonl = join(a, 'a.jsonl'); writeFileSync(jsonl, 'x')
  seed(jsonl, 100, 'strip', 'IN-A'); seed(jsonl, 200, 'strip', 'IN-A2')
  // 另一目录里放"看起来一样"的文件，绝不该被本目录的清理碰到
  const decoy = join(b, 'a.jsonl.bak.100.strip'); writeFileSync(decoy, 'DECOY')
  expect(pruneBackups(jsonl, 'strip', 1).deleted).toBe(1)
  expect(existsSync(decoy)).toBe(true)
  expect(readFileSync(decoy, 'utf8')).toBe('DECOY')
})

test('反向_Unicode与正则元字符文件名_精确匹配不算错', () => {
  const d = mkTmp()
  const jsonl = join(d, '会话 测试(+)$[1].jsonl')
  writeFileSync(jsonl, 'x')
  seed(jsonl, 100, 'strip', 'r100'); seed(jsonl, 200, 'strip', 'r200'); seed(jsonl, 300, 'strip', 'r300')
  const decoy = join(d, '会话 测试(X)$[1].jsonl.bak.100.strip'); writeFileSync(decoy, 'DECOY')

  const r = pruneBackups(jsonl, 'strip', 1)
  expect(r.total).toBe(3)               // 只数真正的 3 份，诱饵不计
  expect(r.deleted).toBe(2)
  expect(existsSync(bak(jsonl, 300, 'strip'))).toBe(true)
  expect(existsSync(decoy)).toBe(true)
  expect(readFileSync(decoy, 'utf8')).toBe('DECOY')
})

test('反向_保留备份的mtime不被改', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  seed(jsonl, 100, 'strip', 'OLD'); const keepP = seed(jsonl, 200, 'strip', 'KEEP')
  const fixed = new Date(1_234_567_000_000)
  utimesSync(keepP, fixed, fixed)
  pruneBackups(jsonl, 'strip', 1)
  expect(Math.floor(lstatSync(keepP).mtimeMs / 1000)).toBe(Math.floor(fixed.getTime() / 1000))
})

// ─── 隐私（只删不读 / 日志无路径无正文）──────────────────────────────────
test('隐私_哨兵写入备份_清理日志不含哨兵不含路径不含文件名', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  const SENT = 'SENTINEL-' + Math.random().toString(36).slice(2) + '-' + Math.random().toString(36).slice(2)
  for (let i = 0; i < 5; i++) seed(jsonl, 9_000_000_000 + i, 'strip', `${SENT}#${i}`)

  const lines: string[] = []
  const r = pruneBackups(jsonl, 'strip', 2, l => lines.push(l))
  expect(r.deleted).toBe(3)
  expect(lines.length).toBe(1)
  const line = lines[0]
  expect(line).not.toContain(SENT)
  expect(line).not.toContain(d)                 // 无目录
  expect(line).not.toContain(basename(jsonl))   // 无文件名/正文
  expect(line).not.toContain('.bak')
  expect(line).toBe('backup-prune suffix=strip total=5 keep=2 deleted=3 failed=0')
})

test('隐私_提供log回调时_也不额外产生console输出', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 3; i++) seed(jsonl, 9_100_000_000 + i, 'strip', 'S')
  const logSpy = spyOn(console, 'log').mockImplementation(() => {})
  const errSpy = spyOn(console, 'error').mockImplementation(() => {})
  const lines: string[] = []
  try {
    pruneBackups(jsonl, 'strip', 1, l => lines.push(l))
    expect(lines.length).toBe(1)
    expect(logSpy).not.toHaveBeenCalled()
    expect(errSpy).not.toHaveBeenCalled()
  } finally { logSpy.mockRestore(); errSpy.mockRestore() }
})

test('隐私_被删备份chmod000_仍被正常删除_证明未读内容', () => {
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  const oldest = seed(jsonl, 9_200_000_000, 'strip', 'SECRET-BODY')
  seed(jsonl, 9_200_000_001, 'strip', 'B')
  chmodSync(oldest, 0o000)                       // 若实现去 open 该文件会 EACCES；正确实现只 lstat/unlink
  const r = pruneBackups(jsonl, 'strip', 1)
  expect(r.deleted).toBe(1)
  expect(existsSync(oldest)).toBe(false)
})

// ─── fail-open（INV4：永不外抛，绝不改变调用方语义）──────────────────────
test('failopen_只读目录下prune_计failed_不抛_打行', () => {
  if (isRoot) return   // root 不受权限约束，本用例无意义
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 4; i++) seed(jsonl, 9_300_000_000 + i, 'strip')
  chmodSync(d, 0o555)                            // 目录只读 → unlink EACCES
  const lines: string[] = []
  let r: any
  expect(() => { r = pruneBackups(jsonl, 'strip', 2, l => lines.push(l)) }).not.toThrow()
  expect(r.deleted).toBe(0)
  expect(r.failed).toBe(2)
  expect(r.doomed.length).toBe(2)                // 尝试删除的集合（含 failed）
  expect(lines).toEqual(['backup-prune suffix=strip total=4 keep=2 deleted=0 failed=2'])
})

test('failopen_只读目录下backupSessionJsonl_拷贝失败_零副作用_不抛', () => {
  if (isRoot) return
  const d = mkTmp(), jsonl = join(d, 'a.jsonl'); writeFileSync(jsonl, 'x')
  for (let i = 0; i < 3; i++) seed(jsonl, 9_400_000_000 + i, 'strip')
  chmodSync(d, 0o555)
  const before = listDir(d)
  expect(() => backupSessionJsonl(jsonl, 'strip')).not.toThrow()
  expect(listDir(d)).toEqual(before)             // 不新增、不 prune
})

test('failopen_父目录不存在_backupSessionJsonl不抛_零副作用', () => {
  const jsonl = join(mkTmp(), 'missing-dir', 'a.jsonl')
  expect(() => backupSessionJsonl(jsonl, 'strip')).not.toThrow()
  expect(() => backupSessionJsonl(jsonl, 'compact')).not.toThrow()
})

test('failopen_怪输入永不抛_遍历分数值', () => {
  const bogus = [undefined, null, 0, NaN, Infinity, '', {}, [], Symbol('s')] as unknown[]
  for (const v of bogus) {
    expect(() => pruneBackups(v as never, v as never)).not.toThrow()
    expect(() => backupSessionJsonl(v as never, v as never)).not.toThrow()
  }
})
