/**
 * 摄像视角拍照 · 状态与解析。
 *
 * 契约：公开版 party/camera 接口（INTERFACE §6.2）第 1 节（唯一真相源）。
 * - 触发/退出只按固定规则解析，不做模糊判断；
 * - 状态文件 <根>/<BOT_NAME>/<chat_id>.json，字段形状固定；损坏/时钟回拨/满 24 小时按关闭；
 * - 轮次只在投递时经 `nextPhotoRound` 增加（burst 合并件只算一轮）。
 * 本模块不打印日志（调用方按原因码记，绝不带用户正文）。
 */
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'fs'
import { createHash } from 'node:crypto'
import { dirname, isAbsolute, join } from 'path'

export const PHOTO_STATE_VERSION = 1
export const PHOTO_TTL_MS = 86_400_000
export const CLOCK_SKEW_MS = 60_000
export const PHOTO_STATE_MAX_BYTES = 4096
export const PHOTO_DELIVERY_KEY_MAX = 512
export const PHOTO_DELIVERY_KEY_HASH_CHARS = 32

const CHAT_ID_RE = /^-?[1-9]\d{0,19}$/
const EXACT_TRIGGER_PHRASES = new Set(['拍照', '摄像', '摄影', '每轮拍照', '摄像机视角拍照', '摄影机视角拍照'])
const PHOTO_WORDS = ['拍照', '摄像', '摄影']
const INTENT_WORDS = ['视角', '机位', '每轮']
const EXIT_RE = /^(?:停|停下|停止|别拍了|不拍了|不用拍了|不要拍了|正常拍|正常拍照|结束拍照|退出拍照|停止拍照)[。！!？?，,、…~～\s]*$/

export type PhotoState = {
  v: 1
  chat_id: string
  opened_at_ms: number
  rounds: number
  last_round_at_ms: number | null
  trigger_message_id: string
  last_delivery_key: string
}

export type PhotoReadStatus = 'ok' | 'missing' | 'unreadable' | 'clock_skew' | 'expired'
export type PhotoReadResult = { state: PhotoState | null; status: PhotoReadStatus }

/** 测试时钟接缝：CLAUDEBOT_TEST=1 时 DISPATCHER_TEST_NOW_MS（有限非负数）生效，否则 Date.now()。 */
export function nowMsFromEnv(env: Record<string, string | undefined> = process.env): number {
  if (env.CLAUDEBOT_TEST === '1') {
    const raw = env.DISPATCHER_TEST_NOW_MS
    if (typeof raw === 'string' && /^\d+$/.test(raw)) {
      const n = Number(raw)
      if (Number.isSafeInteger(n)) return n
    }
  }
  return Date.now()
}

/** 拍照状态根：DISPATCHER_PHOTO_MODE_DIR（绝对路径、非空）否则 <CHANNEL_DIR>/.photo-mode。 */
export function photoModeRoot(channelDir: string, env: Record<string, string | undefined> = process.env): string {
  const configured = env.DISPATCHER_PHOTO_MODE_DIR
  if (typeof configured === 'string' && configured !== '' && isAbsolute(configured)) return configured
  return join(channelDir, '.photo-mode')
}

export function isValidPhotoChatId(chatId: unknown): chatId is string {
  return typeof chatId === 'string' && CHAT_ID_RE.test(chatId)
}

/**
 * 状态文件路径。botName 与 chatId 都来自外部输入，拼路径前必须校验，
 * 否则 `../` 之类可以逃出状态根（路径穿越）。
 */
export function photoStatePath(root: string, botName: string, chatId: string): string {
  photoBotDir(root, botName)
  if (!isValidPhotoChatId(chatId)) throw new TypeError('photoStatePath: invalid chat id')
  return join(root, botName, `${chatId}.json`)
}

/** 整条消息恰是一对同类括号（全角配全角、半角配半角）时返回括号内文本，否则 null。 */
function exactBracketContent(text: string): string | null {
  const full = /^（([^（）\n]*)）$/.exec(text)
  if (full) return full[1]!
  const half = /^\(([^()\n]*)\)$/.exec(text)
  return half ? half[1]! : null
}

/**
 * 顶层、同类配对括号的内容（按出现顺序）。嵌套括号的内层不算顶层；开闭混搭
 *（`（x)`）不算一对；跨行内容不算。纯扫描，不依赖回溯正则。
 */
