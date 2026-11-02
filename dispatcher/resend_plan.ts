// resend-after-recovery：线路恢复后提醒 bot 补发断线期间私聊里确定没发出的段。
// dispatcher 是唯一写者：/send 收尾调 resendRecord 记段，10 秒一次的线路巡检调 resendTickAll 判投件。
// 判定全是纯函数；读写全用同步 fs、全程无 await（Bun 单线程下 record 与 tick 的读→改→写不会交错）。
// 段号与 /send 回包 parts[].index 同号，从 0 起。
import { createHash } from 'node:crypto'
import { chmodSync, existsSync, mkdirSync, readdirSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { INTERNAL_TEXT_MARKERS } from './chat_guard'

/** /send 一段的补发视图（dispatcher 内部，不进回包）；index 与回包 parts[].index 同号，从 0 起 */
export type SendPart = { index: number; kind: 'text' | 'voice' | 'photo' | 'document'
  outcome: 'sent' | 'failed' | 'unknown' | 'skipped'; reason?: string; fallback?: 'text' }

export const RESEND_PREFIX = '[resend-check]'
export const RESEND_SETTLE_MS = 120_000, RESEND_AWAIT_MS = 15 * 60_000
export const RESEND_TTL_MS = 12 * 3600_000, RESEND_MAX_ROUNDS = 3
// 前缀必须登记在内部标记清单里，否则真人正文里的同名标记不会被转义
if (!INTERNAL_TEXT_MARKERS.includes(RESEND_PREFIX)) throw new Error('resend_plan: RESEND_PREFIX 未登记到 INTERNAL_TEXT_MARKERS')

/** 与 parts 等长同序：文字段 {text: 气泡原文}；纯语音段 {text: 念稿}；图片/文件段 {path}；配字幕语音段 null（不入选） */
export type PartSource = { text?: string; path?: string } | null
/** at_ms = 最近一次失败（稳定期与过期都从这一刻算）；first_ms = 首次失败（提醒文案里的时刻），只在再次失败后才有 */
export type PendingPart = { key: string; kind: SendPart['kind']; text?: string; path?: string; reason: 'link_down' | 'network'; at_ms: number; first_ms?: number }
export type ResendState = { v: 2; chat_id: string; epoch: number; parts: PendingPart[]; batch_ms: number | null
  rounds: number; injected_ms: number | null; inbox_key: string | null; taken_ms: number | null; listed_total: number
  unknown_keys: Record<string, number>; cooldown_until_ms: number | null }
export type TickCtx = { nowMs: number; linkUp: boolean; linkUpSinceMs: number | null; blocked: boolean; fileExists: (p: string) => boolean }
type TickAction = 'none' | 'inject' | 'done' | 'give_up' | 'void' | 'expired'
type TickResult = { action: TickAction; state: ResendState | null
  inject?: { round: number; inboxKey: string; messageId: string; parts: PendingPart[]; omitted: number }; removeInboxKey?: string }
export type ResendEvent = { kind: string; added: number; pending: number }

const KINDS = new Set(['text', 'voice', 'photo', 'document'])
const STORED_TEXT_MAX = 500
const MESSAGE_MAX = 2500
const SHORTENED_MAX = 60
const ELLIPSIS = '（后略）'
const SPAN_MS = 10 * 60_000
const LIST_MAX = 20 // 一条提醒最多列的段数，超出的视为已列出、只在末尾报个数

export function isResendChatId(chatId: string): boolean {
  return typeof chatId === 'string' && /^\d+$/.test(chatId)
}

// 段号只取非负整数（与 /send 回包同号，从 0 起）
function partIdx(p: SendPart): number | null {
  const raw = (p as { index?: unknown }).index
  return typeof raw === 'number' && Number.isInteger(raw) && raw >= 0 ? raw : null
}

/** 内容键：kind 与内容之间带分隔符，避免 'ab'+'c' 与 'a'+'bc' 撞键（S5） */
export function contentKey(kind: string, content: string): string {
  return createHash('sha256').update(kind + '\u0000' + content).digest('hex').slice(0, 16)
}

function sourceContent(kind: string, src: PartSource): string | null {
  if (!src || typeof src !== 'object') return null
  const v = kind === 'photo' || kind === 'document' ? src.path : kind === 'text' || kind === 'voice' ? src.text : undefined
  return typeof v === 'string' && v !== '' ? v : null
}

function keyOf(part: SendPart, src: PartSource): string | null {
  const c = sourceContent(part.kind, src)
  return c === null ? null : contentKey(part.kind, c)
}

/** 确定没送达才入选：link_down 任何类型；network 只认非回退的文字段（I1） */
export function eligible(part: SendPart, src: PartSource): boolean {
  if (!part || part.outcome !== 'failed' || !KINDS.has(part.kind)) return false
  if (sourceContent(part.kind, src) === null) return false
  if (part.reason === 'link_down') return true
  return part.reason === 'network' && part.kind === 'text' && part.fallback === undefined
}

/** 线路连续通的起点：不通/未知→通 记 nowMs，通→通 不变，→不通 记 null（I5） */
export function nextLinkUpSince(prev: number | null, upNow: boolean, nowMs: number): number | null {
  if (!upNow) return null
  return prev ?? nowMs
}

const clone = (s: ResendState): ResendState => JSON.parse(JSON.stringify(s))

function emptyState(chatId: string, epoch: number): ResendState {
  return { v: 2, chat_id: chatId, epoch, parts: [], batch_ms: null, rounds: 0, injected_ms: null, inbox_key: null,
    taken_ms: null, listed_total: 0, unknown_keys: {}, cooldown_until_ms: null }
}

/** 清批：只清 parts 与轮次字段，unknown_keys 与冷却保留到各自过期（F2） */
function clearBatch(s: ResendState): ResendState {
  return { ...s, parts: [], batch_ms: null, rounds: 0, injected_ms: null, inbox_key: null, taken_ms: null, listed_total: 0 }
}

function pruneKeys(s: ResendState, now: number): void {
  for (const [k, at] of Object.entries(s.unknown_keys)) if (now - at >= RESEND_TTL_MS) delete s.unknown_keys[k]
  if (s.cooldown_until_ms !== null && now >= s.cooldown_until_ms) s.cooldown_until_ms = null
}

const isEmptyState = (s: ResendState): boolean =>
  s.parts.length === 0 && s.batch_ms === null && Object.keys(s.unknown_keys).length === 0 && s.cooldown_until_ms === null

const firstAt = (p: PendingPart): number => p.first_ms ?? p.at_ms
const cutChars = (s: string, n: number): string => Array.from(s).slice(0, n).join('')
const charLen = (s: string): number => Array.from(s).length

function pendingFrom(part: SendPart, src: PartSource, key: string, now: number): PendingPart {
  const content = sourceContent(part.kind, src) as string
  const reason = part.reason as 'link_down' | 'network'
  if (part.kind === 'photo' || part.kind === 'document') return { key, kind: part.kind, path: content, reason, at_ms: now }
  // 只存前 500 字；超出的截断时就带上「（后略）」，提醒里照此显示
  const text = charLen(content) > STORED_TEXT_MAX ? cutChars(content, STORED_TEXT_MAX) + ELLIPSIS : content
  return { key, kind: part.kind, text, reason, at_ms: now }
}

/** 并入一次 /send 的结果；scheduled = 本次已入批的段号（调用方写盘成功才可外报）。永不抛 */
export function applySendResult(prev: ResendState | null, chatId: string, parts: SendPart[], sources: PartSource[],
  ctx: { nowMs: number; epoch: number }): { state: ResendState | null; scheduled: number[]; event: { kind: string; [k: string]: unknown } } {
  try {
    if (!isResendChatId(chatId)) return { state: prev, scheduled: [], event: { kind: 'bad_chat' } }
    const now = ctx.nowMs
    let st = prev ? clone(prev) : emptyState(chatId, ctx.epoch)
    pruneKeys(st, now)
    // 会话被清后冷却随之作废：新会话里的失败可以开新批（#9）
    if (st.epoch !== ctx.epoch) st.cooldown_until_ms = null
    let voided = false
    if (st.batch_ms !== null && st.epoch !== ctx.epoch) { st = clearBatch(st); voided = true }
    if (st.batch_ms === null) st.epoch = ctx.epoch
    const items = (Array.isArray(parts) ? parts : []).map((p, i) => ({ p, src: (Array.isArray(sources) ? sources[i] : null) ?? null }))
      .map((x) => ({ ...x, key: x.p ? keyOf(x.p, x.src) : null, idx: x.p ? partIdx(x.p) : null }))
    // ① 状态不明的内容 12 小时内不补，已在待补里的同键段剔除
    let unknownAdded = 0
    for (const x of items) if (x.p?.outcome === 'unknown' && x.key) { st.unknown_keys[x.key] = now; unknownAdded++ }
    // ② 同内容后来发出去了（worker 自己原样补上），移出待补；出现 sent 就解除冷却（I3）
    const before = st.parts.length
    const sentKeys = new Set(items.filter((x) => x.p?.outcome === 'sent' && x.key).map((x) => x.key as string))
    if (items.some((x) => x.p?.outcome === 'sent')) st.cooldown_until_ms = null
    const sentRemoved = st.parts.filter((pp) => sentKeys.has(pp.key)).length
    st.parts =st.parts.filter((pp) => !sentKeys.has(pp.key) && !(pp.key in st.unknown_keys))
    const removed = before - st.parts.length
    // ③ 入选段：冷却中不开新批也不加段；同键只留一条（同一次里的重复段不外报段号）
    const scheduled: number[] = []
    let added = 0
    const unknownSkipped = items.filter((x) => x.p && x.key && eligible(x.p, x.src) && x.key in st.unknown_keys).length
    // 已投满 3 轮（等待期或件未取走）：段照记进 parts 供下一次巡检判 give_up，但不会再有提醒，不外报段号（#26）
    const exhausted = st.batch_ms !== null && st.inbox_key !== null && st.rounds >= RESEND_MAX_ROUNDS
    if (st.cooldown_until_ms === null) {
      const seen = new Set<string>()
      for (const x of items) {
        if (!x.p || !x.key || x.idx === null || !eligible(x.p, x.src) || x.key in st.unknown_keys || seen.has(x.key)) continue
        seen.add(x.key)
        const existing = st.parts.find((pp) => pp.key === x.key)
        if (!existing) { st.parts.push(pendingFrom(x.p, x.src, x.key, now)); added++ }
        else {
          // 同一内容又失败：稳定期与过期从这次算，提醒里的时刻仍是首次失败（#1）
          if (existing.first_ms === undefined) existing.first_ms = existing.at_ms
          existing.at_ms = now
        }
        if (!exhausted) scheduled.push(x.idx)
      }
    }
    if (st.parts.length > 0 && st.batch_ms === null) { st.batch_ms = now; st.epoch = ctx.epoch }
    scheduled.sort((a, b) => a - b)
    const anyEligible = items.some((x) => x.p && eligible(x.p, x.src))
    const kind = scheduled.length ? 'recorded' : st.cooldown_until_ms !== null && anyEligible ? 'cooldown' : exhausted && anyEligible ? 'exhausted' : 'none'
    return { state: isEmptyState(st) ? null : st, scheduled, event: { kind, added, removed, unknown: unknownAdded, unknown_skipped: unknownSkipped, sent_removed: sentRemoved, voided, pending: st.parts.length } }
  } catch (e) {
    return { state: prev, scheduled: [], event: { kind: 'error', error: (e as Error)?.name ?? 'Error' } }
  }
}

/** 巡检决策：先做过期维护，再判作废、取件、done、give_up、投件。永不抛 */
export function decideResendTick(s: ResendState, ctx: { nowMs: number; epoch: number; linkUp: boolean
  linkUpSinceMs: number | null; blocked: boolean; inboxPresent: boolean }): TickResult {
  const now = ctx.nowMs
  const finish = (st: ResendState, action: TickAction, extra: Partial<TickResult> = {}): TickResult =>
    ({ action, state: isEmptyState(st) ? null : st, ...extra })
  try {
    let st = clone(s)
    // 与闸门无关的维护：暂停、线路不通、开关关时照样执行（I11）
    pruneKeys(st, now)
    const before = st.parts.length
    st.parts = st.parts.filter((p) => now - p.at_ms < RESEND_TTL_MS)
    const partsExpired = st.parts.length < before
    const present = st.inbox_key !== null && ctx.inboxPresent === true
    const removeKey = present ? { removeInboxKey: st.inbox_key as string } : {}
    if (st.batch_ms === null) return finish(st, partsExpired ? 'expired' : 'none')
    // 会话被清（I2）：整批作废，件还在就撤回
    if (st.epoch !== ctx.epoch) return finish({ ...clearBatch(st), epoch: ctx.epoch }, 'void', removeKey)
    // 件 12 小时都没被取走（worker 起不来）：撤回，免得隔夜投出（I4）
    if (st.injected_ms !== null && present && now - st.injected_ms >= RESEND_TTL_MS) return finish(clearBatch(st), 'expired', removeKey)
    if (st.parts.length === 0 && st.inbox_key === null) return finish(clearBatch(st), partsExpired ? 'expired' : 'none')
    if (st.inbox_key !== null) {
      if (present) return finish(st, 'none')                // 件还在 inbox：等，不计时
      if (st.taken_ms === null) st.taken_ms = now           // 消失的第一刻记为取走
      if (st.parts.length === 0) {
        return now - st.taken_ms >= RESEND_AWAIT_MS ? finish(clearBatch(st), 'done') : finish(st, 'none')
      }
      if (st.rounds >= RESEND_MAX_ROUNDS) return finish({ ...clearBatch(st), cooldown_until_ms: now + RESEND_TTL_MS }, 'give_up')
    }
    // 投件闸：线路通、恢复与最后失败都过了稳定期、不在暂停、不在冷却
    const lastAt = Math.max(...st.parts.map((p) => p.at_ms))
    const settled = ctx.linkUpSinceMs !== null && now - Math.max(lastAt, ctx.linkUpSinceMs) >= RESEND_SETTLE_MS
    if (!ctx.linkUp || !settled || ctx.blocked || st.cooldown_until_ms !== null) return finish(st, partsExpired ? 'expired' : 'none')
    const round = st.rounds + 1
    // #8：只列首次失败最早的 20 段（sort 是稳定排序，同刻按入库先后）；其余只报个数，同样视为已列出
    const ordered = [...st.parts].sort((a, b) => firstAt(a) - firstAt(b))
    const listed = ordered.slice(0, LIST_MAX)
    const inject = { round, inboxKey: `resend${st.batch_ms}r${round}`, messageId: `${st.batch_ms}${round}`, parts: listed, omitted: ordered.length - listed.length }
    // F1：本轮列出的段移出 parts，等待期内只收新失败
    st = { ...st, rounds: round, injected_ms: now, inbox_key: inject.inboxKey, taken_ms: null, parts: [], listed_total: st.listed_total + ordered.length }
    return finish(st, 'inject', { inject })
  } catch {
    return { action: 'none', state: s }
  }
}

function pad2(n: number): string { return String(n).padStart(2, '0') }
function clockOf(ms: number, nowMs: number): string {
  const d = new Date(ms)
  const n = new Date(nowMs)
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  const sameDay = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate()
  return sameDay ? hm : `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`
}

// 原文进上下文前：「」换『』避免和外层引号混淆（S3），换行换空格
const quoteSafe = (s: string): string => s.replace(/「/g, '『').replace(/」/g, '』').replace(/\r\n|\r|\n/g, ' ')

/** omitted = 因篇幅没列出的段数（>0 时提醒末尾加一句报个数） */
export function resendMessageText(parts: PendingPart[], round: number, nowMs: number, fileExists: (p: string) => boolean, omitted = 0): string {
  const ats = parts.map(firstAt)
  const first = Math.min(...ats)
  const last = Math.max(...ats)
  const when = last - first > SPAN_MS ? `${clockOf(first, nowMs)} 至 ${clockOf(last, nowMs)}` : clockOf(first, nowMs)
  const head = round <= 1
    ? `${RESEND_PREFIX} 系统提醒：${when} 你在这个私聊里发出的回复中，下列内容因线路故障确定没有送达，对方没有看到（引号内是你此前的原话，仅供参考）：`
    : `${RESEND_PREFIX} 系统提醒：${when} 你在上一次补发提醒之后发出的下列内容又没有送达，对方没有看到（引号内是你此前的原话，仅供参考）：`
  const tail = round <= 1
    ? '线路现已恢复。请结合现在的时间和此后的对话判断这些内容是否仍需让对方知道：仍需要的，按现在的情况用自己的话重新说，不照搬原文，图片仍合适的用原路径重新发送；此后的回复已经说过、或者内容已经不合时宜的，不必再发，本轮不回复即可。不要提及系统提醒本身。'
    : '线路现已恢复。上一次提醒里列过的内容不再重复列出，请只处理这里列出的：仍需要的，按现在的情况用自己的话重新说，不照搬原文，图片仍合适的用原路径重新发送；已经说过或已不合时宜的，不必再发，本轮不回复即可。不要提及系统提醒本身。'
  const bodies = parts.map((p) => (p.kind === 'text' || p.kind === 'voice' ? quoteSafe(p.text ?? '') : null))
  const lineOf = (p: PendingPart, i: number): string => {
    const n = i + 1
    if (p.kind === 'text') return `${n}. 文字：「${bodies[i]}」`
    if (p.kind === 'voice') return `${n}. 语音（念稿）：「${bodies[i]}」`
    const label = p.kind === 'photo' ? '图片' : '文件'
    const path = p.path ?? ''
    return path !== '' && fileExists(path) ? `${n}. ${label}：${path.replace(/\r\n|\r|\n/g, ' ')}` : `${n}. ${label}：原文件已不存在，无法重发。`
  }
  const omittedNote = Number.isInteger(omitted) && omitted > 0 ? [`另有 ${omitted} 段内容因篇幅未列出。`] : []
  const build = (): string => [head, ...parts.map(lineOf), tail, ...omittedNote].join('\n')
  let text = build()
  // 整条超长：从末尾往前把段缩成前 60 字，直到不超
  for (let i = parts.length - 1; i >= 0 && charLen(text) > MESSAGE_MAX; i--) {
    const b = bodies[i]
    if (b !== null && charLen(b) > SHORTENED_MAX) { bodies[i] = cutChars(b, SHORTENED_MAX) + ELLIPSIS; text = build() }
  }
  return text
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const isNumOrNull = (v: unknown): boolean => v === null || isNum(v)

function parsePending(raw: unknown): PendingPart | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  if (typeof r.key !== 'string' || !/^[0-9a-f]{16}$/.test(r.key) || typeof r.kind !== 'string' || !KINDS.has(r.kind)) return null
  if ((r.reason !== 'link_down' && r.reason !== 'network') || !isNum(r.at_ms)) return null
  const out: PendingPart = { key: r.key, kind: r.kind as SendPart['kind'], reason: r.reason, at_ms: r.at_ms }
  if (r.text !== undefined) { if (typeof r.text !== 'string') return null; out.text = r.text }
  if (r.path !== undefined) { if (typeof r.path !== 'string') return null; out.path = r.path }
  if (r.first_ms !== undefined) { if (!isNum(r.first_ms)) return null; out.first_ms = r.first_ms }
  return out
}

/** 畸形或非 v2 一律 null（v1 只存在于开发期，按无记录处理） */
export function parseResendState(raw: unknown): ResendState | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  if (r.v !== 2 || typeof r.chat_id !== 'string' || !isResendChatId(r.chat_id) || !isNum(r.epoch) || !Array.isArray(r.parts)) return null
  if (!isNumOrNull(r.batch_ms) || !isNum(r.rounds) || !isNumOrNull(r.injected_ms) || !isNumOrNull(r.taken_ms) || !isNum(r.listed_total)) return null
  if (!(r.inbox_key === null || typeof r.inbox_key === 'string') || !isNumOrNull(r.cooldown_until_ms)) return null
  if (!r.unknown_keys || typeof r.unknown_keys !== 'object' || Array.isArray(r.unknown_keys)) return null
  const parts: PendingPart[] = []
  for (const p of r.parts) { const pp = parsePending(p); if (!pp) return null; parts.push(pp) }
  const unknown_keys: Record<string, number> = {}
  for (const [k, v] of Object.entries(r.unknown_keys as Record<string, unknown>)) { if (!isNum(v)) return null; unknown_keys[k] = v }
  return { v: 2, chat_id: r.chat_id, epoch: r.epoch, parts, batch_ms: r.batch_ms as number | null, rounds: r.rounds,
    injected_ms: r.injected_ms as number | null, inbox_key: r.inbox_key as string | null, taken_ms: r.taken_ms as number | null,
    listed_total: r.listed_total, unknown_keys, cooldown_until_ms: r.cooldown_until_ms as number | null }
}

