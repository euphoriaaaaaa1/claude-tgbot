// 守卫压缩丢了老对话后，自动给一次性私聊找回布防（scripts/recall_private.py 的 resume 钩子读这个标记）。
// 标记 <botDir>/.claude/compact-recall.armed 的内容是一行 UTC ISO 时刻：recall_private.marker_armed
// 要求普通文件、修改时刻与内容时刻都在 72 小时内，这个格式都满足。
// 开关：<botDir>/.claude/compact-recall.auto-off 存在（文件、目录、链接都算）即不自动布防。
// 本函数不抛；布防成败不影响 worker 拉起。
import { lstatSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

export type RecallArmResult = 'armed' | 'off' | 'skip' | 'failed'

export const RECALL_ARMED_NAME = 'compact-recall.armed'
export const RECALL_AUTO_OFF_NAME = 'compact-recall.auto-off'

function lexists(p: string): boolean {
  try { lstatSync(p); return true } catch { return false }
}

export function armRecallAfterCompact(botDir: string, nowMs: number = Date.now()): RecallArmResult {
  const claudeDir = join(botDir, '.claude')
  try {
    if (!lstatSync(claudeDir).isDirectory()) return 'skip'
  } catch {
    return 'skip'   // 没有 .claude 目录：不替它建
  }
  if (lexists(join(claudeDir, RECALL_AUTO_OFF_NAME))) return 'off'

  const armed = join(claudeDir, RECALL_ARMED_NAME)
  // 已有的 armed 不是普通文件（目录、链接）→ 不覆盖，不跟随链接写到别处
  try {
    if (!lstatSync(armed).isFile()) return 'failed'
  } catch { /* 不存在：正常新建 */ }

  const tmp = join(claudeDir, `.${RECALL_ARMED_NAME}.${process.pid}.${Math.random().toString(36).slice(2, 10)}.tmp`)
  try {
    writeFileSync(tmp, new Date(nowMs).toISOString() + '\n', { mode: 0o600, flag: 'wx' })
    renameSync(tmp, armed)
    return 'armed'
  } catch {
    try { unlinkSync(tmp) } catch { /* 临时文件没建出来或已不在 */ }
    return 'failed'
  }
}
