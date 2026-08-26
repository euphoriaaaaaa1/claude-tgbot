/**
 * 被晾感知（有生活的追问）· 纯逻辑层。
 *
 * bot 在私聊回完话、用户一直没回音时，按 7±2 / 25±5 / 90±15 三档决定"追不追"。
 * 追不追取决于她此刻在做什么：在睡 → 顺延；在上课/工作（不可打断）→ 不追、只记档案，
 * 等她"下课"后的第一次注入机会自然产生迟到反应；闲着 → 按性格追一句。
 *
 * 纯函数：不碰时钟/文件/随机数，时刻与抖动一律参数注入（IO 与定时器归 hang_runtime.ts）。
 * 任何路径都不接触消息正文：topicHint 只是占位串，档案文本只有时长与活动名。
 */

export type HangState = {
  chatId: string
  armedAt: number                          // 出站武装时刻(ms)
  stage: 0 | 1 | 2 | 3                     // 0=等首档 1=已追1次 2=已追2次 3=已终结
  nextAt: number                           // 下一档触发时刻(ms)
  topicHint: string                        // 允许空串
  episodesToday: number
  dayKey: string                           // YYYY-MM-DD（宿主本地时区，裁决4）
}

export type HangEvent =
  | { kind: 'outboundReply'; chatId: string; nowMs: number; hadRecentInbound: boolean }
  | { kind: 'inbound'; chatId: string; nowMs: number }
  | { kind: 'tick'; nowMs: number; activityInterruptible: boolean; asleep: boolean }

export type HangAction =
  | { kind: 'none' }
  | { kind: 'injectFollowup'; stage: 1 | 2 }
  | { kind: 'injectMemoryWrite' }
  | { kind: 'archiveOnly' }

export type HangDecision = { action: HangAction; state: HangState | null }

const MIN = 60_000
/** 各档"等多久"：stage0 等首档 7±2，stage1 等第二档 25±5，stage2 等第三档 90±15 */
const BASE = [7 * MIN, 25 * MIN, 90 * MIN] as const
const SPREAD = [2 * MIN, 5 * MIN, 15 * MIN] as const
const MAX_EPISODES_PER_DAY = 3
const NONE: HangAction = { kind: 'none' }

