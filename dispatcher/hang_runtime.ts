/**
 * 被晾感知 · IO 层：状态落盘、到点查作息、把动作变成一条内部 inbox 注入。
 *
 * 决策全在 hang_plan.ts（纯函数），这里只做"读写状态文件 / 问 python 桥 / 调注入回调"。
 * 依赖全部注入（时钟、随机数、活动查询、注入回调、日志），所以能拿假时钟 + 假桥 +
 * 临时目录直接白盒测。
 *
 * 铁律：
 * - 桥挂 / 超时 → 降级"不追"（这一轮什么都不做），绝不崩、绝不误追。
 * - 状态文件损坏 → 当没事件（decideHang 自带结构校验），不崩不误触发。
 * - 日志只写 chat_id / 档位 / 原因码，不写正文、不写活动名。
 */
import { existsSync, readFileSync, renameSync, writeFileSync } from 'fs'
import { join } from 'path'
import { decideHang, hangDayKey, renderHangPrompt, type HangState } from './hang_plan'

export type Situation = { name: string; state: string; interruptible: boolean }

export type HangDeps = {
  channelDir: string
  inject: (chatId: string, text: string) => void   // 写一条内部 inbox 消息（[hang-check] 开头）
  probe: () => Situation | null                    // 查当前作息活动；查不到返回 null
  now?: () => number
  rand?: () => number                              // 抖动源，测试可固定
  log?: (line: string) => void
}

export type HangRuntime = {
  onInbound: (chatId: string, tsMs?: number) => void
  onOutbound: (chatId: string) => void             // 只在私聊 /send 成功后调用
  tick: () => void                                 // 生产 60s 粒度
  live: () => number                               // 诊断/测试：状态表条目数
}

const MIN = 60_000
const RECENT_INBOUND_MS = 10 * MIN     // 出站前这么久内有入站 = 这条是"回她的话"
const STALE_MS = 6 * 3600_000          // 机器睡一夜/桥长挂后别再追几小时前的事
const PROBE_WARN_MS = 10 * MIN         // 桥挂时的告警节流

const STATE_FILE = '.hang-state.json'
const ARCHIVE_FILE = '.hang-archive.json'

/** 睡眠判定：作息表的 state 字段。configs 里是英文枚举（sleeping），situation.py 的
 *  兜底也返回 sleeping；中文写法一并认，免得改了 yml 就失效。 */
export function isAsleep(state: unknown): boolean {
  return typeof state === 'string' && /睡|sleep/i.test(state)
}

function readJsonObject(path: string): Record<string, any> {
  try {
    const o = JSON.parse(readFileSync(path, 'utf8'))
    return o && typeof o === 'object' && !Array.isArray(o) ? o : {}
  } catch { return {} }
}

function writeJsonAtomic(path: string, data: unknown): void {
  const tmp = `${path}.tmp`
  writeFileSync(tmp, JSON.stringify(data))
  renameSync(tmp, path)
}

/**
 * 取出并清掉该 chat 的被晾档案，返回渲染好的迟到反应文本（无档案 → 空串）。
 * 由注入侧（worker-plugin 投递每条消息时）调用：她"下课"后看到的第一条消息就带上这段。
 * 读-改-写不加锁：与 dispatcher 的写档撞车最坏是多带一次或少带一次，不影响正确性，
 * 也绝不能让它抛错阻断投递——任何异常都返回空串。
 */
export function takeHangArchive(channelDir: string, chatId: string, nowMs = Date.now()): string {
  try {
    const path = join(channelDir, ARCHIVE_FILE)
    if (!existsSync(path)) return ''
    const all = readJsonObject(path)
    const rec = all[String(chatId)]
    if (!rec || typeof rec !== 'object') return ''
    delete all[String(chatId)]
    writeJsonAtomic(path, all)
    const archivedMin = Number(rec.minutes)
    const at = Number(rec.at)
    // 档案落盘后到她真正看见之间还在继续没回 → 时长要接着算
    const sinceArchive = Number.isFinite(at) && nowMs > at ? (nowMs - at) / MIN : 0
    const minutes = Math.max(0, Math.round((Number.isFinite(archivedMin) ? archivedMin : 0) + sinceArchive))
    return renderHangPrompt('lateReaction', minutes, typeof rec.activity === 'string' ? rec.activity : '忙别的')
  } catch { return '' }
}

