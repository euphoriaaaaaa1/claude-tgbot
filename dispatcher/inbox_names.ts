/**
 * inbox 文件名约定（desync ⑤）：合成消息的文件名前缀集合只在这里定义一次。
 * bot 停用期间 self_initiate / director / moments / voicecall 照写 inbox，启用后这些陈旧合成件
 * 不能补发——pump() 读队头时按文件名 + mtime 丢掉它们；真人消息文件再旧也照投。
 * 契约见 INTERFACE-desync §9.5 §10.5。
 */
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync } from 'fs'
import { basename, dirname, join } from 'path'

export const SYNTHETIC_FILE_PREFIXES = ['self-', 'director-', 'dm-nudge-', 'moment-', 'hang-', 'voice-']
export const STALE_SYNTHETIC_MS = 30 * 60_000

// ─── H-21：队头坏件不删、先重试、超阈值移入隔离目录 ────────────────────────
/** 坏件重试上限（超过它 → 隔离）。 */
export const BAD_HEAD_MAX_RETRY = 3
/** 坏件存活年龄上限（超过它即便重试次数未超也隔离）。 */
export const BAD_HEAD_MAX_AGE_MS = 10 * 60_000

export const QUARANTINE_DIRNAME = '.quarantine'

/**
 * H-21：队头坏件处置（判定 + 尽力移件）。retryCount > BAD_HEAD_MAX_RETRY 或 ageMs > BAD_HEAD_MAX_AGE_MS
 * → 隔离：移入 `<channelDir>/.quarantine/`（**在 inbox 扫描范围之外**，不删件），记 `wm_bad_head_quarantine`。
 * 阈值内 → 保留原位，记 `wm_bad_head_retry n=<次数>`。
 * 返回 `{action:'keep'|'quarantine', retryCount, quarantinePath?}`。
 */
export function decideBadHead(input: {
  name: string; channelDir: string; retryCount: number; ageMs: number; log?: (l: string) => void
}): { action: 'keep' | 'quarantine'; retryCount: number; quarantinePath?: string } {
  const log = typeof input.log === 'function' ? input.log : (l: string) => process.stderr.write(`${l}\n`)
  const retryCount = Number.isFinite(input.retryCount) ? input.retryCount : 0
  const ageMs = Number.isFinite(input.ageMs) ? input.ageMs : 0
  if (retryCount > BAD_HEAD_MAX_RETRY || ageMs > BAD_HEAD_MAX_AGE_MS) {
    const qdir = join(input.channelDir, QUARANTINE_DIRNAME)
    const qp = join(qdir, basename(input.name))
    try {
      mkdirSync(qdir, { recursive: true })
      const src = join(input.channelDir, 'inbox', basename(input.name))
      if (existsSync(src)) renameSync(src, qp)   // 尽力移件；不存在则只返回目标路径
    } catch { /* 移件失败不崩：返回目标路径，下一轮重试 */ }
    log(`wm_bad_head_quarantine id=${input.name}`)
    return { action: 'quarantine', retryCount, quarantinePath: qp }
  }
  log(`wm_bad_head_retry n=${retryCount}`)
  return { action: 'keep', retryCount }
}

/** 按文件名判合成（调用方传 basename）。真人件 `<chat>_<msg>.json`、`user-moment-*` 不匹配任何前缀 */
export function isSyntheticName(name: string): boolean {
  return typeof name === 'string' && SYNTHETIC_FILE_PREFIXES.some(p => name.startsWith(p))
}

/** 合成件且 now - mtime > 阈值 → drop；stat 失败（非有限数）/ 未来 mtime / 真人件 → keep（旧行为照投） */
export function staleDecision(name: string, mtimeMs: number, nowMs: number): 'drop' | 'keep' {
  return isSyntheticName(name) && Number.isFinite(mtimeMs) && nowMs - mtimeMs > STALE_SYNTHETIC_MS ? 'drop' : 'keep'
}

/**
 * pump() 读队头文件的唯一入口：先判陈旧合成件（删 + drop_stale_synthetic 日志 + null），再解析。
 * 读失败 / 坏 JSON / 顶层非对象 → null，但不删（删除归 pump 的 drop 集合）。rm 失败 → 照投 + 日志。
 */
export function readInboxMeta(path: string, nowMs: number,
  log: (line: string) => void = l => process.stderr.write(`${l}\n`)): Record<string, any> | null {
  // ponytail: 容忍一段 `<数字>-` 前缀再判合成前缀（验收夹具用它给文件编号）；公开版真人件是
  // `<chat>_<msg>`（下划线）、群是 `-` 开头，都碰不上这条，不会误删真人消息。
  const name = basename(path).replace(/^\d+-/, '')
  let mtimeMs = NaN
  try { mtimeMs = statSync(path).mtimeMs } catch {}
  if (staleDecision(name, mtimeMs, nowMs) === 'drop') {
    const age = Math.round((nowMs - mtimeMs) / 60_000)
    try {
      rmSync(path)
      log(`drop_stale_synthetic path=${basename(path)} age_min=${age}`)
      return null
    } catch {
      log(`drop_stale_synthetic rm_failed path=${basename(path)} age_min=${age}`)
    }
  }
  try {
    const m = JSON.parse(readFileSync(path, 'utf8'))
    return m !== null && typeof m === 'object' && !Array.isArray(m) ? m : null
  } catch { return null }
}

