/**
 * 线下聚会模式 · 状态与判定。
 *
 * 契约：公开版 party/camera 接口（INTERFACE §6.2）第 2 节（唯一真相源）。
 * - 文件 <根>/<群号>.json 存在且 on=true 即开启；群级共享，文件名不含 bot；
 * - 三入口（bot 工具 / director / 管理台）写同一形状，last-writer-wins；
 * - 损坏/时钟回拨/满 6 小时按关闭；本模块不打印日志（调用方按原因码记）。
 */
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'fs'
import { homedir } from 'os'
import { dirname, isAbsolute, join } from 'path'
import { nowMsFromEnv } from './photo_mode'

export const PARTY_STATE_VERSION = 1
export const PARTY_TTL_MS = 21_600_000
export const CLOCK_SKEW_MS = 60_000
export const PARTY_STATE_MAX_BYTES = 4096
export const PARTY_EXCLUDE_MAX = 20
export const PARTY_NOTE_MAX = 80

const GROUP_ID_RE = /^-[1-9]\d{0,19}$/
const EXCLUDE_RE = /^[a-z][a-z0-9_]{1,31}$/
const PARTY_SOURCES = new Set(['bot', 'director', 'console'])
const END_RE = /^(?:聚会结束|结束聚会|聚会散了|我们散了|散了|线下聚会结束)[。！!？?，,、…~～\s]*$/
const WRAPPING_PARENS_RE = /^[（(]([^（()）\n]*)[）)]$/
const OPT_OUT_PREFIXES = ['私聊：', '私聊:', '私下：', '私下:']

export type PartySource = 'bot' | 'director' | 'console'
export type InboundSenderRecord = { senderId: string; atMs: number }
const MESSAGE_ID_RE = /^[1-9]\d{0,19}$/
export type PartyState = {
  v: 1
  chat_id: string
  on: true
  source: PartySource
  opened_at_ms: number
  exclude: string[]
  note: string
}
export type PartyReadStatus = 'ok' | 'missing' | 'unreadable' | 'clock_skew' | 'expired'
export type PartyReadResult = { state: PartyState | null; status: PartyReadStatus }

export type PartyGroupState = { chat_id: string; state: PartyState }
export type ParticipatingGroups = {
  groups: PartyGroupState[]
  invalid: Array<{ chat_id: string; status: Exclude<PartyReadStatus, 'ok' | 'missing'> }>
  /** 配置里非法群号键：跳过，不拼路径；调用方记原因码。 */
  invalidNames: string[]
}

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v)
const isTimeMs = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0

/** 聚会状态根：DISPATCHER_PARTY_DIR（绝对路径、非空）否则 <DP>/.party-mode（DP=~/.claude/dispatcher）。 */
export function partyModeRoot(env: Record<string, string | undefined> = process.env): string {
  const configured = env.DISPATCHER_PARTY_DIR
  if (typeof configured === 'string' && configured !== '' && isAbsolute(configured)) return configured
  return join(homedir(), '.claude', 'dispatcher', '.party-mode')
}

export function isValidGroupId(chatId: unknown): chatId is string {
  return typeof chatId === 'string' && GROUP_ID_RE.test(chatId)
}

/** 规范化 /party 的 chat_id：字符串 trim 或有限数字转字符串；不合群号格式 → null。 */
export function normalizePartyChatId(value: unknown): string | null {
  let s: string
  if (typeof value === 'string') s = value.trim()
  else if (typeof value === 'number' && Number.isFinite(value)) s = String(value)
  else return null
  return GROUP_ID_RE.test(s) ? s : null
}

/** note：缺省空串；≤80 字符（按码点）、不含换行。 */
export function normalizePartyNote(value: unknown): { ok: true; note: string } | { ok: false } {
  if (value === undefined || value === null) return { ok: true, note: '' }
  if (typeof value !== 'string') return { ok: false }
  if (/[\r\n]/.test(value)) return { ok: false }
  if (Array.from(value).length > PARTY_NOTE_MAX) return { ok: false }
  return { ok: true, note: value }
}

/** exclude：字符串数组、每项 ^[a-z][a-z0-9_]{1,31}$；先按原始列表判长度（>20 不合法），再去重。 */
export function normalizePartyExclude(value: unknown): { ok: true; exclude: string[] } | { ok: false } {
  if (!Array.isArray(value)) return { ok: false }
  // 与 CBL 一致：长度看原始列表，重复项不能把非法长度“洗”成合法
  if (value.length > PARTY_EXCLUDE_MAX) return { ok: false }
  const out: string[] = []
  for (const item of value) {
    if (typeof item !== 'string' || !EXCLUDE_RE.test(item)) return { ok: false }
    if (!out.includes(item)) out.push(item)
  }
  return { ok: true, exclude: out }
}

