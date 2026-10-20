/**
 * 单 bot 暂停标记读取闸。
 *
 * 与公开版 `pause.py` 使用同一组文件（dispatcher 与 Python 侧行为一致）：
 * - 全局：`~/.claudebotlife.pause`，存在即所有 bot 暂停；
 * - 单 bot：`~/.claudebotlife.pause-<bot>`，存在即该 bot 暂停。
 * 返回与 dispatcher isPaused 同形的原因码；都未暂停 → null。
 * 路径可用 PAUSE_FILE_GLOBAL / PAUSE_FILE_BOT_DIR 覆盖（只给测试与隔离部署用）。
 */
import { existsSync } from 'fs'
import { homedir } from 'os'
import { join } from 'path'

export function makePauseCheck(
  botName: string,
  env: Record<string, string | undefined> = process.env,
): () => string | null {
  const home = env.HOME || homedir()
  const globalPath = env.PAUSE_FILE_GLOBAL || join(home, '.claudebotlife.pause')
  const botDir = env.PAUSE_FILE_BOT_DIR || home
  const botPath = join(botDir, `.claudebotlife.pause-${botName}`)
  return () => {
    try { if (existsSync(globalPath)) return 'global' } catch { /* 读不到按未暂停 */ }
    try { if (botName && existsSync(botPath)) return `bot:${botName}` } catch { /* 同上 */ }
    return null
  }
}
