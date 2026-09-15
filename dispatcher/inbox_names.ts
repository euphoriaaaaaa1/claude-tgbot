/**
 * inbox 文件名约定（desync ⑤）：合成消息的文件名前缀集合只在这里定义一次。
 * bot 停用期间 self_initiate / director / moments / voicecall 照写 inbox，启用后这些陈旧合成件
 * 不能补发——pump() 读队头时按文件名 + mtime 丢掉它们；真人消息文件再旧也照投。
 * 契约见 INTERFACE-desync §9.5 §10.5。
 */
import { readFileSync, rmSync, statSync } from 'fs'
import { basename } from 'path'

export const SYNTHETIC_FILE_PREFIXES = ['self-', 'director-', 'dm-nudge-', 'moment-', 'hang-', 'voice-']
export const STALE_SYNTHETIC_MS = 30 * 60_000

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
