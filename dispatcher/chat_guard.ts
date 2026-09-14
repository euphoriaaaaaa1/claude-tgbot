/**
 * 聊天守卫（desync ③b ④）：纯函数，不做 I/O、不读 env、永不抛。
 * - 真人计时：只认生产者（dispatcher / director）正向打的 meta.human_ts，合成消息不重置。
 * - 跨聊天拦截：bot 主动把私聊内容发进闲置的群 → 拦；用户在私聊里明确让它去群里说 → 放行一次。
 * 契约见 INTERFACE-desync §4 §6 §10.5 §11.2。
 */
import { resolve, isAbsolute } from 'path'

export const CROSS_GROUP_IDLE_MS = 10 * 60_000
export const USER_REQUEST_WINDOW_MS = 10 * 60_000
// 回滚开关（PLAN §1.6）：上线后发现模型乱声明 → 改 false 退回纯拦截，不回退代码
export const USER_REQUEST_ENABLED = true
const FUTURE_SKEW_MS = 60_000

export type InboundState = {
  _human: number | null
  _human_by_chat: Record<string, number>
  _director_by_chat: Record<string, number>
  _mentions_group_by_chat: Record<string, number>
}
export type CrossChatResult = {
  block: boolean
  idleMin: number | null
  bypass: 'user_request' | null
  userRequestDenied: 'expired' | 'no_group_word' | 'already_used' | null
}

const isObj = (v: unknown): v is Record<string, any> => v !== null && typeof v === 'object' && !Array.isArray(v)
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** 毫秒 → ISO（秒级，毫秒位恒为 .000Z）；越界/非法 → null */
function isoSec(ms: number): string | null {
  try { return finite(ms) ? new Date(Math.floor(ms / 1000) * 1000).toISOString() : null } catch { return null }
}

export function humanTsOf(meta: unknown, tsMs: number): number | null {
  if (!isObj(meta) || typeof meta.human_ts !== 'string') return null
  const h = Date.parse(meta.human_ts)
  return finite(h) && h > 0 && h <= tsMs + FUTURE_SKEW_MS ? h : null
}

export function isDirectorInbound(meta: unknown): boolean {
  return isObj(meta) && (meta.from_username === 'director' || meta.sender_username === 'director')
}

/** dispatcher 写 inbox meta 的唯一真人打标点：真人非合成 → human_ts；正文（去前导 @mention）含"群" → mentions_group */
export function inboxHumanMarks(text: unknown, isBotSender: unknown, synthetic: unknown, dateSec: unknown, nowMs: number):
  { human_ts?: string; mentions_group?: true } {
  if (isBotSender || synthetic) return {}
  const fromDate = finite(dateSec) && dateSec > 0 ? isoSec(dateSec * 1000) : null
  const human_ts = fromDate ?? isoSec(nowMs) ?? isoSec(Date.now())!
  const body = typeof text === 'string' ? text.replace(/^(?:\s*@\S+)+/, '') : ''
  // "群"只按字面判定（用户决定 §8.10：不认同义词、不认异体字）
  return body.includes('群') ? { human_ts, mentions_group: true } : { human_ts }
}

function numTable(v: unknown): Record<string, number> {
  const out: Record<string, number> = {}
  if (isObj(v)) for (const [k, x] of Object.entries(v)) if (finite(x)) out[k] = x
  return out
}

/** sidecar 启动读入容错：坏 JSON/非对象/非有限数一律退化为空（旧行为） */
export function loadInboundState(json: unknown): InboundState {
  const o = isObj(json) ? json : {}
  return {
    _human: finite(o._human) && o._human > 0 ? o._human : null,
    _human_by_chat: numTable(o._human_by_chat),
    _director_by_chat: numTable(o._director_by_chat),
    _mentions_group_by_chat: numTable(o._mentions_group_by_chat),
  }
}