/** 群级状态文件路径。群号必须在 join 前通过 INTERFACE 2.1 的格式校验，非法值抛固定错误（不带输入）。 */
export function partyStatePath(root: string, chatId: string): string {
  if (!isValidGroupId(chatId)) throw new TypeError('partyStatePath: invalid group id')
  return join(root, `${chatId}.json`)
}

/** owner 唯一来源：access.json.allowFrom[0]；缺失/非字符串/空串 → ''。 */
export function ownerOf(access: unknown): string {
  if (!isPlainObject(access) || !Array.isArray(access.allowFrom)) return ''
  const first = access.allowFrom[0]
  return typeof first === 'string' && first !== '' ? first : ''
}

export function isPartyOptOutPrefix(text: string): boolean {
  if (typeof text !== 'string') return false
  const trimmed = text.trim()
  return OPT_OUT_PREFIXES.some(p => trimmed.startsWith(p))
}

/** 整句结束短语（先剥一层包裹括号）。 */
export function isPartyEndPhrase(text: string): boolean {
  if (typeof text !== 'string') return false
  const trimmed = text.trim()
  const m = WRAPPING_PARENS_RE.exec(trimmed)
  return END_RE.test(m ? m[1]!.trim() : trimmed)
}

/** 由文件原文解析；形状不对 → unreadable，时钟回拨/过期单独给出。 */
export function parsePartyStateText(raw: string, nowMs: number): PartyReadResult {
  let doc: unknown
  try { doc = JSON.parse(raw) } catch { return { state: null, status: 'unreadable' } }
  if (!isPlainObject(doc)) return { state: null, status: 'unreadable' }
  if (doc.v !== PARTY_STATE_VERSION) return { state: null, status: 'unreadable' }
  if (typeof doc.chat_id !== 'string' || !GROUP_ID_RE.test(doc.chat_id)) return { state: null, status: 'unreadable' }
  if (doc.on !== true) return { state: null, status: 'unreadable' }
  if (typeof doc.source !== 'string' || !PARTY_SOURCES.has(doc.source)) return { state: null, status: 'unreadable' }
  if (!isTimeMs(doc.opened_at_ms)) return { state: null, status: 'unreadable' }
  const ex = normalizePartyExclude(doc.exclude)
  if (!ex.ok) return { state: null, status: 'unreadable' }
  const note = normalizePartyNote(doc.note)
  if (!note.ok) return { state: null, status: 'unreadable' }
  const state: PartyState = {
    v: 1,
    chat_id: doc.chat_id,
    on: true,
    source: doc.source as PartySource,
    opened_at_ms: doc.opened_at_ms,
    exclude: ex.exclude,
    note: note.note,
  }
  if (state.opened_at_ms > nowMs + CLOCK_SKEW_MS) return { state: null, status: 'clock_skew' }
  if (nowMs - state.opened_at_ms >= PARTY_TTL_MS) return { state: null, status: 'expired' }
  return { state, status: 'ok' }
}

/** 读状态：不存在 → missing；EACCES/ENOTDIR 等非 ENOENT、超 4 KiB、坏形状 → unreadable。不做删除。 */
export function readPartyState(path: string, nowMs: number): PartyReadResult {
  let st
  try {
    st = statSync(path)
  } catch (e) {
    // 只有确定不存在才算 missing；权限/路径中间不是目录等一律按不可读处理（INTERFACE 2.1）
    if ((e as { code?: unknown } | null)?.code === 'ENOENT') return { state: null, status: 'missing' }
    return { state: null, status: 'unreadable' }
  }
  if (!st.isFile() || st.size > PARTY_STATE_MAX_BYTES) return { state: null, status: 'unreadable' }
  let raw: string
  try { raw = readFileSync(path, 'utf8') } catch { return { state: null, status: 'unreadable' } }
  return parsePartyStateText(raw, nowMs)
}

export function newPartyState(chatId: string, source: PartySource, note: string, nowMs: number, exclude: string[] = []): PartyState {
  return { v: 1, chat_id: chatId, on: true, source, opened_at_ms: nowMs, exclude: [...exclude], note }
}

/** 重复开启：保留首次的 source/opened_at_ms；note/exclude 仅在显式给出时更新。 */
export function refreshPartyState(existing: PartyState, changes: { note?: string; exclude?: string[] }): PartyState {
  return {
    ...existing,
    exclude: changes.exclude !== undefined ? [...changes.exclude] : [...existing.exclude],
    note: changes.note !== undefined ? changes.note : existing.note,
  }
}