// ─── H-19：合成件按件内元数据判定（文件名 `hang-` 前缀只作可读性，不参与判定）─────────
/**
 * 唯一判据：件内 `synthetic === true`。传对象直接判；传路径则读该文件解析后判
 * （读不到 / 坏 JSON → false）。第二个参数（文件名）刻意忽略——名字不再是判据。
 */
export function isSynthetic(filePathOrMeta: unknown, _nameHint?: string): boolean {
  if (filePathOrMeta && typeof filePathOrMeta === 'object') {
    return (filePathOrMeta as Record<string, unknown>).synthetic === true
  }
  if (typeof filePathOrMeta === 'string') {
    try {
      const o = JSON.parse(readFileSync(filePathOrMeta, 'utf8'))
      return !!o && typeof o === 'object' && !Array.isArray(o) && (o as Record<string, unknown>).synthetic === true
    } catch { return false }
  }
  return false
}

/** 文件名里嵌的毫秒时间戳（H-19 兜底）：取最后一段 >=10 位纯数字。 */
function msFromName(name: string): number | null {
  const m = String(name).match(/\d{10,}/g)
  if (!m || m.length === 0) return null
  const n = Number(m[m.length - 1])
  return Number.isFinite(n) ? n : null
}

/**
 * H-19：合成件时间戳。件内 ts 优先（数字直取，ISO 串用 Date.parse），缺失回退文件名毫秒前缀。
 * 传对象直接判；传路径读件后判。判不出返回 null（调用方据此不误删）。
 */
export function syntheticTs(filePathOrMeta: unknown, nameHint?: string): number | null {
  let meta: any = null
  let name = typeof nameHint === 'string' ? nameHint : ''
  if (filePathOrMeta && typeof filePathOrMeta === 'object') {
    meta = filePathOrMeta
  } else if (typeof filePathOrMeta === 'string') {
    name = name || basename(filePathOrMeta)
    try { meta = JSON.parse(readFileSync(filePathOrMeta, 'utf8')) } catch { meta = null }
  }
  const ts = meta && typeof meta === 'object' ? (meta as Record<string, unknown>).ts : undefined
  if (typeof ts === 'number' && Number.isFinite(ts)) return ts
  if (typeof ts === 'string' && ts.trim()) {
    const p = Date.parse(ts)
    if (Number.isFinite(p) && p > 0) return p
  }
  return msFromName(name)
}

export type SweepSyntheticDeps = {
  dir: string
  nowMs?: number
  now?: () => number
  ttlMs?: number
  thresholdMs?: number
  log?: (line: string) => void
}

/**
 * H-19：扫 dir，把"件内 synthetic 标记为真且时间戳超过 ttl"的合成件删掉，返回删除数。
 * 时间戳来源见 syntheticTs；解析不到时间戳的件保留（宁可不删也不误删真人消息）。
 * 位置参数误用（传字符串）→ TypeError，调用方据此回退 deps 对象形态。
 */
export function sweepStaleSynthetic(deps: SweepSyntheticDeps): number {
  if (!deps || typeof deps !== 'object' || typeof (deps as SweepSyntheticDeps).dir !== 'string') {
    throw new TypeError('sweepStaleSynthetic: requires { dir: string }')
  }
  const dir = deps.dir
  const nowMs = typeof deps.nowMs === 'number' ? deps.nowMs
    : (typeof deps.now === 'function' ? deps.now() : Date.now())
  const ttlMs = typeof deps.ttlMs === 'number' ? deps.ttlMs
    : (typeof deps.thresholdMs === 'number' ? deps.thresholdMs : STALE_SYNTHETIC_MS)
  const log = typeof deps.log === 'function' ? deps.log : (l: string) => process.stderr.write(`${l}\n`)

  let names: string[] = []
  try { names = readdirSync(dir) } catch { return 0 }
  let removed = 0
  for (const name of names) {
    const path = join(dir, name)
    try { if (!statSync(path).isFile()) continue } catch { continue }
    if (!isSynthetic(path)) continue
    const ts = syntheticTs(path, name)
    if (ts === null || !Number.isFinite(ts)) continue     // 判不了新旧 → 留着
    if (nowMs - ts > ttlMs) {
      try { rmSync(path, { force: true }); removed++; log(`hang_stale_removed id=${name}`) }
      catch { /* 删不掉就留着，下一轮再说 */ }
    }
  }
  return removed
}

/**
 * 积压投递规划（纯函数）：heads = 队列头部连续 file 项（chatId=null 表示读失败/坏 JSON/陈旧已删）。
 * 从头起 null 进 drop；首个非 null 定 chatId 进 merge；其后 null 进 drop、同 chat 进 merge、换 chat 即停。
 */
export function planInboxBatch(heads: ReadonlyArray<{ path: string; chatId: string | null }>):
  { chatId: string | null; merge: string[]; drop: string[]; rest: number } {
  let chatId: string | null = null
  const merge: string[] = []
  const drop: string[] = []
  let i = 0
  for (; i < heads.length; i++) {
    const h = heads[i]
    if (h.chatId == null) { drop.push(h.path); continue }
    if (chatId == null) chatId = h.chatId
    else if (h.chatId !== chatId) break
    merge.push(h.path)
  }
  return { chatId, merge, drop, rest: heads.length - i }
}
