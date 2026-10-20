/**
 * backup_retention.ts — 会话 `.jsonl.bak.*` 备份保留策略（修备份无限泄漏）。
 *
 * 命名：`<jsonl路径>.bak.<unix秒>.<后缀>`；后缀仅 strip / compact 两类（白名单）。
 * 对外契约见 `.devflow/INTERFACE-bak-retention.md`（验收测试锁定）。要点：
 * - pruneBackups：删除只发生在本类内部——全路径精确匹配 + 同目录单层枚举 + lstat 普通文件，
 *   unlink 前重验；保留最新 K（默认 strip=2 / compact=5）。排序主键 = 文件名内 unix 秒降序，
 *   次键 st_mtime 降序，末位 tiebreak 文件名降序（mtime 不能作主键：备份 mtime 是拷贝源
 *   jsonl 的内容版本时间，非备份创建时间）。
 * - backupSessionJsonl：写一份备份后对同类跑一次清理；写用 COPYFILE_EXCL，撞名 EEXIST → 秒自增
 *   重试（上限 sec+64），撞满则跳过。
 * - fail-open：内部吞掉一切异常，绝不外抛；只 lstat/unlink，不 open 备份内容；日志只含数字与后缀。
 */
import { constants, copyFileSync, lstatSync, readdirSync, unlinkSync } from 'node:fs'
import { dirname, join } from 'node:path'

export const BACKUP_KEEP_STRIP = 2
export const BACKUP_KEEP_COMPACT = 5

/** 白名单：仅这两类后缀参与备份与清理；异形名（大写/前缀相似/带分隔符等）一律不算本类。 */
const VALID_SUFFIXES = new Set(['strip', 'compact'])
/** EEXIST 撞名重试上限：sec..sec+64 共 65 次尝试。 */
const EEXIST_RETRY_LIMIT = 64

export type PruneLog = (line: string) => void

export interface PruneResult {
  total: number      // 清理前该类匹配文件数
  keep: number       // 归一后的保留数（非法入参时为 0）
  deleted: number    // 成功 unlink 数 + ENOENT（已被并发删掉）
  failed: number     // 其余失败数（含非普通文件的跳过）
  doomed: string[]   // 超出保留、尝试删除的完整路径，最旧优先
}

/** 转义正则元字符，供"全路径精确匹配"闸门用（文件名可能含 Unicode/括号等）。 */
const escapeRegExp = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** 闸门 1：候选完整路径必须精确等于 `<jsonl>.bak.<纯数字>.<suffix>`，并捕获秒数。 */
function gateRegex(jsonlPath: string, suffix: string): RegExp {
  return new RegExp(`^${escapeRegExp(jsonlPath)}\\.bak\\.([0-9]+)\\.${escapeRegExp(suffix)}$`)
}

/**
 * 参数归一：未传 keep → 按后缀取默认；非法（非有限整数）→ null（调用方静默返回全零）。
 * 顺序要紧：先判 `< 1` 归一为 1（含 0/负数/-Infinity），再判非有限/非整数（Infinity/小数）。
 */
function resolveKeep(keep: unknown, suffix: string): number | null {
  if (keep === undefined) return suffix === 'compact' ? BACKUP_KEEP_COMPACT : BACKUP_KEEP_STRIP
  if (typeof keep !== 'number' || Number.isNaN(keep)) return null
  if (keep < 1) return 1
  if (!Number.isFinite(keep) || !Number.isInteger(keep)) return null
  return keep
}

function isUsablePath(p: unknown): p is string {
  return typeof p === 'string' && p.length > 0
}
function isUsableSuffix(s: unknown): s is string {
  return typeof s === 'string' && VALID_SUFFIXES.has(s)
}

/**
 * 清理一个 jsonl 的某后缀类备份：保留最新 keep 份，删除其余。永不抛异常（fail-open）。
 * @param jsonlPath 会话 jsonl 完整路径（备份以它为前缀）
 * @param suffix    'strip' | 'compact'
 * @param keep      保留数；未传按后缀默认，<1 归一为 1
 * @param log       有删除/失败时调用一次（恰好一行，无尾随换行）；回调抛异常被吞
 */