// ── 同步 IO（全程无 await，I9） ─────────────────────────────────
const resendDir = (dir: string): string => join(dir, '.resend')
const stateFile = (dir: string, chatId: string): string => join(resendDir(dir), `${chatId}.json`)
const epochFile = (dir: string): string => join(resendDir(dir), 'clear-epoch')

function ensureDir(dir: string): void {
  const d = resendDir(dir)
  const created = mkdirSync(d, { recursive: true, mode: 0o700 })
  if (created !== undefined) { chmodSync(d, 0o700); return } // 新建：mode 受 umask 影响，补成 0700
  // 已有目录只收紧组和其他人的权限，不替属主放开写权限：目录被锁成只读时写盘应当失败、由调用方记日志（#7）
  const mode = statSync(d).mode & 0o777
  if (mode & 0o077) chmodSync(d, mode & 0o700)
}

// 写临时文件后 rename：崩溃时要么旧内容要么新内容，不会留半截文件
function atomicWrite(file: string, content: string): void {
  // basename：Windows 路径是反斜杠，按 '/' 切会拼出非法临时名
  const tmp = join(file, '..', `.${basename(file)}.tmp-${process.pid}`)
  writeFileSync(tmp, content, { mode: 0o600 })
  chmodSync(tmp, 0o600)
  renameSync(tmp, file)
}

