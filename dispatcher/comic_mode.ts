/**
 * 漫画模式 · 解析与状态根。
 *
 * 契约：整条消息恰为一对同类括号、括号内是 漫画/分镜/四格/漫画模式 之一时开启；退出词剥一层括号后整句匹配；
 * 状态文件 <根>/<BOT_NAME>/<chat_id>.json，根为 DISPATCHER_COMIC_MODE_DIR 或 <CHANNEL_DIR>/.comic-mode。
 * 状态的读写删、计轮、meta、投递键、chat_id 校验全部复用 photo_mode.ts（形状同拍照：v=1、24 小时、
 * 60 秒时钟偏差），这里只放漫画特有的部分。
 * 本模块不打印日志（调用方按原因码记，绝不带用户正文）。
 */
import { chmodSync, mkdirSync, renameSync, rmSync, statSync, writeFileSync } from 'fs'
import { dirname, isAbsolute, join } from 'path'
import { photoBotDir, stripWrappingParens, type PhotoState } from './photo_mode'

const COMIC_TRIGGER_PHRASES = new Set(['漫画', '分镜', '四格', '漫画模式'])
const COMIC_EXIT_RE = /^(?:停|不画了|结束漫画|退出漫画|正常聊)[。！!？?，,、…~～\s]*$/

/** 漫画状态根：DISPATCHER_COMIC_MODE_DIR（绝对路径、非空）否则 <CHANNEL_DIR>/.comic-mode。 */
export function comicModeRoot(channelDir: string, env: Record<string, string | undefined> = process.env): string {
  const configured = env.DISPATCHER_COMIC_MODE_DIR
  if (typeof configured === 'string' && configured !== '' && isAbsolute(configured)) return configured
  return join(channelDir, '.comic-mode')
}

/**
 * 拍照与漫画共用：bot 名不合规时两种模式都不读不写状态。
 * 口径与 photoBotDir 完全一致（状态路径最终由它拼），否则名字过了这里却在 photoStatePath 抛错。
 */
export function isValidModeBotName(botName: unknown): botName is string {
  if (typeof botName !== 'string') return false
  try {
    photoBotDir('/', botName)
    return true
  } catch {
    return false
  }
}

/** 整条消息（已 trim）恰是一对同类括号（全角配全角、半角配半角，内部无括号无换行）时返回括号内文本，否则 null。 */
export function wholeBracketContent(text: string): string | null {
  const full = /^（([^（）()\n]*)）$/.exec(text)
  if (full) return full[1]!
  const half = /^\(([^（）()\n]*)\)$/.exec(text)
  return half ? half[1]! : null
}

/** 触发：整条消息恰为一对同类括号，括号内去空白与末尾标点后精确等于 漫画/分镜/四格/漫画模式。 */
export function parseComicTrigger(text: unknown): boolean {
  if (typeof text !== 'string') return false
  const inner = wholeBracketContent(text.trim())
  if (inner === null) return false
  return COMIC_TRIGGER_PHRASES.has(inner.replace(/\s+/g, '').replace(/[。！!？?，,、…~～]+$/, ''))
}

/**
 * 漫画状态原子写（tmp + rename），目录 0700、文件 0600，形状同拍照。
 * 与 writePhotoState 唯一的区别：已存在的目录权限只收紧、不放宽。writePhotoState 会把目录强制改回 0700，
 * 运维把目录设成只读（0500）想停写时会被它自己撤销；漫画按"写不进就记 comic_state_write_failed、本轮不标记"处理。
 */
export function writeComicState(path: string, state: PhotoState): boolean {
  const dir = dirname(path)
  const tmp = `${path}.tmp-${process.pid}-${Date.now()}`
  try {
    mkdirSync(dir, { recursive: true, mode: 0o700 })
    const mode = statSync(dir).mode & 0o777
    if (mode & 0o077) chmodSync(dir, mode & 0o700)
    writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 })
    try { chmodSync(tmp, 0o600) } catch { /* mode 在创建时已给 */ }
    renameSync(tmp, path)
    return true
  } catch {
    try { rmSync(tmp, { force: true }) } catch { /* 清理失败不影响结果 */ }
    return false
  }
}

/** 退出：剥一层包裹括号后整句匹配退出短语；裸词单发也算，句中出现不算。 */
export function isComicExit(text: unknown): boolean {
  if (typeof text !== 'string') return false
  return COMIC_EXIT_RE.test(stripWrappingParens(text))
}
