/**
 * 读 voice-bridge 共享令牌，构造 Authorization 头。
 *
 * 令牌文件由 install.sh 生成：`<secrets>/voice-bridge.token`（0600）。
 * 文件缺失/不可读 → 返回空头，让请求按服务端默认策略失败；绝不回退成“无鉴权重试”。
 */
import { readFileSync } from 'fs'
import { homedir } from 'os'
import { join } from 'path'

export const DEFAULT_TOKEN_FILE = '~/.claude/secrets/voice-bridge.token'

export function expandHome(p: string, home = homedir()): string {
  if (p === '~') return home
  if (p.startsWith('~/')) return join(home, p.slice(2))
  return p
}

export function voiceBridgeAuthHeaders(
  env: Record<string, string | undefined> = process.env,
  read: (path: string) => string = p => readFileSync(p, 'utf8'),
  home = homedir(),
): Record<string, string> {
  const path = env.VOICE_BRIDGE_TOKEN_FILE || DEFAULT_TOKEN_FILE
  let token = ''
  try { token = read(expandHome(path, home)).trim() } catch { return {} }
  if (!token) return {}
  return { Authorization: `Bearer ${token}` }
}