/** 文件不存在或内容畸形 → null（按无记录处理）；权限、IO 等其它读错误照抛：调用方本轮跳过，不能把它当空记录覆盖或删掉（#2） */
export function readResendState(dir: string, chatId: string): ResendState | null {
  if (!isResendChatId(chatId)) return null
  let raw: string
  try {
    raw = readFileSync(stateFile(dir, chatId), 'utf8')
  } catch (e) {
    if ((e as NodeJS.ErrnoException)?.code === 'ENOENT') return null
    throw e
  }
  try {
    const s = parseResendState(JSON.parse(raw))
    return s && s.chat_id === chatId ? s : null
  } catch {
    return null
  }
}

/** null → 删文件；目录 0700、文件 0600、tmp+rename；返回成败 */
export function writeResendState(dir: string, chatId: string, s: ResendState | null): boolean {
  if (!isResendChatId(chatId)) return false
  try {
    const file = stateFile(dir, chatId)
    if (s === null) { if (existsSync(file)) unlinkSync(file); return true }
    ensureDir(dir)
    atomicWrite(file, JSON.stringify(s))
    return true
  } catch {
    return false
  }
}

/** 只返回文件名合法（纯数字 chatId）的私聊（I12） */
export function listResendChats(dir: string): string[] {
  try {
    return readdirSync(resendDir(dir)).map((f) => /^(\d+)\.json$/.exec(f)?.[1]).filter((c): c is string => typeof c === 'string')
  } catch {
    return []
  }
}