export function pruneBackups(jsonlPath: unknown, suffix: unknown, keep?: unknown, log?: PruneLog): PruneResult {
  const zero: PruneResult = { total: 0, keep: 0, deleted: 0, failed: 0, doomed: [] }
  try {
    if (!isUsablePath(jsonlPath) || !isUsableSuffix(suffix)) return { ...zero }
    const k = resolveKeep(keep, suffix)
    if (k === null) return { ...zero }

    const dir = dirname(jsonlPath)
    let names: string[]
    try { names = readdirSync(dir) } catch { return { total: 0, keep: k, deleted: 0, failed: 0, doomed: [] } }

    const re = gateRegex(jsonlPath, suffix)
    const cands: Array<{ path: string; sec: number; mtimeMs: number; name: string }> = []
    for (const name of names) {
      const full = join(dir, name)
      const m = re.exec(full)
      if (!m) continue
      let mtimeMs = 0
      try { mtimeMs = lstatSync(full).mtimeMs } catch { mtimeMs = 0 }
      cands.push({ path: full, sec: Number(m[1]), mtimeMs, name })
    }
    const total = cands.length
    if (total <= k) return { total, keep: k, deleted: 0, failed: 0, doomed: [] }

    // 最新度降序：① 文件名秒 ② mtime ③ 文件名（保证全序确定）
    cands.sort((a, b) =>
      (b.sec - a.sec) || (b.mtimeMs - a.mtimeMs) || (a.name < b.name ? 1 : a.name > b.name ? -1 : 0))
    const doomedCands = cands.slice(k).reverse()   // 超出保留的部分，翻成最旧优先
    const doomed = doomedCands.map(c => c.path)

    let deleted = 0, failed = 0
    for (const c of doomedCands) {
      try {
        if (!lstatSync(c.path).isFile()) { failed++; continue }   // 闸门 3 重验：非普通文件 → 跳过
        unlinkSync(c.path)
        deleted++
      } catch (e) {
        if ((e as NodeJS.ErrnoException)?.code === 'ENOENT') deleted++   // 已被并发删掉 = 目标态达成
        else failed++
      }
    }

    if (deleted + failed > 0 && typeof log === 'function') {
      try { log(`backup-prune suffix=${suffix} total=${total} keep=${k} deleted=${deleted} failed=${failed}`) }
      catch { /* 日志回调自身抛异常：吞掉，不影响删除结果 */ }
    }
    return { total, keep: k, deleted, failed, doomed }
  } catch {
    return { ...zero }
  }
}

/**
 * 写一份会话备份 `<jsonl>.bak.<秒>.<suffix>`，成功后对本类跑一次清理。永不抛异常（fail-open）。
 * COPYFILE_EXCL 保证不覆盖已有；撞名 EEXIST 时秒自增重试，撞满上限则放弃（不写不清理）。
 * 源不存在 / 目录只读等其它错误 → 静默放弃、不清理。@param log 透传给 pruneBackups
 */
export function backupSessionJsonl(jsonlPath: unknown, suffix: unknown, log?: PruneLog): void {
  try {
    if (!isUsablePath(jsonlPath) || !isUsableSuffix(suffix)) return
    const base = Math.floor(Date.now() / 1000)
    let wrote = false
    for (let k = 0; k <= EEXIST_RETRY_LIMIT; k++) {
      const dst = `${jsonlPath}.bak.${base + k}.${suffix}`
      try {
        copyFileSync(jsonlPath, dst, constants.COPYFILE_EXCL)
        wrote = true
        break
      } catch (e) {
        if ((e as NodeJS.ErrnoException)?.code === 'EEXIST') continue   // 撞名 → 试下一秒
        return   // 源不可读/目录不可写等 → 静默放弃，不清理
      }
    }
    if (wrote) pruneBackups(jsonlPath, suffix, undefined, log)
  } catch { /* fail-open：绝不外抛 */ }
}