export function createHangRuntime(deps: HangDeps): HangRuntime {
  const { channelDir, inject, probe } = deps
  const now = deps.now ?? Date.now
  const rand = deps.rand ?? Math.random
  const log = deps.log ?? (() => {})
  const statePath = join(channelDir, STATE_FILE)
  const archivePath = join(channelDir, ARCHIVE_FILE)

  // 落盘形态：{ chats: {<chat_id>: HangState}, day: {key, count} }。
  // day 单独存：decideHang 的契约是"inbound 一律清事件"，当日额度跟着事件一起没了 →
  // 一天里她被晾五次也只算第一次。额度是"每 bot 每天"的，所以由本层保管、进出决策时带上。
  const persisted = readJsonObject(statePath)
  const states: Record<string, HangState> = (persisted.chats && typeof persisted.chats === 'object'
    && !Array.isArray(persisted.chats)) ? persisted.chats : {}
  const day: { key: string; count: number } = (() => {
    const d = persisted.day
    return (d && typeof d === 'object' && typeof d.key === 'string'
      && typeof d.count === 'number' && Number.isFinite(d.count) && d.count >= 0)
      ? { key: d.key, count: d.count } : { key: '', count: 0 }
  })()
  // 最后入站时刻：启动时读 P3 的文件（worker-plugin 写、这里只读），之后由 onInbound 维护。
  const lastInbound: Record<string, number> = (() => {
    const raw = readJsonObject(join(channelDir, '.last-inbound-ts.json'))
    const out: Record<string, number> = {}
    for (const [k, v] of Object.entries(raw)) if (typeof v === 'number' && Number.isFinite(v)) out[k] = v
    return out
  })()

  let lastProbeWarn = 0

  function persist(): void {
    try { writeJsonAtomic(statePath, { chats: states, day }) }
    catch (e) { log(`hang_persist_failed reason=${e instanceof Error ? e.name : 'unknown'}`) }
  }

  /** 当日已用额度（跨天自动归零） */
  function usedToday(t: number): number {
    return day.key === hangDayKey(t) ? day.count : 0
  }

  /** 进决策前把当日额度塞回状态里；没有现役事件但今天已用过额度时，造一个"已终结"的壳带额度。 */
  function seed(chatId: string, t: number): HangState | null {
    const used = usedToday(t)
    const cur = states[chatId]
    if (cur && typeof cur === 'object') return { ...cur, episodesToday: used, dayKey: hangDayKey(t) }
    if (used === 0) return null
    return { chatId, armedAt: t, stage: 3, nextAt: t, topicHint: '', episodesToday: used, dayKey: hangDayKey(t) }
  }

  /** 出决策后把额度收回本层保管（inbound 清事件也带不走它） */
  function absorb(next: HangState | null, t: number): void {
    if (!next) return
    const used = usedToday(t)          // 必须在改 day.key 之前取，否则跨天时会把昨天的数留下
    day.key = hangDayKey(t)
    day.count = Math.max(used, next.episodesToday)
  }

  /** 落状态：null = 事件清除。返回是否真的变了（省掉无谓的落盘）。 */
  function store(chatId: string, next: HangState | null): boolean {
    if (next === null) {
      if (!(chatId in states)) return false
      delete states[chatId]
      return true
    }
    const prev = states[chatId]
    if (prev && prev.stage === next.stage && prev.nextAt === next.nextAt
        && prev.armedAt === next.armedAt && prev.episodesToday === next.episodesToday
        && prev.dayKey === next.dayKey) return false
    states[chatId] = next
    return true
  }

  function archive(chatId: string, minutes: number, activity: string, at: number): void {
    try {
      const all = readJsonObject(archivePath)
      all[chatId] = { minutes, activity, at }
      writeJsonAtomic(archivePath, all)
    } catch (e) {
      log(`hang_archive_failed chat=${chatId} reason=${e instanceof Error ? e.name : 'unknown'}`)
    }
  }

  function onInbound(chatId: string, tsMs?: number): void {
    const id = String(chatId)
    const t = typeof tsMs === 'number' && Number.isFinite(tsMs) ? tsMs : now()
    lastInbound[id] = t
    const cur = states[id]
    if (!cur) return                                   // 没有在跟的事件，只记时刻
    const r = decideHang(cur, { kind: 'inbound', chatId: id, nowMs: now() }, rand())
    if (store(id, r.state)) { persist(); log(`hang_cancel chat=${id} reason=inbound`) }
  }

  function onOutbound(chatId: string): void {
    const id = String(chatId)
    const t = now()
    const hadRecentInbound = t - (lastInbound[id] ?? 0) <= RECENT_INBOUND_MS
    const r = decideHang(seed(id, t),
      { kind: 'outboundReply', chatId: id, nowMs: t, hadRecentInbound }, rand())
    const armed = r.state !== null && r.state.stage === 0 && r.state.armedAt === t
    absorb(r.state, t)
    if (store(id, r.state)) {
      persist()
      if (armed) log(`hang_arm chat=${id} stage=0`)
    }
  }

  function runAction(chatId: string, action: { kind: string; stage?: number },
                     minutes: number, sit: Situation): void {
    switch (action.kind) {
      case 'injectFollowup':
        inject(chatId, renderHangPrompt(action.stage === 2 ? 'followup2' : 'followup1', minutes, sit.name))
        log(`hang_followup chat=${chatId} stage=${action.stage}`)
        break
      case 'injectMemoryWrite':
        inject(chatId, renderHangPrompt('memoryWrite', minutes, sit.name))
        log(`hang_memory_write chat=${chatId} stage=3`)
        break
      case 'archiveOnly':
        archive(chatId, minutes, sit.name, now())
        log(`hang_archive chat=${chatId} reason=uninterruptible`)
        break
      default:
        break
    }
  }

  function tick(): void {
    const t = now()
    const today = hangDayKey(t)
    const due: string[] = []
    let changed = false
    for (const [chatId, s] of Object.entries(states)) {
      // 结构坏掉的条目：decideHang 会当没事件，这里顺手清掉免得越积越多
      if (!s || typeof s !== 'object' || !Number.isFinite(s.armedAt) || !Number.isFinite(s.nextAt)
          || !Number.isInteger(s.stage) || s.stage < 0 || s.stage > 3) {
        delete states[chatId]; changed = true; continue
      }
      if (s.stage === 3) {
        // 终结的事件只为留当日计数，隔天就没用了
        if (s.dayKey !== today) { delete states[chatId]; changed = true }
        continue
      }
      // 机器睡了一夜 / 桥挂了半天：醒来别去追几小时前的那句
      if (t - s.armedAt > STALE_MS) {
        delete states[chatId]; changed = true
        log(`hang_drop chat=${chatId} stage=${s.stage} reason=stale`)
        continue
      }
      if (t >= s.nextAt) due.push(chatId)
    }
    if (due.length === 0) { if (changed) persist(); return }

    const sit = probe()
    if (!sit) {
      // 桥挂/超时：这一轮什么都不做（事件留着，下一轮再看），绝不瞎追
      if (t - lastProbeWarn > PROBE_WARN_MS) {
        lastProbeWarn = t
        log(`hang_probe_failed count=${due.length} reason=bridge_unavailable`)
      }
      if (changed) persist()
      return
    }
    const asleep = isAsleep(sit.state)
    for (const chatId of due) {
      const cur = states[chatId]
      if (!cur) continue
      const minutes = Math.max(0, Math.round((t - cur.armedAt) / MIN))
      const r = decideHang(seed(chatId, t),
        { kind: 'tick', nowMs: t, activityInterruptible: sit.interruptible !== false, asleep }, rand())
      absorb(r.state, t)
      if (store(chatId, r.state)) changed = true
      if (r.action.kind !== 'none') runAction(chatId, r.action, minutes, sit)
    }
    if (changed) persist()
  }

  return { onInbound, onOutbound, tick, live: () => Object.keys(states).length }
}