export function topLevelBracketContents(text: string): string[] {
  const out: string[] = []
  const stack: Array<'(' | '（'> = []
  let start = -1
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (ch === '(' || ch === '（') {
      stack.push(ch)
      if (stack.length === 1) start = i
      continue
    }
    if (ch !== ')' && ch !== '）') continue
    if (stack.length === 0) continue
    const open = stack.pop()!
    const same = (open === '(' && ch === ')') || (open === '（' && ch === '）')
    if (stack.length > 0) continue
    const inner = start >= 0 ? text.slice(start + 1, i) : ''
    // 顶层括号内不得再有括号（并发的另一类括号或嵌套），也不得跨行
    if (same && !/[（()）\n]/.test(inner)) out.push(inner)
    start = -1
  }
  return out
}

/** 剥掉整条消息最外层的一对包裹括号（同类配对、括号内不得再有括号），再 trim。 */
export function stripWrappingParens(text: string): string {
  const trimmed = text.trim()
  const inner = exactBracketContent(trimmed)
  return inner === null ? trimmed : inner.trim()
}

/** 触发 A（意图词 + 拍照词）或 B（整条消息就是精确短语）。 */
export function parsePhotoTrigger(text: string): boolean {
  if (typeof text !== 'string') return false
  const trimmed = text.trim()
  // B：trim 后的整条消息只有一对同类括号（括号外不能再有文字），括号内去空白与末尾标点后精确等于固定短语。
  const exact = exactBracketContent(trimmed)
  if (exact !== null) {
    const inner = exact.replace(/\s+/g, '').replace(/[。！!？?，,、…~～]+$/, '')
    if (EXACT_TRIGGER_PHRASES.has(inner)) return true
  }
  // A：只认顶层括号对（嵌套内层不命中）
  for (const inner of topLevelBracketContents(trimmed)) {
    if (Array.from(inner).length > 60) continue
    const hasPhoto = PHOTO_WORDS.some(w => inner.includes(w))
    const hasIntent = INTENT_WORDS.some(w => inner.includes(w))
    if (hasPhoto && hasIntent) return true
  }
  return false
}

/** 整句退出短语（先剥一层包裹括号）。 */
export function isPhotoExit(text: string): boolean {
  if (typeof text !== 'string') return false
  return EXIT_RE.test(stripWrappingParens(text))
}

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v)

const isTimeMs = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0

/** 由文件原文解析状态；形状不对 → unreadable，时钟回拨/过期单独给出。 */
export function parsePhotoStateText(raw: string, nowMs: number): PhotoReadResult {
  let doc: unknown
  try { doc = JSON.parse(raw) } catch { return { state: null, status: 'unreadable' } }
  if (!isPlainObject(doc)) return { state: null, status: 'unreadable' }
  if (doc.v !== PHOTO_STATE_VERSION) return { state: null, status: 'unreadable' }
  if (typeof doc.chat_id !== 'string' || !CHAT_ID_RE.test(doc.chat_id)) return { state: null, status: 'unreadable' }
  if (!isTimeMs(doc.opened_at_ms)) return { state: null, status: 'unreadable' }
  if (!isTimeMs(doc.rounds)) return { state: null, status: 'unreadable' }
  if (doc.last_round_at_ms !== null && !isTimeMs(doc.last_round_at_ms)) return { state: null, status: 'unreadable' }
  if (typeof doc.trigger_message_id !== 'string') return { state: null, status: 'unreadable' }
  if (typeof doc.last_delivery_key !== 'string') return { state: null, status: 'unreadable' }
  const state: PhotoState = {
    v: 1,
    chat_id: doc.chat_id,
    opened_at_ms: doc.opened_at_ms,
    rounds: doc.rounds,
    last_round_at_ms: doc.last_round_at_ms === null ? null : doc.last_round_at_ms,
    trigger_message_id: doc.trigger_message_id,
    last_delivery_key: doc.last_delivery_key,
  }
  if (state.opened_at_ms > nowMs + CLOCK_SKEW_MS) return { state: null, status: 'clock_skew' }
  if (nowMs - state.opened_at_ms >= PHOTO_TTL_MS) return { state: null, status: 'expired' }
  return { state, status: 'ok' }
}