/** 本地时区的 YYYY-MM-DD（裁决4）。不用 toISOString——那是 UTC，会把跨时区的一天切错。 */
export function hangDayKey(nowMs: number): string {
  const d = new Date(nowMs)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

const jitter = (stage: 0 | 1 | 2, rand01: number): number =>
  Math.round(BASE[stage] + (rand01 * 2 - 1) * SPREAD[stage])

/** 结构缺键/类型不对一律当"没有事件"（安全降级）：状态文件损坏时绝不误触发。 */
function valid(s: unknown): s is HangState {
  if (!s || typeof s !== 'object' || Array.isArray(s)) return false
  const o = s as Record<string, unknown>
  return typeof o.chatId === 'string'
    && typeof o.armedAt === 'number' && Number.isFinite(o.armedAt)
    && typeof o.stage === 'number' && Number.isInteger(o.stage) && o.stage >= 0 && o.stage <= 3
    && typeof o.nextAt === 'number' && Number.isFinite(o.nextAt)
    && typeof o.topicHint === 'string'
    && typeof o.episodesToday === 'number' && Number.isFinite(o.episodesToday) && o.episodesToday >= 0
    && typeof o.dayKey === 'string'
}

/**
 * 决策机：给定当前状态（null=无事件）与事件，返回动作与新状态（null=事件清除）。
 *
 * 武装：outboundReply 且 10 分钟内有该 chat 入站 且 无现役事件 且 今日额度未满 →
 *   只落状态、不注入，nextAt = nowMs + 7±2 分钟。
 * 取消：inbound 任意档位一律清除，什么都不发。
 * 到点：在睡 → 顺延到 nowMs + 本档基础间隔（不重新抖动、不耗档位不耗次数，裁决2）；
 *   不可打断 → archiveOnly 并终结；可打断 → 按档出追问1/追问2/记忆写入。
 * 计数（裁决1）：episodesToday 在"事件首次产生实际动作"时才 +1，武装不预扣，
 *   被 inbound 取消的武装零消耗；episodesToday>=3 仍禁止新武装。跨天自动清零。
 * 错误契约：nowMs 非有限数 / 事件非法 / rand01 越界 → TypeError；state 结构缺键 → 视为 null。
 * 入参不被修改，返回的 state 恒为新对象（或原对象的只读透传）。
 */
export function decideHang(state: HangState | null, ev: HangEvent, rand01: number): HangDecision {
  if (typeof rand01 !== 'number' || !Number.isFinite(rand01) || rand01 < 0 || rand01 > 1) {
    throw new TypeError('decideHang: rand01 must be a number in [0,1]')
  }
  if (!ev || typeof ev !== 'object' || Array.isArray(ev)) {
    throw new TypeError('decideHang: event must be an object')
  }
  const kind = (ev as { kind?: unknown }).kind
  if (kind !== 'outboundReply' && kind !== 'inbound' && kind !== 'tick') {
    throw new TypeError('decideHang: unknown event kind')
  }
  const now = (ev as { nowMs?: unknown }).nowMs
  if (typeof now !== 'number' || !Number.isFinite(now)) {
    throw new TypeError('decideHang: event requires a finite nowMs')
  }
  const s = valid(state) ? state : null

  if (kind === 'inbound') return { action: NONE, state: null }

  if (kind === 'outboundReply') {
    const e = ev as Extract<HangEvent, { kind: 'outboundReply' }>
    // stage<3 = 现役事件：这条出站是它自己引发的（追问后 bot 又说了一句），不重复武装
    if (s && s.stage !== 3) return { action: NONE, state: s }
    const today = hangDayKey(now)
    const carried = s && s.dayKey === today ? s.episodesToday : 0
    const kept = s ? { ...s, episodesToday: carried, dayKey: today } : null
    if (e.hadRecentInbound !== true) return { action: NONE, state: kept }   // bot 主动消息不算被晾(S5)
    if (carried >= MAX_EPISODES_PER_DAY) return { action: NONE, state: kept }
    return {
      action: NONE,
      state: {
        chatId: typeof e.chatId === 'string' && e.chatId ? e.chatId : (s?.chatId ?? ''),
        armedAt: now,
        stage: 0,
        nextAt: now + jitter(0, rand01),
        topicHint: '',
        episodesToday: carried,
        dayKey: today,
      },
    }
  }

  // tick
  if (!s) return { action: NONE, state: null }
  if (s.stage === 3 || now < s.nextAt) return { action: NONE, state: s }
  const stage = s.stage as 0 | 1 | 2
  const e = ev as Extract<HangEvent, { kind: 'tick' }>

  if (e.asleep === true) {
    // 睡着不追：往后挪一档基础间隔，档位与次数都不动
    return { action: NONE, state: { ...s, nextAt: now + BASE[stage] } }
  }
  // 首个实际动作才记账；跨天先清零（裁决1 + 裁决4）
  const counted = stage === 0
    ? { episodesToday: (s.dayKey === hangDayKey(now) ? s.episodesToday : 0) + 1, dayKey: hangDayKey(now) }
    : {}

  if (e.activityInterruptible !== true) {
    // 在上课/工作：一条都不发，只留档案，事件就此终结（迟到反应由注入层带出）
    return { action: { kind: 'archiveOnly' }, state: { ...s, ...counted, stage: 3, nextAt: now } }
  }
  if (stage === 2) {
    return { action: { kind: 'injectMemoryWrite' }, state: { ...s, ...counted, stage: 3, nextAt: now } }
  }
  const next = (stage + 1) as 1 | 2
  return {
    action: { kind: 'injectFollowup', stage: next },
    state: { ...s, ...counted, stage: next, nextAt: now + jitter(next, rand01) },
  }
}

// ─── 注入用的一小段内部文本 ────────────────────────────────────────────
export type HangPromptKind = 'followup1' | 'followup2' | 'memoryWrite' | 'lateReaction'
const PROMPT_KINDS: readonly string[] = ['followup1', 'followup2', 'memoryWrite', 'lateReaction']
const TAG = '[hang-check]'

/**
 * 档案/追问渲染：只含未回时长与活动名，**绝不夹带任何用户正文**（话题一句话由她自己写记忆时提炼）。
 * 输出以 "[hang-check]" 开头 → burst 层不合并、日志/摘要层认得出是内部注入。
 * 入参非法（kind 不在四种内 / 分钟数非有限数或负数 / 活动名非字符串）→ TypeError。
 */
export function renderHangPrompt(kind: HangPromptKind, minutesSilent: number, activityName: string): string {
  if (typeof kind !== 'string' || !PROMPT_KINDS.includes(kind)) {
    throw new TypeError('renderHangPrompt: unknown kind')
  }
  if (typeof minutesSilent !== 'number' || !Number.isFinite(minutesSilent) || minutesSilent < 0) {
    throw new TypeError('renderHangPrompt: minutesSilent must be a finite number >= 0')
  }
  if (typeof activityName !== 'string') {
    throw new TypeError('renderHangPrompt: activityName must be a string')
  }
  const m = Math.round(minutesSilent)
  const at = `「${activityName}」`
  switch (kind) {
    case 'followup1':
      return `${TAG} 他 ${m} 分钟没回你了，你这会儿在${at}，手上不忙。追一句还是继续等，按你的性子来。`
    case 'followup2':
      return `${TAG} 他还是没回，已经 ${m} 分钟了，你在${at}。最后一次开口，淡一点，别追着不放。`
    case 'memoryWrite':
      return `${TAG} 他 ${m} 分钟没回，别再发消息了。把这次被晾写进记忆：一句话记下聊到哪儿、你当时在${at}。`
    default:
      return `${TAG} 他那条消息你隔了 ${m} 分钟才看见，那会儿你在${at}。回他的时候把这段空白说出来，别装作无事发生。`
  }
}