export function readClearEpoch(dir: string): number {
  try {
    const n = Number(readFileSync(epochFile(dir), 'utf8').trim())
    return Number.isSafeInteger(n) && n >= 0 ? n : 0
  } catch {
    return 0
  }
}

/** 会话文件被 dispatcher 移走后加 1；失败抛错由调用方记日志 */
export function bumpClearEpoch(dir: string): void {
  ensureDir(dir)
  atomicWrite(epochFile(dir), String(readClearEpoch(dir) + 1))
}

/** 只在有段号时加 resend_parts（升序去重的非负整数）；空时原样返回同一对象（回包逐字不变） */
export function withResendParts<T extends object>(body: T, nums: number[]): T {
  const xs = [...new Set((Array.isArray(nums) ? nums : []).filter((n) => Number.isInteger(n) && n >= 0))].sort((a, b) => a - b)
  return xs.length ? { ...body, resend_parts: xs } : body
}

/** 同一次 /send 里与已登记段内容相同的入选段，段号一并外报（入库仍只一条，#5） */
function withDuplicateIdx(parts: SendPart[], sources: PartSource[], scheduled: number[]): number[] {
  if (scheduled.length === 0 || !Array.isArray(parts)) return scheduled
  const items = parts.map((p, i) => ({ p, src: (Array.isArray(sources) ? sources[i] : null) ?? null }))
    .filter((x) => x.p && eligible(x.p, x.src)).map((x) => ({ idx: partIdx(x.p), key: keyOf(x.p, x.src) }))
  const keys = new Set(items.filter((x) => x.idx !== null && scheduled.includes(x.idx)).map((x) => x.key))
  const out = new Set(scheduled)
  for (const x of items) if (x.idx !== null && x.key !== null && keys.has(x.key)) out.add(x.idx)
  return [...out].sort((a, b) => a - b)
}