/** 读状态：不存在 → missing；EACCES/ENOTDIR 等非 ENOENT、超 4 KiB、坏形状 → unreadable。不做删除。 */
export function readPhotoState(path: string, nowMs: number): PhotoReadResult {
  let st
  try {
    st = statSync(path)
  } catch (e) {
    // 只有确定不存在才算 missing；权限/路径中间不是目录等一律按不可读处理（INTERFACE 1.3）
    if ((e as { code?: unknown } | null)?.code === 'ENOENT') return { state: null, status: 'missing' }
    return { state: null, status: 'unreadable' }
  }
  if (!st.isFile() || st.size > PHOTO_STATE_MAX_BYTES) return { state: null, status: 'unreadable' }
  let raw: string
  try { raw = readFileSync(path, 'utf8') } catch { return { state: null, status: 'unreadable' } }
  return parsePhotoStateText(raw, nowMs)
}

/** 触发件建立的状态：rounds=0，轮次等投递时再涨。 */
export function triggerPhotoState(chatId: string, triggerMessageId: string, nowMs: number): PhotoState {
  return {
    v: 1,
    chat_id: chatId,
    opened_at_ms: nowMs,
    rounds: 0,
    last_round_at_ms: null,
    trigger_message_id: triggerMessageId,
    last_delivery_key: '',
  }
}

/** 投递轮计数：同 key 幂等；否则 rounds+1。纯函数，返回新对象。 */
export function nextPhotoRound(state: PhotoState, deliveryKey: string, nowMs: number): PhotoState {
  if (deliveryKey === state.last_delivery_key) return state
  return { ...state, rounds: state.rounds + 1, last_round_at_ms: nowMs, last_delivery_key: deliveryKey }
}

export function photoExpiresAtMs(state: PhotoState): number {
  return state.opened_at_ms + PHOTO_TTL_MS
}

/** 投递 meta 里给 worker 的拍照状态（形状固定）。 */
export function photoMeta(state: PhotoState): { active: true; opened_at_ms: number; expires_at_ms: number } {
  return { active: true, opened_at_ms: state.opened_at_ms, expires_at_ms: photoExpiresAtMs(state) }
}

/** 投递键超长时取 sha256 前 32 个十六进制字符，保证同一合并件幂等（INTERFACE 1.4）。 */
export function normalizePhotoDeliveryKey(raw: string): string {
  if (raw === '') return ''
  return Array.from(raw).length > PHOTO_DELIVERY_KEY_MAX
    ? createHash('sha256').update(raw).digest('hex').slice(0, PHOTO_DELIVERY_KEY_HASH_CHARS)
    : raw
}

/** 按投递件 meta 生成 delivery_key：合并件用 merged_message_ids.join(',')；否则 message_id；再否则 ts。 */
export function photoDeliveryKeyFromMeta(meta: unknown): string {
  if (!isPlainObject(meta)) return ''
  const ids = Array.isArray(meta.merged_message_ids) ? meta.merged_message_ids.filter(v => v != null) : []
  const raw = ids.length > 0 ? ids.map(String).join(',')
    : meta.message_id != null ? String(meta.message_id)
    : typeof meta.ts === 'string' ? meta.ts : ''
  return normalizePhotoDeliveryKey(raw)
}

/** 原子写（tmp + rename），目录 0700、文件 0600。成功 true；失败 false（调用方记日志）。 */
export function writePhotoState(path: string, state: PhotoState): boolean {
  const dir = dirname(path)
  const tmp = `${path}.tmp-${process.pid}-${Date.now()}`
  try {
    mkdirSync(dir, { recursive: true, mode: 0o700 })
    try { chmodSync(dir, 0o700) } catch { /* 已存在且无权限改就沿用 */ }
    writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 })
    try { chmodSync(tmp, 0o600) } catch { /* mode 在创建时已给 */ }
    renameSync(tmp, path)
    return true
  } catch {
    try { rmSync(tmp, { force: true }) } catch { /* 清理失败不影响结果 */ }
    return false
  }
}

/** 删除状态（幂等）。删除失败但文件仍在 → false。 */
export function deletePhotoState(path: string): boolean {
  try {
    rmSync(path, { force: true })
  } catch {
    return false
  }
  return !existsSync(path)
}

/** bot 目录名只允许字母、数字、下划线与短横，1 到 64 个字符；其余一律拒绝，防路径穿越。 */
const BOT_DIR_RE = /^[A-Za-z0-9_-]{1,64}$/

/** 文件名里的 bot 目录（BOT_NAME 只取 dispatcher 启动 env，与频道目录身份同源）。 */
export function photoBotDir(root: string, botName: string): string {
  if (typeof botName !== 'string' || !BOT_DIR_RE.test(botName)) {
    throw new TypeError('photoBotDir: invalid bot name')
  }
  return join(root, botName)
}
