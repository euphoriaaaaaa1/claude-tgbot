/**
 * 凑一波再递（进线防抖合并）· 纯逻辑层。
 *
 * 背景：用户连发 N 条，第一条立刻投给 claude 就开了回合 → 只回第一条，其余挤进下一轮。
 * 这里给"要不要再等一下"的状态机 + "把这一波拼成一条"的合并器。
 *
 * 纯函数：不碰时钟/文件/网络，时间一律参数注入（IO 与定时器归 burst_inbox.ts / worker-plugin.ts）。
 * 正文只做拼接、逐字不改，任何路径都不打印正文（错误信息只带序号，不带内容）。
 */

export type InMsg = { path: string; ts: number; hasMedia: boolean; text: string; chatId: string }

export type BurstAction =
  | { kind: 'hold' }                                              // 继续等
  | { kind: 'flush'; paths: string[] }                            // 把这些原件按序合并投递
  | { kind: 'flushThenSingle'; paths: string[]; single: string }  // 先合并投 paths，再单投 single（媒体）

export type BurstEvent =
  | { kind: 'arrive'; msg: InMsg }
  | { kind: 'timer'; nowMs: number }

export type BurstDecision = { action: BurstAction; wave: InMsg[] }

const pathsOf = (wave: InMsg[]): string[] => wave.map(m => m.path)

/** 波非空时来了个"不能并进去"的消息：先把波投掉，再单投它；波清空 */
function cutWave(wave: InMsg[], single: string): BurstDecision {
  return wave.length === 0
    ? { action: { kind: 'flush', paths: [single] }, wave: [] }
    : { action: { kind: 'flushThenSingle', paths: pathsOf(wave), single }, wave: [] }
}

/**
 * 状态机决策：给定当前波（按到达序）、新事件与时刻，返回动作与新波。
 *
 * 规则：无波时 arrive 文本 → 开波 hold；arrive 媒体且无波 → flush 单投；
 *   波内 arrive 文本 → 续窗 hold；波内 arrive 媒体 → flushThenSingle（波不含媒体）；
 *   timer 时刻 - 首条 ts >= maxMs，或 - 末条 ts >= windowMs → flush；否则 hold。
 *   windowMs<=0 → 每次 arrive 立即 flush（关闭防抖）。
 * 错误契约：wave 非数组 / 事件非法 / ts 非有限数 → TypeError；空波 timer → hold（幂等）。
 * 入参不被修改，返回的 wave 恒为新数组。
 */
export function decideBurst(
  wave: InMsg[],
  ev: { kind: 'arrive'; msg: InMsg } | { kind: 'timer'; nowMs: number },
  windowMs: number,
  maxMs: number,
): BurstDecision {
  if (!Array.isArray(wave)) throw new TypeError('decideBurst: wave must be an array')
  if (!ev || typeof ev !== 'object') throw new TypeError('decideBurst: event must be an object')
  const win = Number(windowMs)
  const cap = Number(maxMs)
  const debounceOff = !(win > 0)   // 0 / 负数 / 非数 → 关闭防抖

  if (ev.kind === 'arrive') {
    const msg = (ev as { msg?: InMsg }).msg
    if (!msg || typeof msg !== 'object') throw new TypeError('decideBurst: arrive event requires msg')
    if (typeof msg.path !== 'string') throw new TypeError('decideBurst: msg.path must be a string')
    if (typeof msg.ts !== 'number' || !Number.isFinite(msg.ts)) {
      throw new TypeError('decideBurst: msg.ts must be a finite number')
    }
    // 关闭防抖 / 媒体：都不进波。波里有残留就先冲掉（正常不会有，冲掉也绝不丢）。
    if (debounceOff || msg.hasMedia) return cutWave(wave, msg.path)
    return { action: { kind: 'hold' }, wave: [...wave, msg] }
  }

  if (ev.kind === 'timer') {
    const now = (ev as { nowMs?: number }).nowMs
    if (typeof now !== 'number' || !Number.isFinite(now)) {
      throw new TypeError('decideBurst: timer event requires a finite nowMs')
    }
    if (wave.length === 0) return { action: { kind: 'hold' }, wave: [] }
    const firstTs = wave[0]!.ts
    const lastTs = wave[wave.length - 1]!.ts
    // 坏 ts（调用方自己拼的波）一律立刻投：宁可早投，也不能让它永远卡在内存里丢掉。
    if (!Number.isFinite(firstTs) || !Number.isFinite(lastTs) || debounceOff) {
      return { action: { kind: 'flush', paths: pathsOf(wave) }, wave: [] }
    }
    const capped = Number.isFinite(cap) && cap > 0 && now - firstTs >= cap
    if (capped || now - lastTs >= win) return { action: { kind: 'flush', paths: pathsOf(wave) }, wave: [] }
    return { action: { kind: 'hold' }, wave: wave.slice() }
  }

  throw new TypeError('decideBurst: unknown event kind')
}

/**
 * 合并 N 条已读出的 inbox JSON 文本为一条投递用 JSON 文本。
 * 文本按序以 '\n' 连接（各条原文不改）；meta（chat_id/message_id/scene/来源人）取第一条；ts 取最后一条。
 * 任一条 JSON 解析失败 → 抛 Error（调用方降级为逐条单投，绝不丢）。入参非字符串数组/空数组 → TypeError。
 */
export function mergeInboxJsons(rawJsons: string[]): string {
  if (!Array.isArray(rawJsons)) throw new TypeError('mergeInboxJsons: rawJsons must be an array')
  if (rawJsons.length === 0) throw new TypeError('mergeInboxJsons: rawJsons must not be empty')
  for (let i = 0; i < rawJsons.length; i++) {
    if (typeof rawJsons[i] !== 'string') throw new TypeError(`mergeInboxJsons: item ${i} must be a string`)
  }
  const objs = rawJsons.map((raw, i) => {
    let o: unknown
    // 重新包一层再抛：JSON.parse 的原生报错里带输入片段（=消息正文），绝不能流进日志。
    try { o = JSON.parse(raw) } catch { throw new Error(`mergeInboxJsons: item ${i} is not valid JSON`) }
    if (!o || typeof o !== 'object' || Array.isArray(o)) {
      throw new Error(`mergeInboxJsons: item ${i} is not a JSON object`)
    }
    return o as Record<string, unknown>
  })
  const first = objs[0]!
  const last = objs[objs.length - 1]!
  const text = objs.map(o => (typeof o.text === 'string' ? o.text : '')).join('\n')
  return JSON.stringify({ ...first, text, ts: 'ts' in last ? last.ts : first.ts })
}