/** /send 收尾的同步入口：读→判→写一气呵成；写盘失败返回空数组（响应就不加字段）。
 *  读状态遇到权限/IO 错误照抛（不覆盖文件），由调用方记日志。onEvent 只在状态落定（无变化或写盘成功）后回调 */
export function resendRecord(dir: string, chatId: string, parts: SendPart[], sources: PartSource[], nowMs: number,
  onEvent?: (e: ResendEvent) => void): number[] {
  const emit = (kind: string, added: number, pending: number): void => {
    if (onEvent) try { onEvent({ kind, added, pending }) } catch { /* 记日志失败不影响登记 */ }
  }
  if (!isResendChatId(chatId)) { emit('bad_chat', 0, 0); return [] }
  const prev = readResendState(dir, chatId)
  const r = applySendResult(prev, chatId, parts, sources, { nowMs, epoch: readClearEpoch(dir) })
  if (JSON.stringify(r.state) !== JSON.stringify(prev) && !writeResendState(dir, chatId, r.state)) return []
  const ev = r.event as { kind: string; added?: number; pending?: number; sent_removed?: number; unknown?: number; unknown_skipped?: number }
  const added = ev.added ?? 0, pending = ev.pending ?? 0
  if ((ev.sent_removed ?? 0) > 0) emit('sent_match', 0, pending)
  if ((ev.unknown ?? 0) > 0 || (ev.unknown_skipped ?? 0) > 0) emit('unknown_key', 0, pending)
  if (ev.kind === 'recorded' || ev.kind === 'cooldown' || ev.kind === 'exhausted') emit(ev.kind, ev.kind === 'recorded' ? added : 0, pending)
  return withDuplicateIdx(parts, sources, r.scheduled)
}