/** sidecar 四个保留键的更新规则（§4.3）。返回新对象，不改入参 */
export function applyInboundMarks(state: unknown, chatId: string, meta: unknown, tsMs: number): InboundState {
  const s = loadInboundState(state)
  const h = humanTsOf(meta, tsMs)
  if (h != null) {
    if (h > (s._human ?? -Infinity)) s._human = h
    if (chatId && h > (s._human_by_chat[chatId] ?? -Infinity)) s._human_by_chat[chatId] = h
    if (chatId && (meta as any).mentions_group === true) s._mentions_group_by_chat[chatId] = h
  }
  if (chatId && finite(tsMs) && isDirectorInbound(meta)) s._director_by_chat[chatId] = tsMs
  return s
}

/** src = 最近一条真人消息所在 chat；无记录 → null */
export function pickSrcChat(state: unknown): string | null {
  const t = isObj(state) ? numTable(state._human_by_chat) : {}
  let best: string | null = null
  for (const [k, v] of Object.entries(t)) if (best == null || v > t[best]) best = k
  return best
}

/** dst 最近活跃 = max(该 chat 最近真人, 最近导演注入)；都缺 → undefined */
export function dstActiveMs(state: unknown, dst: string): number | undefined {
  if (!isObj(state)) return undefined
  const vals = [numTable(state._human_by_chat)[dst], numTable(state._director_by_chat)[dst]].filter(finite)
  return vals.length ? Math.max(...vals) : undefined
}

export function crossChatDecision(srcChatId: unknown, dstChatId: unknown, dstActive: unknown, nowMs: number, opts?: unknown): CrossChatResult {
  const o = isObj(opts) ? opts : {}
  const dst = typeof dstChatId === 'string' ? dstChatId : ''
  const src = typeof srcChatId === 'string' ? srcChatId : ''
  const dstNumeric = /^-?\d+$/.test(dst)
  // 本仓无 chat_id 别名层：非数字 dst 不可能是可信私聊 → 按"群且无记录"处理
  const active = dstNumeric && finite(dstActive) ? dstActive : null
  // 取整用 floor（已过去的整分钟）：公开锁定用例要求 30 秒 → 0，Math.round(0.5) 会得 1
  const idleRaw = active != null ? Math.floor((nowMs - active) / 60_000) : null
  const idleMin = idleRaw != null && Number.isFinite(idleRaw) ? idleRaw : null
  const dstGroupish = dst !== '' && (!dstNumeric || dst.startsWith('-'))
  const block = /^\d+$/.test(src) && dstGroupish && src !== dst
    && (active == null || nowMs - active > CROSS_GROUP_IDLE_MS)
  if (!USER_REQUEST_ENABLED || o.userRequested !== true) return { block, idleMin, bypass: null, userRequestDenied: null }

  const h = o.srcHumanMs
  // 无下界：时钟回拨（h > now）视为刚说
  const denied = !(finite(h) && nowMs - h <= USER_REQUEST_WINDOW_MS) ? 'expired'
    : o.srcMentionsGroup !== true ? 'no_group_word'
    : finite(o.usedForHumanMs) && o.usedForHumanMs === h ? 'already_used'
    : null
  return denied
    ? { block, idleMin, bypass: null, userRequestDenied: denied }
    : { block: false, idleMin, bypass: 'user_request', userRequestDenied: null }
}

/** 放行账本：只有"靠声明放行且 /send 成功"才记下这条真人消息已用过 */
export function applyReplyOutcome(ledger: Record<string, number>, src: string | null, srcHumanMs: number | null,
  decision: unknown, sendOk: boolean): Record<string, number> {
  if (isObj(decision) && decision.bypass === 'user_request' && sendOk === true && src != null && srcHumanMs != null) {
    return { ...ledger, [src]: srcHumanMs }
  }
  return ledger
}

const USER_REQUEST_RULE = '用户在私聊里刚刚（10 分钟内）明确让你去群里说某件事时，直接去群里说，reply 带 user_requested:true；'
  + '私聊里照常回他，两边互不耽误；同一句要求只去群里发一次；其它任何情况都不得声明 user_requested。'