/** 原子写（tmp + rename），目录 0700、文件 0600。成功 true；失败 false。 */
export function writePartyState(path: string, state: PartyState): boolean {
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

/** 关闭（删文件，幂等）。删除失败且文件仍在 → false。 */
export function deletePartyState(path: string): boolean {
  try {
    rmSync(path, { force: true })
  } catch {
    return false
  }
  return !existsSync(path)
}

export function partyStateResponse(state: PartyState): Pick<PartyState, 'on' | 'source' | 'opened_at_ms' | 'exclude' | 'note'> {
  return { on: true, source: state.source, opened_at_ms: state.opened_at_ms, exclude: [...state.exclude], note: state.note }
}

/**
 * 这只 bot 对哪些群参与聚会。
 * 条件：状态有效未过期、群在该 bot access.groups、BOT_NAME 不在 exclude、groups 功能开、owner 非空。
 * groups 功能关或 owner 缺失 → 不读文件、不删状态，直接返回空（PTY-15）。
 * 坏状态只报告、不在这里删（调用方决定并记原因码）。
 */
export function participatingPartyGroups(opts: {
  partyRoot: string
  accessGroups: unknown
  botName: string
  owner: string
  groupsFeatureOn: boolean
  nowMs: number
}): ParticipatingGroups {
  const out: ParticipatingGroups = { groups: [], invalid: [], invalidNames: [] }
  if (!opts.groupsFeatureOn || opts.owner === '') return out
  if (!isPlainObject(opts.accessGroups)) return out
  for (const chatId of Object.keys(opts.accessGroups)) {
    // 非法群号不拼路径（partyStatePath 也会拒绝）；跳过并交给调用方记原因码
    if (!GROUP_ID_RE.test(chatId)) { out.invalidNames.push(chatId); continue }
    const r = readPartyState(partyStatePath(opts.partyRoot, chatId), opts.nowMs)
    if (r.status === 'ok' && r.state) {
      if (r.state.exclude.includes(opts.botName)) continue
      out.groups.push({ chat_id: chatId, state: r.state })
    } else if (r.status !== 'missing') {
      out.invalid.push({ chat_id: chatId, status: r.status })
    }
  }
  return out
}

/** owner 发言窗口：0 <= now - 时刻 <= windowMs；缺失、未来时刻、超窗都无效（INTERFACE 2.4）。 */
export function ownerWindowOpen(nowMs: number, ownerSpokeAtMs: unknown, windowMs: number): boolean {
  if (typeof ownerSpokeAtMs !== 'number' || !Number.isFinite(ownerSpokeAtMs) || !Number.isFinite(nowMs) || !Number.isFinite(windowMs)) return false
  const age = nowMs - ownerSpokeAtMs
  return age >= 0 && age <= windowMs
}

/**
 * /party 的 message_id：单个数字/字符串，或 burst 合并件的 message id 列表（逗号分隔）。
 * 空串/非法/超过 200 项 → null（调用方按 unknown 处理，403 owner_required）。
 */
export function parsePartyMessageIds(raw: unknown): string[] | null {
  if (typeof raw === 'number') {
    return Number.isSafeInteger(raw) && raw > 0 ? [String(raw)] : null
  }
  if (typeof raw !== 'string') return null
  const s = raw.trim()
  if (s === '') return null
  const ids = s.split(',')
  if (ids.length > 200) return null
  for (const id of ids) if (!MESSAGE_ID_RE.test(id)) return null
  return ids
}

/**
 * 触发 /party 的入站件（合并件为全部入站）的真人发送者必须全是 owner。
 * ledger 的键是 `${chatId}|${messageId}`（dispatcher 在 gate 后按已路由真人件写入）。
 */
export function inboundItemSendersAllOwner(
  ledger: Map<string, InboundSenderRecord>,
  chatId: string,
  raw: unknown,
  owner: string,
): boolean {
  if (owner === '' || !(ledger instanceof Map)) return false
  const ids = parsePartyMessageIds(raw)
  if (ids === null) return false
  for (const id of ids) {
    if (ledger.get(`${chatId}|${id}`)?.senderId !== owner) return false
  }
  return true
}

/** 测试时钟接缝的再导出，调用方只 import 一个模块即可。 */
export { nowMsFromEnv }

/**
 * 当前投递件的 reply 默认目标（INTERFACE 2.7）：
 * 显式非空 chat_id 优先；当前件带 party_chat_id 且非 ambiguous → 群 G；
 * 否则当前件所在聊天（群聊/合成/普通私聊都一样）。不跨投递件复用改投目标。
 */
export function replyTargetForDelivery(explicitChatId: unknown, meta: unknown): string {
  const explicit = explicitChatId == null ? '' : String(explicitChatId)
  if (explicit !== '') return explicit
  const m = isPlainObject(meta) ? meta : {}
  const party = typeof m.party_chat_id === 'string' && m.party_chat_id !== '' ? m.party_chat_id : ''
  if (party !== '' && m.party_ambiguous !== true) return party
  return m.chat_id != null ? String(m.chat_id) : ''
}
