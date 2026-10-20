/**
 * /send 的 files 出口白名单。
 *
 * worker/模型能往 reply.files 里填任意字符串；不发白名单校验就会变成
 * “让 bot 把本机任意文件（.env、providers.json、/etc/passwd）发到 Telegram”。
 *
 * 规则：
 * - 只允许媒体根目录之下（生图输出、表情库、bot 的 inbox/media、朋友圈媒体）；
 * - 目标必须真实存在，且 realpath 后仍落在某个根目录之下（软链逃逸一律拒绝）；
 * - 文件本身是软链也拒绝；
 * - `.env*`、`providers.json`、路径里出现 `configs` / `secrets` 段一律拒绝。
 *
 * 纯函数、不读环境变量，方便单测。
 */
import { lstatSync, realpathSync } from 'fs'
import { basename, isAbsolute, join, resolve, sep } from 'path'

export type FileDecision = { ok: true; path: string } | { ok: false; reason: string }

const SENSITIVE_SEGMENTS = new Set(['configs', 'secrets'])

function expandHome(p: string, home: string): string {
  if (p === '~') return home
  if (p.startsWith('~/')) return join(home, p.slice(2))
  return p
}

function under(file: string, dir: string): boolean {
  const d = dir.endsWith(sep) ? dir : dir + sep
  return file.startsWith(d)
}

/**
 * 校验一个 files 项。
 * @param raw 请求里的原始值（可能是任意类型）
 * @param roots 允许的根目录（可为相对路径或 `~/...`）
 * @param home 展开 `~` 用的 home（默认取参数，测试可注入）
 */
export function resolveAllowedFile(raw: unknown, roots: readonly string[], home = ''): FileDecision {
  if (typeof raw !== 'string' || raw.trim() === '') return { ok: false, reason: 'invalid' }
  if (raw.includes('\0')) return { ok: false, reason: 'invalid' }

  const abs = isAbsolute(raw) ? resolve(raw) : resolve(expandHome(raw, home))
  const base = basename(abs)
  if (base.startsWith('.env') || base === 'providers.json') return { ok: false, reason: 'sensitive' }
  if (abs.split(sep).some(s => SENSITIVE_SEGMENTS.has(s))) return { ok: false, reason: 'sensitive' }

  let st
  try { st = lstatSync(abs) } catch { return { ok: false, reason: 'not_found' } }
  if (st.isSymbolicLink()) return { ok: false, reason: 'symlink' }

  let realFile: string
  try { realFile = realpathSync(abs) } catch { return { ok: false, reason: 'not_found' } }

  for (const root of roots) {
    if (!root) continue
    const rootAbs = isAbsolute(root) ? root : resolve(expandHome(root, home))
    let realRoot: string
    try { realRoot = realpathSync(rootAbs) } catch { continue }
    if (under(realFile, realRoot)) return { ok: true, path: realFile }
  }
  return { ok: false, reason: 'outside_roots' }
}