/** MCP instructions 的跨场景提示（从 access.json 取私聊 id + 群 id）；缺任一 → '' */
export function crossSceneHint(access: unknown): string {
  try {
    if (!isObj(access) || !Array.isArray(access.allowFrom) || access.allowFrom.length === 0 || !isObj(access.groups)) return ''
    const priv = String(access.allowFrom[0] ?? '')
    const grp = Object.keys(access.groups)[0] ?? ''
    if (!priv || !grp) return ''
    return `【你可以主动切换场景，像真人一样】你同时在群和私聊、是同一个你。想在哪说，就用 reply 显式带上对应 chat_id（他的私聊 chat_id=${priv}；群 chat_id=${grp}）。`
      + '典型时机：在群里被调侃/被说到、或有话不想当众讲 → 用私聊 id 私下找他诉苦。'
      + '私聊里聊到的内容（尤其亲密/私密的）永远不带进群；想在群里说别的事，也只在群本来就热闹时。'
      + USER_REQUEST_RULE
      + '**务必克制**：主动切换是偶尔冒出来的真实冲动，不是每轮都切——绝大多数时候就待在当前场景回。'
  } catch { return '' }
}

/** reply.inputSchema.user_requested 的描述 */
export function replyToolDescription(): string {
  return '只有用户在私聊里刚刚（10 分钟内）明确让你去群里说某件事时才置 true；其它任何情况不得声明。私聊里聊到的内容永远不带进群。'
}

const FORBIDDEN_PORTS = new Set(['17801', '17802', '17803', '17804', '7897', '7788'])

/** 取 URL 端口；解析不了 → null。不用 WHATWG URL：它拒绝 >65535 的端口，而契约要求只按端口字面判定 */
function urlPort(u: string): string | null {
  const m = /^([a-z][a-z0-9+.-]*):\/\/(?:[^@\/?#]*@)?(?:\[[^\]]*\]|[^:\/?#]+)(?::(\d*))?(?:[\/?#]|$)/i.exec(u)
  if (!m) return null
  return m[2] || (m[1].toLowerCase() === 'https' ? '443' : '80')
}

/** 测试模式 fail-closed 自检（§11.2 第 5 条）：只读传入 env，按序返回首个不满足项 */
export function testModeCheck(env: unknown): { ok: boolean; reason: string | null } {
  const e = isObj(env) ? env : {}
  if (e.CLAUDEBOTLIFE_TEST !== '1') return { ok: true, reason: null }
  const root = e.CLAUDEBOTLIFE_TEST_ROOT
  if (typeof root !== 'string' || !isAbsolute(root)) return { ok: false, reason: 'missing_root' }
  const rr = resolve(root)
  // 字符串规范化后比前缀（不解析符号链接）；<root>y 这种同前缀兄弟目录不算
  const under = (p: unknown) => typeof p === 'string' && p !== '' && (resolve(p) === rr || resolve(p).startsWith(rr + '/'))
  if (!under(e.HOME)) return { ok: false, reason: 'home_outside_root' }
  if (!under(e.CHANNEL_DIR)) return { ok: false, reason: 'path_outside_root:CHANNEL_DIR' }
  for (const k of ['HUB_CONFIGS_DIR', 'DIRECTOR_CHANNELS_ROOT']) {
    if (typeof e[k] === 'string' && e[k] !== '' && !under(e[k])) return { ok: false, reason: `path_outside_root:${k}` }
  }
  for (const k of ['TELEGRAM_DISPATCHER_URL', 'DISPATCHER_URL']) {
    if (typeof e[k] !== 'string' || e[k] === '') continue
    const port = urlPort(e[k])
    if (port == null || FORBIDDEN_PORTS.has(port)) return { ok: false, reason: 'forbidden_port' }
  }
  return { ok: true, reason: null }
}