/** 巡检的同步入口：每个私聊各自 try，一个出错不影响别的 */
export function resendTickAll(dir: string, ctx: TickCtx, io: {
  inboxPresent(key: string, chatId: string): boolean
  writeInbox(chatId: string, key: string, meta: { text: string; [k: string]: unknown }): void
  removeInbox(chatId: string, key: string): void
  ensureWorker(chatId: string): void
  log(line: string): void }): void {
  const epoch = readClearEpoch(dir)
  const errName = (e: unknown): string => (e as Error)?.name ?? 'Error'
  for (const chat of listResendChats(dir)) {
    try {
      const s = readResendState(dir, chat)
      if (!s) { writeResendState(dir, chat, null); continue } // 畸形或旧版本：按无记录处理，整文件删除
      const present = s.inbox_key !== null && io.inboxPresent(s.inbox_key, chat)
      const d = decideResendTick(s, { nowMs: ctx.nowMs, epoch, linkUp: ctx.linkUp, linkUpSinceMs: ctx.linkUpSinceMs, blocked: ctx.blocked, inboxPresent: present })
      if (d.removeInboxKey) io.removeInbox(chat, d.removeInboxKey)
      if (d.action === 'inject' && d.inject) {
        const { round, inboxKey, messageId, parts, omitted } = d.inject
        const text = resendMessageText(parts, round, ctx.nowMs, ctx.fileExists, omitted)
        // I10：先写状态再写 inbox；崩在两步之间最多少补一轮，不会重复投
        if (!writeResendState(dir, chat, d.state)) { io.log(`resend_inject_failed reason=state_write chat=${chat}`); continue }
        try {
          io.writeInbox(chat, inboxKey, { text, chat_id: chat, scene: 'private', from_id: chat, chat_type: 'private',
            is_bot_sender: false, from_username: 'user', sender_username: 'user', ts: new Date(ctx.nowMs).toISOString(),
            message_id: messageId, synthetic: true })
        } catch (e) {
          io.log(`resend_inject_failed reason=${errName(e)} chat=${chat}`)
          // 回滚到投件前，下轮再试；回滚也写不进去时这批段已移出待补，必须留一行记录（#7）
          if (!writeResendState(dir, chat, s)) io.log(`resend_inject_failed reason=rollback chat=${chat}`)
          continue
        }
        const ageS = Math.round((ctx.nowMs - Math.min(...parts.map(firstAt))) / 1000)
        io.log(`resend_inject chat=${chat} round=${round}/${RESEND_MAX_ROUNDS} parts=${parts.length} chars=${text.length} age_s=${ageS}`)
        try { io.ensureWorker(chat) } catch (e) { io.log(`resend_ensure_worker_failed reason=${errName(e)} chat=${chat}`) }
        continue
      }
      if (JSON.stringify(d.state) !== JSON.stringify(s) && !writeResendState(dir, chat, d.state)) io.log(`resend_tick_failed reason=state_write chat=${chat}`)
      if (s.taken_ms === null && d.state?.taken_ms != null) io.log(`resend_taken chat=${chat} round=${s.rounds}`)
      if (d.action === 'done') io.log(`resend_done chat=${chat} rounds=${s.rounds} listed=${s.listed_total}`)
      else if (d.action === 'give_up') io.log(`resend_give_up chat=${chat} rounds=${s.rounds} pending=${s.parts.length} cooldown_h=12`)
      else if (d.action === 'void') io.log(`resend_void chat=${chat} reason=session`)
      else if (d.action === 'expired') io.log(`resend_void chat=${chat} reason=expired`)
    } catch (e) {
      io.log(`resend_tick_failed reason=${errName(e)} chat=${chat}`)
    }
  }
}
